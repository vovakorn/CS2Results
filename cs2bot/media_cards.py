"""Deterministic branded images for Telegram match posts."""
from __future__ import annotations

import io
import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Sequence
from urllib.parse import urlparse

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from .match_sources.models import MatchNormalized, UpcomingMatchNormalized

RESULT_CARD_SIZE = (1080, 1080)
SCHEDULE_CARD_SIZE = (1080, 1080)
MAX_SCHEDULE_MATCHES = 10
MAX_LOGO_BYTES = 2_000_000
MAX_LOGO_PIXELS = 4_000_000
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

NAVY = (7, 17, 32)
PANEL = (18, 38, 61)
PANEL_LIGHT = (24, 49, 76)
WHITE = (244, 247, 251)
MUTED = (150, 168, 191)
CYAN = (22, 199, 255)
AMBER = (255, 159, 28)
LOGO_PLATE_DARK = (10, 24, 43)
LOGO_PLATE_LIGHT = (220, 230, 240)

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


def _background(size: tuple[int, int]) -> Image.Image:
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
    for offset, color in ((0, CYAN), (width // 2, AMBER)):
        x0 = 55 + offset
        x1 = min(width - 55, x0 + width // 3)
        draw.line((x0, 58, x1, 58), fill=(*color, 220), width=8)
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
        or not parsed.path.startswith("/images/team/image/")
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


def fetch_team_logo(url: str | None, timeout: int = 5) -> Image.Image | None:
    """Download and validate a PandaScore team logo without following redirects."""
    safe_url = _safe_logo_url(url)
    if not safe_url:
        return None
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

        raw = b"".join(chunks)
        try:
            with Image.open(io.BytesIO(raw)) as probe:
                if probe.width * probe.height > MAX_LOGO_PIXELS:
                    raise MediaCardError("Team logo dimensions are too large")
                probe.verify()
            logo = Image.open(io.BytesIO(raw))
            logo.load()
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            last_error = MediaCardError("Team logo is not a valid image")
            last_error.__cause__ = exc
            continue
        return logo.convert("RGBA")

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
    center_y = (y0 + y1) // 2
    draw.rounded_rectangle(box, radius=30, fill=(*PANEL, 237))
    draw.line((x0 + 28, y0 + 1, x0 + 168, y0 + 1), fill=(*CYAN, 190), width=3)
    draw.line((x1 - 168, y0 + 1, x1 - 28, y0 + 1), fill=(*AMBER, 190), width=3)

    logo_diameter = min(142, max(88, int(height * 0.44)))
    _draw_logo(
        canvas,
        draw,
        (x0 + 88, center_y),
        logo_diameter,
        match.team1_name,
        match.team1_logo_url,
        CYAN,
        match.team1_logo_fallback_url,
    )
    _draw_logo(
        canvas,
        draw,
        (x1 - 88, center_y),
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
        center_y - (76 if height >= 300 else 62),
        _schedule_time(match, display_timezone),
        _font(time_size),
        AMBER,
    )
    team_width = 205
    _centered_text(
        draw,
        x0 + 300,
        center_y + 2,
        match.team1_name.upper(),
        _fit_font(draw, match.team1_name.upper(), team_width, team_size, 20),
        WHITE,
    )
    _centered_text(
        draw,
        x1 - 300,
        center_y + 2,
        match.team2_name.upper(),
        _fit_font(draw, match.team2_name.upper(), team_width, team_size, 20),
        WHITE,
    )
    event = match.tournament_name.upper()
    _centered_text(
        draw,
        (x0 + x1) // 2,
        center_y + (78 if height >= 300 else 62),
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
    center_y = (y0 + y1) // 2
    draw.rounded_rectangle(box, radius=min(26, height // 5), fill=(*PANEL, 237))
    draw.line((x0 + 22, y0 + 1, center_x - 22, y0 + 1), fill=(*CYAN, 185), width=2)
    draw.line((center_x + 22, y0 + 1, x1 - 22, y0 + 1), fill=(*AMBER, 185), width=2)

    if height >= 210:
        logo_diameter, time_size, team_size, event_size = 68, 42, 24, 15
        time_y, logo_y, team_y, event_y = y0 + 19, y0 + 98, y0 + 140, y1 - 46
    elif height >= 165:
        logo_diameter, time_size, team_size, event_size = 52, 34, 20, 13
        time_y, logo_y, team_y, event_y = y0 + 12, y0 + 70, y0 + 104, y1 - 34
    else:
        logo_diameter, time_size, team_size, event_size = 42, 28, 17, 11
        time_y, logo_y, team_y, event_y = y0 + 7, y0 + 53, y0 + 78, y1 - 27

    team1_x = x0 + int((x1 - x0) * 0.27)
    team2_x = x1 - int((x1 - x0) * 0.27)
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
    name_width = 166 if height >= 210 else 154 if height >= 165 else 146
    team1 = match.team1_name.upper()
    team2 = match.team2_name.upper()
    _centered_text(
        draw,
        team1_x,
        team_y,
        team1,
        _fit_font(draw, team1, name_width, team_size, 13),
        WHITE,
    )
    _centered_text(
        draw,
        team2_x,
        team_y,
        team2,
        _fit_font(draw, team2, name_width, team_size, 13),
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


def _as_png(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.convert("RGB").save(output, format="PNG", optimize=True)
    data = output.getvalue()
    if not data:
        raise MediaCardError("Rendered media card is empty")
    return data


def render_result_card(match: MatchNormalized) -> bytes:
    """Render a square result card whose score is protected by Telegram media spoiler."""
    canvas = _background(RESULT_CARD_SIZE).convert("RGBA")
    draw = ImageDraw.Draw(canvas, "RGBA")
    width, height = RESULT_CARD_SIZE

    _centered_text(draw, width // 2, 92, "РЕЗУЛЬТАТ МАТЧА", _font(42, display=True), CYAN)
    tournament_font = _fit_font(
        draw,
        match.tournament_name.upper(),
        900,
        38,
        24,
        display=True,
    )
    _centered_text(
        draw,
        width // 2,
        160,
        match.tournament_name.upper(),
        tournament_font,
        WHITE,
    )

    draw.rounded_rectangle((90, 255, 990, 825), radius=36, fill=(*PANEL, 232))
    _draw_logo(
        canvas,
        draw,
        (285, 455),
        250,
        match.team1_name,
        match.team1_logo_url,
        CYAN,
        match.team1_logo_fallback_url,
    )
    _draw_logo(
        canvas,
        draw,
        (795, 455),
        250,
        match.team2_name,
        match.team2_logo_url,
        AMBER,
        match.team2_logo_fallback_url,
    )

    score = f"{match.score1}:{match.score2}"
    score_font = _fit_font(draw, score, 270, 144, 96)
    _centered_text(draw, width // 2, 375, score, score_font, WHITE)
    _centered_text(draw, width // 2, 535, "BO" + str(match.best_of or "—"), _font(34), MUTED)

    for center_x, name in ((285, match.team1_name), (795, match.team2_name)):
        team_font = _fit_font(draw, name.upper(), 350, 48, 26)
        _centered_text(draw, center_x, 625, name.upper(), team_font, WHITE)

    winner = (
        match.team1_name
        if match.score1 is not None and match.score2 is not None and match.score1 > match.score2
        else match.team2_name
    )
    _centered_text(draw, width // 2, 750, f"ПОБЕДИТЕЛЬ · {winner.upper()}", _font(32, display=True), AMBER)
    _centered_text(
        draw,
        width // 2,
        932,
        "CS2 TIER-1 · РЕЗУЛЬТАТЫ МАТЧЕЙ",
        _font(24, display=True),
        MUTED,
    )
    return _as_png(canvas)


def render_schedule_card(
    matches: Sequence[UpcomingMatchNormalized],
    local_now: datetime,
    timezone_name: str,
) -> bytes:
    """Render a square daily schedule with an adaptive one- or two-column grid."""
    if not matches or len(matches) > MAX_SCHEDULE_MATCHES:
        raise MediaCardError("Schedule card supports between one and ten matches")
    try:
        from zoneinfo import ZoneInfo

        display_timezone = ZoneInfo(timezone_name)
    except Exception as exc:
        raise MediaCardError("Schedule timezone is invalid") from exc

    canvas = _background(SCHEDULE_CARD_SIZE).convert("RGBA")
    draw = ImageDraw.Draw(canvas, "RGBA")
    width, height = SCHEDULE_CARD_SIZE
    month_names = (
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
    _draw_channel_logo(canvas, draw, (105, 118), 88)
    header_center = 615
    _centered_text(draw, header_center, 75, "МАТЧИ CS2 СЕГОДНЯ", _font(44, display=True), WHITE)
    _centered_text(
        draw,
        header_center,
        138,
        f"{local_now.day} {month_names[local_now.month]}",
        _font(27, display=True),
        CYAN,
    )

    sorted_matches = sorted(matches, key=lambda item: item.scheduled_at)
    columns = 1 if len(sorted_matches) <= 3 else 2
    rows = math.ceil(len(sorted_matches) / columns)
    area_top = 222
    area_bottom = 960
    gap = 14
    available_height = area_bottom - area_top - gap * (rows - 1)
    if columns == 1:
        max_row_height = {1: 330, 2: 280, 3: 235}[len(sorted_matches)]
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
