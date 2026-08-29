"""Deterministic branded images for Telegram match posts."""
from __future__ import annotations

import io
from collections import OrderedDict
import hashlib
import logging
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Sequence
from urllib.parse import urlparse

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from .match_sources.models import MatchNormalized, TournamentRadar, UpcomingMatchNormalized
from .match_sources.storage import read_cached_logo, write_cached_logo

RESULT_CARD_SIZE = (1080, 1080)
SCHEDULE_CARD_SIZE = (1080, 1080)
MAX_RESULT_MATCHES = 10
MAX_SCHEDULE_MATCHES = 10
MAX_SCHEDULE_TOTAL_MATCHES = 20
MAX_LOGO_BYTES = 2_000_000
MAX_LOGO_PIXELS = 4_000_000
# PandaScore's CDN can take longer than two seconds to start a cold response.
# This is still bounded per image and stays within the results-card budget.
LOGO_DOWNLOAD_TIMEOUT_SECONDS = 5.0
MAX_LOGO_MEMORY_CACHE_ITEMS = 256
LOGO_HOSTS = {
    "cdn-api.pandascore.co",
    "cdn.pandascore.co",
}
ALLOWED_LOGO_TYPES = {
    "application/octet-stream",
    "image/png",
    "image/jpeg",
    "image/webp",
}

logger = logging.getLogger(__name__)
_logo_memory_cache: OrderedDict[str, bytes] = OrderedDict()

NAVY = (7, 17, 32)
PANEL = (18, 38, 61)
PANEL_LIGHT = (24, 49, 76)
WHITE = (244, 247, 251)
MUTED = (150, 168, 191)
CYAN = (22, 199, 255)
AMBER = (255, 159, 28)
LOGO_PLATE_DARK = (10, 24, 43)
LOGO_PLATE_LIGHT = (220, 230, 240)
MONTH_NAMES = (
    "",
    "ЯНВАРЯ",
    "ФЕВРАЛЯ",
    "МАРТА",
    "АПРЕЛЯ",
    "МАЯ",
    "ИЮНЯ",
    "ИЮЛЯ",
    "АВГУСТА",
    "СЕНТЯБРЯ",
    "ОКТЯБРЯ",
    "НОЯБРЯ",
    "ДЕКАБРЯ",
)

ASSET_DIR = Path(__file__).resolve().parent / "assets"
FONT_DIR = ASSET_DIR / "fonts"
CHANNEL_LOGO = ASSET_DIR / "channel-logo.png"
DISPLAY_FONT = FONT_DIR / "RussoOne-Regular.ttf"
LATIN_BOLD_FONT = FONT_DIR / "Rajdhani-Bold.ttf"
LATIN_MEDIUM_FONT = FONT_DIR / "Rajdhani-Medium.ttf"


class MediaCardError(RuntimeError):
    """A recoverable card-rendering or logo-download error."""


def _font(size: int, *, display: bool = False, medium: bool = False) -> ImageFont.FreeTypeFont:
    path = DISPLAY_FONT if display else LATIN_MEDIUM_FONT if medium else LATIN_BOLD_FONT
    try:
        return ImageFont.truetype(str(path), size=size)
    except OSError as exc:
        raise MediaCardError("Bundled media font is unavailable") from exc


def _header_accent_segments(width: int) -> tuple[tuple[int, int], tuple[int, int]]:
    outer_margin = 55
    center_gap = 110
    return (
        (outer_margin, width // 2 - center_gap // 2),
        (width // 2 + center_gap // 2, width - outer_margin),
    )


def _background(size: tuple[int, int], *, header_accent_y: int = 58) -> Image.Image:
    width, height = size
    image = Image.new("RGB", size, NAVY)
    pixels = image.load()
    for y in range(height):
        vertical = y / max(height - 1, 1)
        for x in range(width):
            horizontal = abs(x - width / 2) / (width / 2)
            glow = max(0.0, 1.0 - math.hypot(horizontal * 0.8, (vertical - 0.35) * 1.2))
            pixels[x, y] = (
                int(NAVY[0] + 7 * glow),
                int(NAVY[1] + 20 * glow),
                int(NAVY[2] + 31 * glow),
            )

    draw = ImageDraw.Draw(image, "RGBA")
    margin = 34
    draw.rounded_rectangle(
        (margin, margin, width - margin, height - margin),
        radius=42,
        outline=(52, 91, 139, 150),
        width=2,
    )
    accent_segments = _header_accent_segments(width)
    for (x0, x1), color in zip(accent_segments, (CYAN, AMBER), strict=True):
        draw.line((x0, header_accent_y, x1, header_accent_y), fill=(*color, 220), width=8)
    for index in range(7):
        x = -180 + index * 225
        draw.line((x, height, x + 520, 0), fill=(39, 75, 119, 25), width=2)
    return image


def _fit_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    max_width: int,
    max_size: int,
    min_size: int,
    *,
    display: bool = False,
    medium: bool = False,
) -> ImageFont.FreeTypeFont:
    for size in range(max_size, min_size - 1, -2):
        candidate = _font(size, display=display, medium=medium)
        if draw.textbbox((0, 0), text, font=candidate)[2] <= max_width:
            return candidate
    return _font(min_size, display=display, medium=medium)


def _centered_text(
    draw: ImageDraw.ImageDraw,
    center_x: int,
    y: int,
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int],
) -> None:
    box = draw.textbbox((0, 0), text, font=font)
    draw.text((center_x - (box[2] - box[0]) / 2, y), text, font=font, fill=fill)


def _centered_text_on_point(
    draw: ImageDraw.ImageDraw,
    center_x: int,
    center_y: int,
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int],
) -> None:
    """Draw text whose visible bounding-box centre is at the requested point."""
    box = draw.textbbox((0, 0), text, font=font)
    draw.text(
        (
            center_x - (box[0] + box[2]) / 2,
            center_y - (box[1] + box[3]) / 2,
        ),
        text,
        font=font,
        fill=fill,
    )


def _aligned_text(
    draw: ImageDraw.ImageDraw,
    edge_x: int,
    y: int,
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int],
    alignment: str,
) -> None:
    """Draw text against a fixed outer edge so long team names grow inward."""
    box = draw.textbbox((0, 0), text, font=font)
    if alignment == "left":
        text_x = edge_x - box[0]
    elif alignment == "right":
        text_x = edge_x - box[2]
    else:
        raise ValueError("alignment must be left or right")
    draw.text((text_x, y), text, font=font, fill=fill)


def _safe_logo_url(value: str | None) -> str | None:
    if not value or len(value) > 2048:
        return None
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or port not in (None, 443)
        or (parsed.hostname or "").casefold() not in LOGO_HOSTS
        or not re.match(r"^/images/(?:team|league|serie|tournament)/image/", parsed.path)
    ):
        return None
    return value


def _logo_candidates(url: str) -> list[str]:
    """Prefer PandaScore's small official thumbnail, then the original logo."""
    parsed = urlparse(url)
    path_parts = parsed.path.rsplit("/", 1)
    if len(path_parts) != 2 or path_parts[1].startswith(
        ("thumb_", "normal_", "250px_", "800px_")
    ):
        return [url]
    thumbnail_path = f"{path_parts[0]}/thumb_{path_parts[1]}"
    thumbnail = parsed._replace(path=thumbnail_path).geturl()
    return [thumbnail, url]


def _decode_logo(raw: bytes) -> Image.Image:
    if not raw or len(raw) > MAX_LOGO_BYTES:
        raise MediaCardError("Team logo is too large or empty")
    try:
        with Image.open(io.BytesIO(raw)) as probe:
            if probe.width * probe.height > MAX_LOGO_PIXELS:
                raise MediaCardError("Team logo dimensions are too large")
            probe.verify()
        logo = Image.open(io.BytesIO(raw))
        logo.load()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        error = MediaCardError("Team logo is not a valid image")
        error.__cause__ = exc
        raise error
    return logo.convert("RGBA")


def _remember_logo(url: str, raw: bytes) -> None:
    _logo_memory_cache[url] = raw
    _logo_memory_cache.move_to_end(url)
    while len(_logo_memory_cache) > MAX_LOGO_MEMORY_CACHE_ITEMS:
        _logo_memory_cache.popitem(last=False)


def _cached_logo(url: str) -> Image.Image | None:
    raw = _logo_memory_cache.get(url)
    if raw is not None:
        _logo_memory_cache.move_to_end(url)
    else:
        try:
            raw = read_cached_logo(url)
        except Exception:
            raw = None
        if raw is not None:
            _remember_logo(url, raw)
    if raw is None:
        return None
    try:
        return _decode_logo(raw)
    except MediaCardError:
        logger.warning("team_logo_cache_invalid key=%s", hashlib.sha256(url.encode()).hexdigest()[:12])
        return None


def _cache_logo(url: str, logo: Image.Image) -> None:
    output = io.BytesIO()
    logo.save(output, "PNG", optimize=True)
    raw = output.getvalue()
    if len(raw) > MAX_LOGO_BYTES:
        return
    _remember_logo(url, raw)
    try:
        write_cached_logo(url, raw)
    except Exception:
        pass


def fetch_team_logo(
    url: str | None,
    timeout: float = LOGO_DOWNLOAD_TIMEOUT_SECONDS,
) -> Image.Image | None:
    """Download and validate a PandaScore team logo without following redirects."""
    safe_url = _safe_logo_url(url)
    if not safe_url:
        return None
    cached = _cached_logo(safe_url)
    if cached is not None:
        return cached
    last_error: MediaCardError | None = None
    for candidate_url in _logo_candidates(safe_url):
        try:
            response = requests.get(
                candidate_url,
                timeout=timeout,
                allow_redirects=False,
                stream=True,
                headers={"User-Agent": "CS2ResultsBot/0.5"},
            )
        except requests.RequestException as exc:
            last_error = MediaCardError("Team logo request failed")
            last_error.__cause__ = exc
            continue

        try:
            if response.status_code >= 300:
                raise MediaCardError(f"Team logo returned HTTP {response.status_code}")
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].casefold()
            if content_type not in ALLOWED_LOGO_TYPES:
                raise MediaCardError("Team logo content type is not allowed")
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > MAX_LOGO_BYTES:
                        raise MediaCardError("Team logo is too large")
                except ValueError as exc:
                    raise MediaCardError("Team logo content length is invalid") from exc

            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_LOGO_BYTES:
                    raise MediaCardError("Team logo is too large")
                chunks.append(chunk)
        except MediaCardError as exc:
            last_error = exc
            continue
        finally:
            response.close()

        try:
            logo = _decode_logo(b"".join(chunks))
        except MediaCardError as exc:
            last_error = exc
            continue
        _cache_logo(safe_url, logo)
        return logo

    raise last_error or MediaCardError("Team logo is unavailable")


def _initials(name: str) -> str:
    words = [word for word in name.replace("-", " ").split() if word]
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    return name[:2].upper()


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    """Return WCAG relative luminance for an sRGB colour."""
    channels = []
    for value in rgb:
        channel = value / 255
        channels.append(
            channel / 12.92
            if channel <= 0.04045
            else ((channel + 0.055) / 1.055) ** 2.4
        )
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _logo_plate_fill(logo: Image.Image) -> tuple[int, int, int]:
    """Choose the plate that keeps the largest part of a logo legible.

    Team marks often combine transparent padding with black, white and saturated
    colours. Scoring their visible pixels against both approved plates is more
    reliable than treating the image's average colour as the logo colour.
    """
    sample = ImageOps.contain(logo.convert("RGBA"), (96, 96))
    pixels = sample.load()
    visible_pixels = [
        pixels[x, y]
        for y in range(sample.height)
        for x in range(sample.width)
        if pixels[x, y][3] >= 32
    ]
    if not visible_pixels:
        return LOGO_PLATE_DARK

    def contrast_score(background: tuple[int, int, int]) -> float:
        background_luminance = _relative_luminance(background)
        weighted_score = 0.0
        total_weight = 0.0
        for red, green, blue, alpha in visible_pixels:
            foreground_luminance = _relative_luminance((red, green, blue))
            lighter = max(foreground_luminance, background_luminance)
            darker = min(foreground_luminance, background_luminance)
            contrast = (lighter + 0.05) / (darker + 0.05)
            weight = alpha / 255
            # Capping prevents a few pure black/white pixels from outweighing
            # the main shape of a multi-colour emblem.
            weighted_score += min(contrast, 7.0) * weight
            total_weight += weight
        return weighted_score / total_weight

    dark_score = contrast_score(LOGO_PLATE_DARK)
    light_score = contrast_score(LOGO_PLATE_LIGHT)
    return LOGO_PLATE_LIGHT if light_score > dark_score else LOGO_PLATE_DARK


def _draw_logo_plate(
    draw: ImageDraw.ImageDraw,
    center: tuple[int, int],
    diameter: int,
    accent: tuple[int, int, int],
    fill: tuple[int, int, int],
) -> None:
    x, y = center
    radius = diameter // 2
    line_width = max(3, diameter // 45)
    draw.ellipse(
        (x - radius + 2, y - radius + 7, x + radius + 2, y + radius + 7),
        fill=(0, 4, 12, 105),
    )
    draw.ellipse(
        (x - radius, y - radius, x + radius, y + radius),
        fill=(*fill, 250),
        outline=(*accent, 225),
        width=line_width,
    )
    inset = line_width + max(2, diameter // 60)
    inner_outline = (255, 255, 255, 65) if fill == LOGO_PLATE_LIGHT else (84, 119, 157, 90)
    draw.ellipse(
        (x - radius + inset, y - radius + inset, x + radius - inset, y + radius - inset),
        outline=inner_outline,
        width=max(1, line_width // 2),
    )


def _draw_logo(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    center: tuple[int, int],
    diameter: int,
    team_name: str,
    logo_url: str | None,
    accent: tuple[int, int, int],
    fallback_logo_url: str | None = None,
) -> None:
    x, y = center
    logo = None
    failures: list[str] = []
    logo_urls = list(dict.fromkeys(url for url in (logo_url, fallback_logo_url) if url))
    for candidate_url in logo_urls:
        try:
            logo = fetch_team_logo(candidate_url)
        except MediaCardError as exc:
            failures.append(str(exc))
            continue
        if logo is not None:
            break
    if logo is None and logo_urls:
        logger.warning(
            "team_logo_fallback team=%s hosts=%s errors=%s",
            team_name,
            [urlparse(url).hostname or "missing" for url in logo_urls],
            failures or ["unavailable"],
        )
    if logo is not None:
        _draw_logo_plate(draw, center, diameter, accent, _logo_plate_fill(logo))
        contained = ImageOps.contain(logo, (int(diameter * 0.64), int(diameter * 0.64)))
        canvas.alpha_composite(contained, (x - contained.width // 2, y - contained.height // 2))
        return
    _draw_logo_plate(draw, center, diameter, accent, LOGO_PLATE_DARK)
    initials_font = _fit_font(
        draw,
        _initials(team_name),
        int(diameter * 0.65),
        int(diameter * 0.34),
        22,
    )
    box = draw.textbbox((0, 0), _initials(team_name), font=initials_font)
    draw.text(
        (x - (box[2] - box[0]) / 2, y - (box[3] - box[1]) / 2 - box[1]),
        _initials(team_name),
        font=initials_font,
        fill=WHITE,
    )


def _draw_tournament_logo(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    center: tuple[int, int],
    diameter: int,
    logo_url: str | None,
) -> None:
    """Draw an official event mark when PandaScore provides one; never invent it."""
    if not logo_url:
        return
    try:
        logo = fetch_team_logo(logo_url)
    except MediaCardError as exc:
        logger.warning(
            "tournament_logo_unavailable host=%s error=%s",
            urlparse(logo_url).hostname or "missing",
            exc,
        )
        return
    if logo is None:
        return
    _draw_logo_plate(draw, center, diameter, CYAN, _logo_plate_fill(logo))
    contained = ImageOps.contain(logo, (int(diameter * 0.64), int(diameter * 0.64)))
    canvas.alpha_composite(
        contained,
        (center[0] - contained.width // 2, center[1] - contained.height // 2),
    )


def _schedule_tournament_header(
    matches: Sequence[UpcomingMatchNormalized],
) -> tuple[str, str | None]:
    """Return one truthful event label for the whole card, or a neutral mixed-day label."""
    labels: dict[str, tuple[str, str | None]] = {}
    for match in matches:
        # Keep the full title, including the stage. It is the same source of truth
        # as the individual fixture cards and avoids silently merging group and
        # playoff matches under one series name.
        label = match.tournament_name
        labels.setdefault(label.casefold(), (label, match.tournament_logo_url))
    if len(labels) == 1:
        return next(iter(labels.values()))
    return "ТУРНИРЫ ДНЯ", None


def _draw_schedule_header(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    matches: Sequence[UpcomingMatchNormalized],
    local_now: datetime,
    page_number: int,
    page_count: int,
) -> None:
    width, _ = canvas.size
    _draw_channel_logo(canvas, draw, (width // 2, 98), 100)
    _centered_text(draw, width // 2, 197, "МАТЧИ CS2 СЕГОДНЯ", _font(36, display=True), WHITE)
    tournament_name, tournament_logo_url = _schedule_tournament_header(matches)
    tournament_text = tournament_name.upper()
    tournament_font = _fit_font(
        draw,
        tournament_text,
        800 if tournament_logo_url else 900,
        36,
        18,
        display=True,
    )
    if tournament_logo_url:
        text_box = draw.textbbox((0, 0), tournament_text, font=tournament_font)
        text_width = text_box[2] - text_box[0]
        group_width = 54 + 16 + text_width
        logo_x = width // 2 - group_width // 2 + 27
        text_center_x = logo_x + 27 + 16 + text_width // 2
        _draw_tournament_logo(canvas, draw, (logo_x, 268), 54, tournament_logo_url)
    else:
        text_center_x = width // 2
    _centered_text(draw, text_center_x, 254, tournament_text, tournament_font, CYAN)

    date_label = f"{local_now.day} {MONTH_NAMES[local_now.month]}"
    if page_count > 1:
        date_label = f"{date_label} · {page_number}/{page_count}"
    _centered_text(draw, width // 2, 315, date_label, _font(22, display=True), AMBER)


def _draw_channel_logo(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    center: tuple[int, int],
    diameter: int,
) -> None:
    """Place the bundled channel mark in the header without dominating it."""
    try:
        with Image.open(CHANNEL_LOGO) as source:
            logo = ImageOps.fit(source.convert("RGBA"), (diameter, diameter))
    except (OSError, UnidentifiedImageError) as exc:
        raise MediaCardError("Bundled channel logo is unavailable") from exc

    mask = Image.new("L", (diameter, diameter), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, diameter - 1, diameter - 1), fill=255)
    logo.putalpha(mask)
    x, y = center
    radius = diameter // 2
    draw.ellipse(
        (x - radius - 5, y - radius - 5, x + radius + 5, y + radius + 5),
        fill=(0, 5, 15, 150),
        outline=(69, 104, 151, 125),
        width=2,
    )
    canvas.alpha_composite(logo, (x - radius, y - radius))


def _schedule_time(match: UpcomingMatchNormalized, display_timezone: object) -> str:
    try:
        parsed = datetime.fromisoformat(match.scheduled_at.replace("Z", "+00:00"))
        return parsed.astimezone(display_timezone).strftime("%H:%M")
    except ValueError:
        return "—"


def _draw_wide_schedule_match(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    match: UpcomingMatchNormalized,
    box: tuple[int, int, int, int],
    display_timezone: object,
) -> None:
    x0, y0, x1, y1 = box
    height = y1 - y0
    draw.rounded_rectangle(box, radius=30, fill=(*PANEL, 237))
    draw.line((x0 + 28, y0 + 1, x0 + 168, y0 + 1), fill=(*CYAN, 190), width=3)
    draw.line((x1 - 168, y0 + 1, x1 - 28, y0 + 1), fill=(*AMBER, 190), width=3)

    logo_diameter = min(142, max(88, int(height * 0.42)))
    logo_y = y0 + int(height * 0.34)
    team1_x = x0 + 135
    team2_x = x1 - 135
    _draw_logo(
        canvas,
        draw,
        (team1_x, logo_y),
        logo_diameter,
        match.team1_name,
        match.team1_logo_url,
        CYAN,
        match.team1_logo_fallback_url,
    )
    _draw_logo(
        canvas,
        draw,
        (team2_x, logo_y),
        logo_diameter,
        match.team2_name,
        match.team2_logo_url,
        AMBER,
        match.team2_logo_fallback_url,
    )

    time_size = 64 if height >= 300 else 54 if height >= 250 else 46
    team_size = 42 if height >= 300 else 36 if height >= 250 else 30
    event_size = 24 if height >= 300 else 21 if height >= 250 else 18
    _centered_text(
        draw,
        (x0 + x1) // 2,
        y0 + int(height * 0.27),
        _schedule_time(match, display_timezone),
        _font(time_size),
        AMBER,
    )
    team_width = (x1 - x0) // 2 - 80
    team_y = y0 + int(height * 0.61)
    team1 = match.team1_name.upper()
    team2 = match.team2_name.upper()
    _aligned_text(
        draw,
        x0 + 48,
        team_y,
        team1,
        _fit_font(draw, team1, team_width, team_size, 18),
        WHITE,
        "left",
    )
    _aligned_text(
        draw,
        x1 - 48,
        team_y,
        team2,
        _fit_font(draw, team2, team_width, team_size, 18),
        WHITE,
        "right",
    )
    event = match.tournament_name.upper()
    _centered_text(
        draw,
        (x0 + x1) // 2,
        y1 - (54 if height >= 300 else 46),
        event,
        _fit_font(draw, event, x1 - x0 - 300, event_size, 14, display=True),
        MUTED,
    )


def _draw_compact_schedule_match(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    match: UpcomingMatchNormalized,
    box: tuple[int, int, int, int],
    display_timezone: object,
) -> None:
    x0, y0, x1, y1 = box
    height = y1 - y0
    center_x = (x0 + x1) // 2
    draw.rounded_rectangle(box, radius=min(26, height // 5), fill=(*PANEL, 237))
    draw.line((x0 + 22, y0 + 1, center_x - 22, y0 + 1), fill=(*CYAN, 185), width=2)
    draw.line((center_x + 22, y0 + 1, x1 - 22, y0 + 1), fill=(*AMBER, 185), width=2)

    # Every dense schedule card uses the same visual hierarchy: two centred
    # team blocks and a time badge in the exact centre of the fixture.
    dense_layout = height < 125
    if height >= 210:
        logo_diameter, time_size, team_size, event_size = 84, 46, 26, 15
    elif height >= 165:
        logo_diameter, time_size, team_size, event_size = 70, 40, 22, 13
    elif dense_layout:
        logo_diameter, time_size, team_size, event_size = 48, 32, 15, 9
    else:
        logo_diameter, time_size, team_size, event_size = 58, 36, 18, 11
    if dense_layout:
        # Leave a deliberate breathing margin under the top accent line.
        logo_y = y0 + int(height * 0.42)
        time_y = y0 + int(height * 0.32)
        team_y = y0 + int(height * 0.68)
        event_y = y1 - 18
    else:
        logo_y = y0 + int(height * 0.40)
        time_y = y0 + int(height * 0.33)
        team_y = y0 + int(height * 0.68)
        event_y = y1 - max(25, int(height * 0.16))
    team1_x = x0 + int((center_x - x0) * 0.48)
    team2_x = x1 - int((x1 - center_x) * 0.48)
    _draw_logo(
        canvas,
        draw,
        (team1_x, logo_y),
        logo_diameter,
        match.team1_name,
        match.team1_logo_url,
        CYAN,
        match.team1_logo_fallback_url,
    )
    _draw_logo(
        canvas,
        draw,
        (team2_x, logo_y),
        logo_diameter,
        match.team2_name,
        match.team2_logo_url,
        AMBER,
        match.team2_logo_fallback_url,
    )

    _centered_text(
        draw,
        center_x,
        time_y,
        _schedule_time(match, display_timezone),
        _font(time_size),
        AMBER,
    )
    name_width = int((center_x - x0) * 0.82)
    team1 = match.team1_name.upper()
    team2 = match.team2_name.upper()
    _centered_text(
        draw,
        team1_x,
        team_y,
        team1,
        _fit_font(draw, team1, name_width, team_size, 11),
        WHITE,
    )
    _centered_text(
        draw,
        team2_x,
        team_y,
        team2,
        _fit_font(draw, team2, name_width, team_size, 11),
        WHITE,
    )

    event = match.tournament_name.upper()
    _centered_text(
        draw,
        center_x,
        event_y,
        event,
        _fit_font(draw, event, x1 - x0 - 112, event_size, 10, display=True),
        MUTED,
    )


def _radar_standing_entries(radar: TournamentRadar) -> list[tuple[int, str, str | None]]:
    if radar.standing_teams:
        return [(team.rank, team.name, team.logo_url) for team in radar.standing_teams[:4]]
    entries: list[tuple[int, str, str | None]] = []
    for line in radar.standings[:4]:
        rank, separator, name = line.partition(".")
        if separator and rank.strip().isdigit() and name.strip():
            entries.append((int(rank.strip()), name.strip(), None))
    return entries


def _radar_header(canvas: Image.Image, draw: ImageDraw.ImageDraw, tournament_name: str, subtitle: str) -> None:
    width, _ = canvas.size
    _draw_channel_logo(canvas, draw, (width // 2, 98), 100)
    _centered_text(draw, width // 2, 207, "ТУРНИРНЫЙ РАДАР", _font(44, display=True), WHITE)
    _centered_text(
        draw,
        width // 2,
        276,
        tournament_name.upper(),
        _fit_font(draw, tournament_name.upper(), 860, 28, 16, display=True),
        CYAN,
    )
    _centered_text(draw, width // 2, 323, subtitle.upper(), _font(22, display=True), AMBER)


def _draw_radar_standings(canvas: Image.Image, draw: ImageDraw.ImageDraw, radar: TournamentRadar) -> None:
    entries = _radar_standing_entries(radar)
    if not entries:
        _centered_text(draw, 540, 560, "ПОЛОЖЕНИЕ ПОКА НЕ ОПУБЛИКОВАНО", _font(24, display=True), MUTED)
        return
    row_height = 118
    top = 386
    for index, (rank, name, logo_url) in enumerate(entries):
        y0 = top + index * (row_height + 14)
        draw.rounded_rectangle((70, y0, 1010, y0 + row_height), radius=24, fill=(*PANEL, 238))
        draw.rectangle(
            (70, y0, 82, y0 + row_height),
            fill=(*(CYAN if index % 2 == 0 else AMBER), 230),
        )
        _centered_text(draw, 128, y0 + 27, str(rank), _font(42), WHITE)
        _draw_logo(canvas, draw, (235, y0 + 59), 78, name, logo_url, CYAN if index % 2 == 0 else AMBER)
        _aligned_text(draw, 305, y0 + 37, name.upper(), _fit_font(draw, name.upper(), 530, 34, 18), WHITE, "left")


def _draw_radar_bracket(canvas: Image.Image, draw: ImageDraw.ImageDraw, radar: TournamentRadar) -> None:
    if not radar.bracket_matches:
        _draw_radar_standings(canvas, draw, radar)
        return
    _centered_text(draw, 540, 390, "ПОДТВЕРЖДЁННЫЕ ПАРЫ", _font(20, display=True), MUTED)
    for index, match in enumerate(radar.bracket_matches[:2]):
        y0 = 440 + index * 210
        draw.rounded_rectangle((100, y0, 980, y0 + 160), radius=26, fill=(*PANEL, 238))
        _aligned_text(
            draw, 150, y0 + 38, match.team1_name.upper(),
            _fit_font(draw, match.team1_name.upper(), 310, 32, 15), WHITE, "left"
        )
        _aligned_text(
            draw, 930, y0 + 38, match.team2_name.upper(),
            _fit_font(draw, match.team2_name.upper(), 310, 32, 15), WHITE, "right"
        )
        _centered_text(draw, 540, y0 + 40, "VS", _font(32, display=True), AMBER)
        label = (match.round_name or "СЕТКА ТУРНИРА").upper()
        _centered_text(
            draw,
            540,
            y0 + 104,
            label,
            _fit_font(draw, label, 620, 17, 11, display=True),
            MUTED,
        )


def _draw_radar_next_match(canvas: Image.Image, draw: ImageDraw.ImageDraw, radar: TournamentRadar, timezone_name: str) -> None:
    if not radar.next_matches:
        _draw_radar_standings(canvas, draw, radar)
        return
    match = radar.next_matches[0]
    try:
        from zoneinfo import ZoneInfo
        display_timezone = ZoneInfo(timezone_name)
    except Exception:
        display_timezone = timezone.utc
    _draw_logo(canvas, draw, (235, 565), 190, match.team1_name, match.team1_logo_url, CYAN, match.team1_logo_fallback_url)
    _draw_logo(canvas, draw, (845, 565), 190, match.team2_name, match.team2_logo_url, AMBER, match.team2_logo_fallback_url)
    _centered_text(draw, 540, 510, "VS", _font(72, display=True), WHITE)
    _centered_text(draw, 540, 596, _schedule_time(match, display_timezone), _font(44), AMBER)
    _aligned_text(draw, 115, 700, match.team1_name.upper(), _fit_font(draw, match.team1_name.upper(), 270, 34, 16), WHITE, "left")
    _aligned_text(draw, 965, 700, match.team2_name.upper(), _fit_font(draw, match.team2_name.upper(), 270, 34, 16), WHITE, "right")
    best_of = f"BO{match.best_of}" if match.best_of else "ФОРМАТ УТОЧНЯЕТСЯ"
    _centered_text(draw, 540, 755, best_of, _font(22, display=True), MUTED)


def render_tournament_radar_card(
    radar: TournamentRadar,
    tournament_name: str,
    timezone_name: str,
    variant: str = "auto",
) -> bytes:
    """Render one of the branded radar cards without fabricating tournament data."""
    if variant not in {"auto", "standings", "bracket", "next_match"}:
        raise MediaCardError("Unsupported radar card variant")
    chosen = variant
    if chosen == "auto":
        chosen = "next_match" if radar.next_matches else "bracket" if radar.bracket_matches else "standings"
    canvas = _background(SCHEDULE_CARD_SIZE, header_accent_y=98).convert("RGBA")
    draw = ImageDraw.Draw(canvas, "RGBA")
    subtitles = {"standings": "ПОЛОЖЕНИЕ", "bracket": "ПЛЕЙ-ОФФ", "next_match": "СЛЕДУЮЩИЙ МАТЧ"}
    _radar_header(canvas, draw, tournament_name, subtitles[chosen])
    if chosen == "standings":
        _draw_radar_standings(canvas, draw, radar)
    elif chosen == "bracket":
        _draw_radar_bracket(canvas, draw, radar)
    else:
        _draw_radar_next_match(canvas, draw, radar, timezone_name)
    facts = f"{radar.roster_team_count} УЧАСТНИКОВ   ·   {radar.bracket_match_count} МАТЧЕЙ В СЕТКЕ"
    _centered_text(draw, 540, 1007, facts, _fit_font(draw, facts, 900, 20, 13, display=True), MUTED)
    return _as_png(canvas)


def _as_png(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.convert("RGB").save(output, format="PNG", optimize=True)
    data = output.getvalue()
    if not data:
        raise MediaCardError("Rendered media card is empty")
    return data


def _winner_side(match: MatchNormalized) -> str | None:
    if match.score1 is None or match.score2 is None or match.score1 == match.score2:
        return None
    return "left" if match.score1 > match.score2 else "right"


def _draw_wide_result_match(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    match: MatchNormalized,
    box: tuple[int, int, int, int],
    *,
    show_tournament: bool = True,
) -> None:
    x0, y0, x1, y1 = box
    height = y1 - y0
    draw.rounded_rectangle(box, radius=30, fill=(*PANEL, 237))
    draw.line((x0 + 28, y0 + 1, x0 + 168, y0 + 1), fill=(*CYAN, 190), width=3)
    draw.line((x1 - 168, y0 + 1, x1 - 28, y0 + 1), fill=(*AMBER, 190), width=3)

    # Match the schedule card's visual language: each team is a centred block,
    # with the score occupying the exact centre between the two blocks.
    logo_diameter = min(210, max(96, int(height * 0.42)))
    logo_radius = logo_diameter // 2
    logo_y = y0 + int(height * 0.35)
    team1_center = x0 + int((x1 - x0) * 0.18)
    team2_center = x1 - int((x1 - x0) * 0.18)
    _draw_logo(
        canvas,
        draw,
        (team1_center, logo_y),
        logo_diameter,
        match.team1_name,
        match.team1_logo_url,
        CYAN,
        match.team1_logo_fallback_url,
    )
    _draw_logo(
        canvas,
        draw,
        (team2_center, logo_y),
        logo_diameter,
        match.team2_name,
        match.team2_logo_url,
        AMBER,
        match.team2_logo_fallback_url,
    )

    if height >= 400:
        score_size, team_size, event_size = 110, 48, 25
    elif height >= 300:
        score_size, team_size, event_size = 78, 42, 22
    elif height >= 250:
        score_size, team_size, event_size = 58, 36, 19
    else:
        score_size, team_size, event_size = 46, 28, 15
    score = f"{match.score1 if match.score1 is not None else '—'}:{match.score2 if match.score2 is not None else '—'}"
    _centered_text_on_point(
        draw,
        (x0 + x1) // 2,
        logo_y,
        score,
        _fit_font(draw, score, 260, score_size, 36),
        WHITE,
    )
    if match.best_of is not None and height >= 250:
        _centered_text(
            draw,
            (x0 + x1) // 2,
            y0 + int(height * 0.49),
            f"BO{match.best_of}",
            _font(24 if height < 400 else 30),
            MUTED,
        )

    winner_side = _winner_side(match)
    name_gap = 30 if height >= 400 else 18 if height >= 250 else 10
    team_y = logo_y + logo_radius + name_gap
    team1 = match.team1_name.upper()
    team2 = match.team2_name.upper()
    name_width = int((x1 - x0) * 0.30)
    _centered_text(
        draw,
        team1_center,
        team_y,
        team1,
        _fit_font(draw, team1, name_width, team_size, 16),
        AMBER if winner_side == "left" else WHITE,
    )
    _centered_text(
        draw,
        team2_center,
        team_y,
        team2,
        _fit_font(draw, team2, name_width, team_size, 16),
        AMBER if winner_side == "right" else WHITE,
    )

    if show_tournament:
        footer_text = match.tournament_name.upper()
        _centered_text(
            draw,
            (x0 + x1) // 2,
            y1 - (58 if height >= 300 else 43),
            footer_text,
            _fit_font(draw, footer_text, x1 - x0 - 160, event_size, 12, display=True),
            MUTED,
        )


def _draw_compact_result_match(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    match: MatchNormalized,
    box: tuple[int, int, int, int],
) -> None:
    x0, y0, x1, y1 = box
    height = y1 - y0
    center_x = (x0 + x1) // 2
    draw.rounded_rectangle(box, radius=min(26, height // 5), fill=(*PANEL, 237))
    draw.line((x0 + 22, y0 + 1, center_x - 22, y0 + 1), fill=(*CYAN, 185), width=2)
    draw.line((center_x + 22, y0 + 1, x1 - 22, y0 + 1), fill=(*AMBER, 185), width=2)

    if height >= 210:
        logo_diameter, score_size, team_size, event_size = 84, 46, 26, 15
        logo_y, team_y, event_y = y0 + 92, y0 + 143, y1 - 46
    elif height >= 165:
        logo_diameter, score_size, team_size, event_size = 70, 40, 22, 13
        logo_y, team_y, event_y = y0 + 73, y0 + 116, y1 - 34
    else:
        logo_diameter, score_size, team_size, event_size = 44, 29, 16, 10
        logo_y, team_y, event_y = y0 + 42, y0 + 74, y1 - 19

    logo_radius = logo_diameter // 2
    team1_center = x0 + int((x1 - x0) * 0.24)
    team2_center = x1 - int((x1 - x0) * 0.24)
    _draw_logo(
        canvas,
        draw,
        (team1_center, logo_y),
        logo_diameter,
        match.team1_name,
        match.team1_logo_url,
        CYAN,
        match.team1_logo_fallback_url,
    )
    _draw_logo(
        canvas,
        draw,
        (team2_center, logo_y),
        logo_diameter,
        match.team2_name,
        match.team2_logo_url,
        AMBER,
        match.team2_logo_fallback_url,
    )

    score = f"{match.score1 if match.score1 is not None else '—'}:{match.score2 if match.score2 is not None else '—'}"
    _centered_text_on_point(draw, center_x, logo_y, score, _font(score_size), WHITE)
    winner_side = _winner_side(match)
    name_width = 180 if height >= 210 else 170 if height >= 165 else 150
    team1 = match.team1_name.upper()
    team2 = match.team2_name.upper()
    _centered_text(
        draw,
        team1_center,
        team_y,
        team1,
        _fit_font(draw, team1, name_width, team_size, 11),
        AMBER if winner_side == "left" else WHITE,
    )
    _centered_text(
        draw,
        team2_center,
        team_y,
        team2,
        _fit_font(draw, team2, name_width, team_size, 11),
        AMBER if winner_side == "right" else WHITE,
    )
    event = match.tournament_name.upper()
    _centered_text(
        draw,
        center_x,
        event_y,
        event,
        _fit_font(draw, event, x1 - x0 - 112, event_size, 10, display=True),
        MUTED,
    )


def render_result_card(match: MatchNormalized) -> bytes:
    """Render a square result card whose score is protected by Telegram media spoiler."""
    canvas = _background(RESULT_CARD_SIZE, header_accent_y=98).convert("RGBA")
    draw = ImageDraw.Draw(canvas, "RGBA")
    width, height = RESULT_CARD_SIZE

    _draw_channel_logo(canvas, draw, (width // 2, 98), 100)
    _centered_text(draw, width // 2, 207, "РЕЗУЛЬТАТ МАТЧА", _font(44, display=True), WHITE)
    tournament_font = _fit_font(
        draw,
        match.tournament_name.upper(),
        900,
        30,
        18,
        display=True,
    )
    _centered_text(
        draw,
        width // 2,
        286,
        match.tournament_name.upper(),
        tournament_font,
        CYAN,
    )
    _draw_wide_result_match(
        canvas,
        draw,
        match,
        (75, 340, 1005, 850),
        show_tournament=False,
    )
    _centered_text(
        draw,
        width // 2,
        1012,
        "CS2 TIER-1 · РЕЗУЛЬТАТЫ МАТЧЕЙ",
        _font(20, display=True),
        MUTED,
    )
    return _as_png(canvas)


def _chamfered_panel(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    *,
    cut: int = 22,
) -> None:
    x0, y0, x1, y1 = box
    points = [(x0 + cut, y0), (x1 - cut, y0), (x1, y0 + cut), (x1, y1 - cut),
              (x1 - cut, y1), (x0 + cut, y1), (x0, y1 - cut), (x0, y0 + cut)]
    draw.polygon(points, fill=(10, 19, 29, 245), outline=AMBER)
    draw.line(points + [points[0]], fill=AMBER, width=2)


def _format_usd(value: int) -> str:
    return f"${value:,}".replace(",", " ")


def can_render_final_card(match: MatchNormalized) -> bool:
    """Require explicit final metadata and complete, source-confirmed display data."""
    return (
        match.is_final
        and match.winner_prize_usd is not None
        and 3 <= len(match.maps) <= 5
        and all(item.score1 is not None and item.score2 is not None for item in match.maps)
    )


def render_final_card(match: MatchNormalized) -> bytes:
    """Render a deterministic 1080px final card with map scores and champion payout."""
    if not can_render_final_card(match):
        raise MediaCardError("Final card requires confirmed final maps and winner payout")

    canvas = _background(RESULT_CARD_SIZE, header_accent_y=82).convert("RGBA")
    draw = ImageDraw.Draw(canvas, "RGBA")
    width = RESULT_CARD_SIZE[0]
    panel = (190, 54, 890, 122)
    _chamfered_panel(draw, panel, cut=16)
    title = match.tournament_name.upper()
    _centered_text(draw, width // 2, 68, title, _fit_font(draw, title, 620, 30, 16, display=True), WHITE)
    _centered_text(draw, width // 2, 176, "ГРАНД-ФИНАЛ", _font(40, display=True), AMBER)

    score = f"{match.score1}:{match.score2}"
    score_font = _font(156, display=True)
    _centered_text(draw, width // 2, 252, score, score_font, AMBER)
    winner_side = _winner_side(match)
    name_width = 330
    left_name, right_name = match.team1_name.upper(), match.team2_name.upper()
    _aligned_text(draw, 66, 320, left_name, _fit_font(draw, left_name, name_width, 62, 20),
                  AMBER if winner_side == "left" else WHITE, "left")
    _aligned_text(draw, 1014, 320, right_name, _fit_font(draw, right_name, name_width, 62, 20),
                  AMBER if winner_side == "right" else WHITE, "right")

    table = (190, 470, 890, 470 + 76 * len(match.maps))
    _chamfered_panel(draw, table)
    x0, y0, x1, _ = table
    divider_x = (x0 + x1) // 2
    draw.line((divider_x, y0 + 16, divider_x, table[3] - 16), fill=(*AMBER, 180), width=2)
    for index, item in enumerate(match.maps):
        row_y = y0 + index * 76
        if index:
            draw.line((x0 + 18, row_y, x1 - 18, row_y), fill=(*AMBER, 150), width=1)
        _centered_text(draw, (x0 + divider_x) // 2, row_y + 18, item.name,
                       _fit_font(draw, item.name, 280, 36, 16), WHITE)
        _centered_text(draw, (divider_x + x1) // 2, row_y + 18, f"{item.score1}:{item.score2}", _font(38), AMBER)

    prize_top = table[3] + 30
    prize = (190, prize_top, 890, prize_top + 86)
    _chamfered_panel(draw, prize)
    draw.line((divider_x, prize_top + 16, divider_x, prize_top + 70), fill=(*AMBER, 180), width=2)
    _centered_text(draw, (prize[0] + divider_x) // 2, prize_top + 28, "ПРИЗОВЫЕ ПОБЕДИТЕЛЯ",
                   _fit_font(draw, "ПРИЗОВЫЕ ПОБЕДИТЕЛЯ", 300, 24, 13, display=True), AMBER)
    amount = _format_usd(match.winner_prize_usd)
    _centered_text(draw, (divider_x + prize[2]) // 2, prize_top + 20, amount,
                   _fit_font(draw, amount, 300, 54, 22, display=True), AMBER)
    _centered_text(draw, width // 2, 1012, "ИСТОЧНИК: LIQUIPEDIA", _font(18, display=True), MUTED)
    return _as_png(canvas)


def render_results_card(
    matches: Sequence[MatchNormalized],
    local_now: datetime,
) -> bytes:
    """Render an adaptive daily results card with one to ten matches."""
    if not matches or len(matches) > MAX_RESULT_MATCHES:
        raise MediaCardError("Results card supports between one and ten matches")

    canvas = _background(RESULT_CARD_SIZE, header_accent_y=98).convert("RGBA")
    draw = ImageDraw.Draw(canvas, "RGBA")
    width, height = RESULT_CARD_SIZE
    _draw_channel_logo(canvas, draw, (width // 2, 98), 100)
    _centered_text(draw, width // 2, 207, "ИТОГИ ДНЯ", _font(44, display=True), WHITE)
    _centered_text(
        draw,
        width // 2,
        286,
        f"{local_now.day} {MONTH_NAMES[local_now.month]}",
        _font(27, display=True),
        CYAN,
    )

    sorted_matches = sorted(matches, key=lambda item: item.end_date or item.date or item.start_date or "")
    columns = 1 if len(sorted_matches) <= 3 else 2
    rows = math.ceil(len(sorted_matches) / columns)
    area_top = 340
    area_bottom = 960
    gap = 14
    available_height = area_bottom - area_top - gap * (rows - 1)
    if columns == 1:
        max_row_height = {1: 390, 2: 280, 3: 195}[len(sorted_matches)]
    else:
        max_row_height = 230
    row_height = min(max_row_height, available_height // rows)
    group_height = row_height * rows + gap * (rows - 1)
    top = area_top + ((area_bottom - area_top) - group_height) // 2
    card_width = 960 if columns == 1 else 468
    column_gap = 24
    left = 60
    for index, match in enumerate(sorted_matches):
        row = index // columns
        column = index % columns
        if columns == 2 and row == rows - 1 and len(sorted_matches) % 2 == 1:
            x0 = (width - card_width) // 2
        else:
            x0 = left + column * (card_width + column_gap)
        y0 = top + row * (row_height + gap)
        box = (x0, y0, x0 + card_width, y0 + row_height)
        if columns == 1:
            _draw_wide_result_match(canvas, draw, match, box)
        else:
            _draw_compact_result_match(canvas, draw, match, box)

    _centered_text(
        draw,
        width // 2,
        1012,
        "CS2 TIER-1 · РЕЗУЛЬТАТЫ МАТЧЕЙ",
        _font(20, display=True),
        MUTED,
    )
    return _as_png(canvas)


def render_schedule_card(
    matches: Sequence[UpcomingMatchNormalized],
    local_now: datetime,
    timezone_name: str,
    *,
    page_number: int = 1,
    page_count: int = 1,
) -> bytes:
    """Render a square daily schedule with an adaptive one- or two-column grid."""
    if not matches or len(matches) > MAX_SCHEDULE_MATCHES:
        raise MediaCardError("Schedule card supports between one and ten matches")
    if page_count < 1 or page_number < 1 or page_number > page_count:
        raise MediaCardError("Schedule card page information is invalid")
    try:
        from zoneinfo import ZoneInfo

        display_timezone = ZoneInfo(timezone_name)
    except Exception as exc:
        raise MediaCardError("Schedule timezone is invalid") from exc

    canvas = _background(SCHEDULE_CARD_SIZE, header_accent_y=98).convert("RGBA")
    draw = ImageDraw.Draw(canvas, "RGBA")
    width, height = SCHEDULE_CARD_SIZE
    sorted_matches = sorted(matches, key=lambda item: item.scheduled_at)
    _draw_schedule_header(canvas, draw, sorted_matches, local_now, page_number, page_count)
    columns = 1 if len(sorted_matches) <= 3 else 2
    rows = math.ceil(len(sorted_matches) / columns)
    area_top = 360
    area_bottom = 960
    gap = 14
    available_height = area_bottom - area_top - gap * (rows - 1)
    if columns == 1:
        max_row_height = {1: 390, 2: 280, 3: 195}[len(sorted_matches)]
    else:
        max_row_height = 230
    row_height = min(max_row_height, available_height // rows)
    group_height = row_height * rows + gap * (rows - 1)
    top = area_top + ((area_bottom - area_top) - group_height) // 2
    card_width = 960 if columns == 1 else 468
    column_gap = 24
    left = 60
    for index, match in enumerate(sorted_matches):
        row = index // columns
        column = index % columns
        if columns == 2 and row == rows - 1 and len(sorted_matches) % 2 == 1:
            x0 = (width - card_width) // 2
        else:
            x0 = left + column * (card_width + column_gap)
        y0 = top + row * (row_height + gap)
        box = (x0, y0, x0 + card_width, y0 + row_height)
        if columns == 1:
            _draw_wide_schedule_match(canvas, draw, match, box, display_timezone)
        else:
            _draw_compact_schedule_match(canvas, draw, match, box, display_timezone)

    _centered_text(
        draw,
        width // 2,
        1012,
        "CS2 TIER-1 · РЕЗУЛЬТАТЫ МАТЧЕЙ",
        _font(20, display=True),
        MUTED,
    )
    return _as_png(canvas)


def paginate_schedule_matches(
    matches: Sequence[UpcomingMatchNormalized],
) -> list[list[UpcomingMatchNormalized]]:
    """Split a busy schedule into one or two chronological, balanced pages."""
    if not matches or len(matches) > MAX_SCHEDULE_TOTAL_MATCHES:
        raise MediaCardError("Schedule album supports between one and twenty matches")
    sorted_matches = sorted(matches, key=lambda item: item.scheduled_at)
    if len(sorted_matches) <= MAX_SCHEDULE_MATCHES:
        return [sorted_matches]
    first_page_size = math.ceil(len(sorted_matches) / 2)
    return [sorted_matches[:first_page_size], sorted_matches[first_page_size:]]


def render_schedule_cards(
    matches: Sequence[UpcomingMatchNormalized],
    local_now: datetime,
    timezone_name: str,
) -> list[bytes]:
    """Render one schedule card or a balanced two-card album for 11–20 matches."""
    pages = paginate_schedule_matches(matches)
    page_count = len(pages)
    return [
        render_schedule_card(
            page,
            local_now,
            timezone_name,
            page_number=index,
            page_count=page_count,
        )
        for index, page in enumerate(pages, start=1)
    ]
