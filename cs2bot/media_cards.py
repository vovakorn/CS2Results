"""Deterministic branded images for Telegram match posts."""
from __future__ import annotations

import io
from collections import OrderedDict
import hashlib
import logging
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence, TypeVar
from urllib.parse import urlparse

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, UnidentifiedImageError

from .match_sources.models import (
    MatchNormalized,
    RadarBracketMatch,
    RadarBracketNode,
    TournamentPlacement,
    TournamentVRSImpact,
    TournamentRadar,
    UpcomingMatchNormalized,
)
from .match_sources.storage import read_cached_logo, write_cached_logo

RESULT_CARD_SIZE = (1080, 1080)
SCHEDULE_CARD_SIZE = (1080, 1080)
SCHEDULE_CONTEXT_COVER_SIZE = (1080, 1080)
MAX_RESULT_MATCHES = 10
MAX_RESULT_MATCHES_PER_CARD = 4
MAX_MEDIA_ALBUM_PAGES = 10
MAX_SCHEDULE_MATCHES = 4
MAX_SCHEDULE_TOTAL_MATCHES = 20
MAX_TOURNAMENT_STANDINGS = 64
TOURNAMENT_STANDINGS_PER_CARD = 8
TOURNAMENT_VRS_PER_CARD = 8
MAX_RADAR_BRACKET_NODES_PER_CARD = 6
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
_TMatch = TypeVar("_TMatch")

NAVY = (7, 17, 32)
PANEL = (18, 38, 61)
PANEL_LIGHT = (24, 49, 76)
WHITE = (244, 247, 251)
MUTED = (150, 168, 191)
CYAN = (22, 199, 255)
AMBER = (255, 159, 28)
GOLD_FOIL = (205, 164, 71)
GOLD_FOIL_HIGHLIGHT = (255, 220, 126)
GOLD_FOIL_SHADOW = (126, 85, 24)
# Final-result cards use the brighter champion-gold from the approved template.
# Keep it separate from standings so their podium hierarchy stays restrained.
FINAL_GOLD = (255, 202, 46)
FINAL_GOLD_HIGHLIGHT = (255, 235, 140)
FINAL_GOLD_SHADOW = (166, 105, 18)
VRS_UP = (73, 210, 126)
VRS_DOWN = (245, 91, 91)
STANDINGS_GOLD = (214, 181, 104)
STANDINGS_SILVER = (190, 202, 214)
STANDINGS_BRONZE = (215, 158, 115)
STANDINGS_METAL_PALETTES = {
    STANDINGS_GOLD: ((156, 111, 44), (222, 188, 107), (255, 237, 174)),
    STANDINGS_SILVER: ((129, 148, 165), (199, 211, 222), (250, 253, 255)),
    STANDINGS_BRONZE: ((135, 83, 51), (193, 139, 96), (244, 194, 143)),
}
STANDINGS_MEDAL_TEXT = (20, 31, 43)
STANDINGS_HEADER = (28, 55, 83)
STANDINGS_HEADER_LINE = (94, 137, 174)
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
SCHEDULE_CONTEXT_BACKGROUND = ASSET_DIR / "schedule-context-bg.png"
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


def _background(
    size: tuple[int, int],
    *,
    header_accent_y: int = 90,
    header_accent_colors: tuple[tuple[int, int, int], tuple[int, int, int]] = (CYAN, AMBER),
    header_foil: bool = False,
) -> Image.Image:
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
    for (x0, x1), color in zip(accent_segments, header_accent_colors, strict=True):
        draw.line((x0, header_accent_y, x1, header_accent_y), fill=(*color, 220), width=8)
        if header_foil:
            draw.line(
                (x0, header_accent_y - 2, x1, header_accent_y - 2),
                fill=(*GOLD_FOIL_HIGHLIGHT, 210),
                width=2,
            )
            draw.line(
                (x0, header_accent_y + 3, x1, header_accent_y + 3),
                fill=(*GOLD_FOIL_SHADOW, 190),
                width=2,
            )
    for index in range(7):
        x = -180 + index * 225
        draw.line((x, height, x + 520, 0), fill=(39, 75, 119, 25), width=2)
    return image


def _fit_font(draw, text, max_width, max_size, min_size, *, display=False, medium=False):
    # The font carries a width bound so drawing remains safe at the size floor.
    for size in range(max(max_size, min_size), min_size - 1, -1):
        font = _font(size, display=display, medium=medium)
        font._cs2_max_width = max_width
        box = draw.textbbox((0, 0), text, font=font)
        if box[2] - box[0] <= max_width:
            return font
    return font



def _bounded_text(draw, text, font, width=None):
    """Shorten at a readable size while leaving the source model unchanged."""
    width = width if width is not None else getattr(font, "_cs2_max_width", None)
    if width is None:
        return text
    def fits(value):
        b = draw.textbbox((0, 0), value, font=font)
        return b[2] - b[0] <= width
    if fits(text):
        return text
    shortened = text.rstrip()
    while shortened and not fits(shortened + "…"):
        shortened = shortened[:-1].rstrip()
    return shortened + "…" if fits(shortened + "…") else ""


def _wrap_text(draw, text, font, width, max_lines=2):
    words = text.split()
    lines = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        b = draw.textbbox((0, 0), candidate, font=font)
        if b[2] - b[0] <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = ""
        # A token can itself exceed the box; wrap it without dropping letters.
        while word:
            b = draw.textbbox((0, 0), word, font=font)
            if b[2] - b[0] <= width:
                current = word
                break
            cut = len(word) - 1
            while cut > 0:
                b = draw.textbbox((0, 0), word[:cut], font=font)
                if b[2] - b[0] <= width:
                    break
                cut -= 1
            if not cut:
                break
            lines.append(word[:cut])
            word = word[cut:]
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        tail = " ".join(lines[max_lines - 1:])
        lines = lines[:max_lines - 1] + [_bounded_text(draw, tail, font, width)]
    return lines or [""]


def _draw_text_block(draw, anchor_x, center_y, text, width, size=38, *, display=False,
                     medium=False, fill=WHITE, alignment="center", max_lines=2, gap=5, min_size=None,
                     foil_canvas=None):
    font = _font(size, display=display, medium=medium)
    if min_size is not None:
        while size > min_size and len(_wrap_text(draw, text, font, width, 10000)) > max_lines:
            size -= 1
            font = _font(size, display=display, medium=medium)
    lines = _wrap_text(draw, text, font, width, max_lines)
    boxes = [draw.textbbox((0, 0), line, font=font) for line in lines]
    heights = [b[3] - b[1] for b in boxes]
    y = center_y - (sum(heights) + gap * (len(lines) - 1)) / 2
    for line, box, height in zip(lines, boxes, heights):
        if alignment == "center":
            x = anchor_x - (box[0] + box[2]) / 2
        elif alignment == "right":
            x = anchor_x - box[2]
        else:
            x = anchor_x - box[0]
        if foil_canvas is None:
            draw.text((x, y - box[1]), line, font=font, fill=fill)
        else:
            _foil_text(foil_canvas, x, y - box[1], line, font)
        y += height + gap


def _uses_cyrillic(text):
    return bool(re.search(r"[А-Яа-яЁё]", text))


def _draw_channel_brand(canvas, draw, *, center_y=90, logo_diameter=88,
                        label_center_y=156, label_size=38):
    """Keep the channel emblem and its readable name on one central axis."""
    center_x = canvas.width // 2
    _draw_channel_logo(canvas, draw, (center_x, center_y), logo_diameter)
    _draw_text_block(draw, center_x, label_center_y, "CS2 RESULTS",
                     canvas.width - 120, label_size, max_lines=1)


def _card_header(canvas, draw, title, tournament, meta="", tournament_logo_url=None,
                 *, title_fill=WHITE, tournament_fill=CYAN, foil=False):
    _draw_channel_brand(canvas, draw)
    _draw_text_block(draw, 540, 204, title, 960, 40, display=True,
                     fill=title_fill, max_lines=1, foil_canvas=canvas if foil else None)
    name = tournament.upper()
    display = _uses_cyrillic(name)
    size = 30 if display else 38
    center_x, width = 540, 880
    if tournament_logo_url:
        font = _font(size, display=display)
        boxes = [draw.textbbox((0, 0), line, font=font)
                 for line in _wrap_text(draw, name, font, 800)]
        text_width = max(box[2] - box[0] for box in boxes)
        group_width = 54 + 16 + text_width
        logo_x = 540 - group_width / 2 + 27
        if _draw_tournament_logo(canvas, draw, (round(logo_x), 255), 54, tournament_logo_url):
            center_x = logo_x + 43 + text_width / 2
            width = 800
    _draw_text_block(draw, center_x, 255, name, width, size,
                     display=display, fill=tournament_fill, foil_canvas=canvas if foil else None)
    if meta:
        _draw_text_block(draw, 540, 304, meta, 960, 24,
                         display=True, fill=MUTED, max_lines=1)


def _card_footer(draw, source="PANDASCORE", page_number=1, page_count=1, *, accent=CYAN):
    _draw_text_block(draw, 60, 1018, f"ИСТОЧНИК: {source.upper()}", 730, 22,
                     display=True, fill=MUTED, alignment="left", max_lines=1)
    right = f"{page_number}/{page_count}" if page_count > 1 else "@CS2_RESULTS"
    _aligned_text(draw, 1020, 1002, right, _font(28), accent, "right")


def _shared_fixture_format(matches):
    """Share a page format only when every fixture has the same known value."""
    formats = {match.best_of for match in matches}
    if len(formats) == 1 and (best_of := next(iter(formats))):
        return f"BO{best_of}"
    return ""


def _draw_fixture_row(canvas, draw, match, box, *, time_label=None, event=None, show_format=True):
    x0, y0, x1, y1 = box
    draw.rounded_rectangle(box, radius=18, fill=(*PANEL, 245))
    draw.line((x0, y0 + 18, x0, y1 - 18), fill=(*CYAN, 180), width=3)
    center_y = y0 + (y1 - y0) * .43
    for left, center_x, name, url, fallback in (
        (True, x0 + 52, match.team1_name, match.team1_logo_url, match.team1_logo_fallback_url),
        (False, x1 - 52, match.team2_name, match.team2_logo_url, match.team2_logo_fallback_url),
    ):
        _draw_logo(canvas, draw, (center_x, round(center_y)), 70, name, url,
                   CYAN if left else AMBER, fallback, subtle=True)
        winner = _winner_side(match) if time_label is None else None
        _draw_text_block(draw, x0 + 111 if left else x1 - 111, center_y,
                         name.upper(), 300, 38, min_size=36, alignment="left" if left else "right",
                         fill=AMBER if winner == ("left" if left else "right") else WHITE)
    value = time_label if time_label is not None else f"{match.score1 if match.score1 is not None else '—'}:{match.score2 if match.score2 is not None else '—'}"
    _centered_text_on_point(draw, (x0 + x1) // 2, round(center_y), value,
                            _font(64 if time_label is not None else 76), AMBER if time_label is not None else WHITE)
    match_format = f"BO{match.best_of}" if show_format and match.best_of else ""
    # Keep the format visible even when a long event label is shortened.
    label = " · ".join(part for part in (match_format, event) if part)
    if label:
        _draw_text_block(draw, (x0 + x1) // 2, y1 - 22, label.upper(), x1 - x0 - 70,
                         23 if _uses_cyrillic(label) else 28, display=_uses_cyrillic(label),
                         fill=MUTED, max_lines=1)


def _draw_fixture_hero(canvas, draw, match, box, *, time_label=None, event=None,
                       value_label=None, show_format=True, prominent=False, logo_deadline=None,
                       subtle=False):
    x0, y0, x1, y1 = box
    draw.rounded_rectangle(box, radius=24, fill=(*PANEL, 240))
    center_x = (x0 + x1) // 2
    logo_y = y0 + 135
    diameter = 200 if prominent else 182
    winner = _winner_side(match) if time_label is None and value_label is None else None
    for left, x, name, url, fallback in (
        (True, x0 + 180, match.team1_name, match.team1_logo_url, match.team1_logo_fallback_url),
        (False, x1 - 180, match.team2_name, match.team2_logo_url, match.team2_logo_fallback_url),
    ):
        remaining = logo_deadline - time.monotonic() if logo_deadline is not None else None
        if remaining is not None and remaining < .1:
            url = fallback = None
        _draw_logo(canvas, draw, (x, logo_y), diameter, name, url, CYAN if left else AMBER, fallback,
                   subtle=subtle, download_timeout=min(.75, remaining) if remaining is not None and remaining >= .1 else None)
        text, size, name_width = name.upper(), 66 if prominent else 60, 360
        while size > 48 and len(_wrap_text(draw, text, _font(size), name_width, 10000)) > 2:
            size -= 1
        font = _font(size)
        max_lines = 3 if len(_wrap_text(draw, text, font, name_width, 10000)) > 2 else 2
        lines = _wrap_text(draw, text, font, name_width, max_lines)
        boxes = [draw.textbbox((0, 0), line, font=font) for line in lines]
        text_height = sum(b[3] - b[1] for b in boxes) + 6 * (len(lines) - 1)
        # Keep the first line 23 px below the logo, including for a three-line name.
        name_center_y = logo_y + diameter / 2 + 23 + text_height / 2
        _draw_text_block(draw, x, name_center_y, text, name_width, size, min_size=48,
                         max_lines=max_lines, gap=6,
                         fill=AMBER if winner == ("left" if left else "right") else WHITE)
    value = value_label if value_label is not None else time_label if time_label is not None else f"{match.score1 if match.score1 is not None else '—'}:{match.score2 if match.score2 is not None else '—'}"
    _centered_text_on_point(draw, center_x, logo_y, value, _font(72 if value_label is not None else 104 if time_label is not None else 176 if prominent else 164),
                            AMBER if time_label is not None else WHITE)
    if show_format and getattr(match, "best_of", None):
        _centered_text_on_point(draw, center_x, logo_y + 92, f"BO{match.best_of}", _font(32), MUTED)
    if event:
        _draw_text_block(draw, center_x, y1 - 20, event.upper(), 860, 26,
                         display=_uses_cyrillic(event), fill=MUTED, max_lines=1)


def _fixture_page_boxes(count):
    if count == 1:
        return [(60, 425, 1020, 825)]
    height = min(236, (640 - 12 * (count - 1)) // count)
    top = 326 + (640 - (height * count + 12 * (count - 1))) // 2
    return [(60, top + i * (height + 12), 1020, top + i * (height + 12) + height) for i in range(count)]


def _fixture_timestamp(raw_date):
    if not raw_date:
        return float("-inf")
    try:
        value = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MediaCardError("Fixture contains an invalid date") from exc
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp()


def _paginate_fixture_groups(matches, key, total_limit, date_key):
    if not 1 <= len(matches) <= total_limit:
        raise MediaCardError(f"Fixture album supports one to {total_limit} matches")
    ordered = sorted(matches, key=lambda item: _fixture_timestamp(date_key(item)))
    pages = []
    for item in ordered:
        if not pages or len(pages[-1]) == 4 or key(pages[-1][-1]) != key(item):
            pages.append([])
        pages[-1].append(item)
    if len(pages) > MAX_MEDIA_ALBUM_PAGES:
        # With many interleaved events, labels on each row preserve ownership
        # while chronological packing keeps the entire album within ten pages.
        pages = [ordered[i:i + 4] for i in range(0, len(ordered), 4)]
    return pages


def _centered_text(draw, center_x, y, text, font, fill):
    text = _bounded_text(draw, text, font)
    box = draw.textbbox((0, 0), text, font=font)
    draw.text((center_x - (box[0] + box[2]) / 2, y), text, font=font, fill=fill)


def _centered_text_on_point(draw, center_x, center_y, text, font, fill):
    text = _bounded_text(draw, text, font)
    box = draw.textbbox((0, 0), text, font=font)
    draw.text((center_x - (box[0] + box[2]) / 2, center_y - (box[1] + box[3]) / 2), text, font=font, fill=fill)


def _aligned_text(draw, edge_x, y, text, font, fill, alignment):
    text = _bounded_text(draw, text, font)
    box = draw.textbbox((0, 0), text, font=font)
    if alignment == "left":
        x = edge_x - box[0]
    elif alignment == "right":
        x = edge_x - box[2]
    else:
        raise ValueError("alignment must be left or right")
    draw.text((x, y), text, font=font, fill=fill)


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
    *,
    subtle: bool = False,
) -> None:
    x, y = center
    radius = diameter // 2
    line_width = 1 if subtle else max(3, diameter // 45)
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
    if subtle:
        return
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
    *,
    content_scale: float = 0.64,
    download_timeout: float | None = None,
    subtle: bool = False,
) -> None:
    x, y = center
    logo = None
    failures: list[str] = []
    logo_urls = list(dict.fromkeys(url for url in (logo_url, fallback_logo_url) if url))
    for candidate_url in logo_urls:
        try:
            logo = (
                fetch_team_logo(candidate_url, timeout=download_timeout)
                if download_timeout is not None else fetch_team_logo(candidate_url)
            )
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
        _draw_logo_plate(draw, center, diameter, accent, _logo_plate_fill(logo), subtle=subtle)
        contained = ImageOps.contain(
            logo,
            (int(diameter * content_scale), int(diameter * content_scale)),
        )
        canvas.alpha_composite(contained, (x - contained.width // 2, y - contained.height // 2))
        return
    _draw_logo_plate(draw, center, diameter, accent, LOGO_PLATE_DARK, subtle=subtle)
    initials_font = _fit_font(
        draw,
        _initials(team_name),
        int(diameter * 0.65),
        int(diameter * 0.34),
        max(12, min(22, int(diameter * 0.34))),
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
) -> bool:
    """Draw an official event mark when PandaScore provides one; never invent it."""
    if not logo_url:
        return False
    try:
        logo = fetch_team_logo(logo_url)
    except MediaCardError as exc:
        logger.warning(
            "tournament_logo_unavailable host=%s error=%s",
            urlparse(logo_url).hostname or "missing",
            exc,
        )
        return False
    if logo is None:
        return False
    _draw_logo_plate(draw, center, diameter, CYAN, _logo_plate_fill(logo))
    contained = ImageOps.contain(logo, (int(diameter * 0.64), int(diameter * 0.64)))
    canvas.alpha_composite(
        contained,
        (center[0] - contained.width // 2, center[1] - contained.height // 2),
    )
    return True


def _schedule_tournament_header(
    matches: Sequence[UpcomingMatchNormalized],
) -> tuple[str, str | None]:
    """Return the shared competition label and its official logo for one card."""
    labels: dict[str, tuple[str, str | None]] = {}
    for match in matches:
        # competition_key can be only a city or season label (for example,
        # "Porto"), so keep the full event name and strip only its group.
        parts = [part.strip() for part in match.tournament_name.split(" — ") if part.strip()]
        label = (
            " — ".join(parts[:-1])
            if len(parts) > 1
            and re.fullmatch(r"(?:group|группа)\s+[\w\d]+", parts[-1], re.IGNORECASE)
            else match.tournament_name
        )
        if match.competition_key and match.competition_key.casefold() not in label.casefold():
            label = match.competition_key
        key = label.casefold()
        if key not in labels or (not labels[key][1] and match.tournament_logo_url):
            labels[key] = (label, match.tournament_logo_url)
    if len(labels) == 1:
        return next(iter(labels.values()))
    return "ТУРНИРЫ ДНЯ", None


def _schedule_tournament_key(match: UpcomingMatchNormalized) -> str:
    """Group schedule fixtures under the event that owns their logo."""
    return (match.competition_key or match.tournament_name).casefold()


def _schedule_match_event_label(match: UpcomingMatchNormalized) -> str:
    """Use the same event label in every schedule-card hierarchy level."""
    return match.competition_key or match.tournament_name


def _draw_schedule_header(canvas, draw, matches, local_now, page_number, page_count):
    event, logo_url = _schedule_tournament_header(matches)
    date = f"{local_now.day} {MONTH_NAMES[local_now.month]}"
    if shared_format := _shared_fixture_format(matches):
        date += f" · {shared_format}"
    _card_header(canvas, draw, "МАТЧИ CS2 СЕГОДНЯ", event, date, logo_url)


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


def _draw_wide_schedule_match(canvas, draw, match, box, display_timezone, *, show_format=True):
    _draw_fixture_hero(canvas, draw, match, box, time_label=_schedule_time(match, display_timezone),
                       show_format=show_format, subtle=True)


def _draw_compact_schedule_match(canvas, draw, match, box, display_timezone, *, show_format=True):
    _draw_fixture_row(canvas, draw, match, box, time_label=_schedule_time(match, display_timezone),
                      show_format=show_format)


def _radar_standing_entries(radar: TournamentRadar) -> list[tuple[int, str, str | None]]:
    if radar.standing_teams:
        return [(team.rank, team.name, team.logo_url) for team in radar.standing_teams[:4]]
    entries: list[tuple[int, str, str | None]] = []
    for line in radar.standings[:4]:
        rank, separator, name = line.partition(".")
        if separator and rank.strip().isdigit() and name.strip():
            entries.append((int(rank.strip()), name.strip(), None))
    return entries


def _radar_header(canvas, draw, tournament_name, subtitle):
    _card_header(canvas, draw, "ТУРНИРНЫЙ РАДАР", tournament_name, subtitle.upper())


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


def _radar_has_only_confirmed_pairs(matches: Sequence[RadarBracketNode]) -> bool:
    return bool(matches) and all(
        match.team1_name and match.team2_name and not match.previous_match_ids
        for match in matches
    )


def _radar_bracket_content_label(matches: Sequence[RadarBracketNode]) -> str:
    """Describe what the card shows without assuming a tournament stage."""
    if _radar_has_only_confirmed_pairs(matches):
        return "ПОДТВЕРЖДЁННАЯ ПАРА" if len(matches) == 1 else "ПОДТВЕРЖДЁННЫЕ ПАРЫ"
    return "СЕТКА ТУРНИРА"


def _radar_bracket_section_label(matches: Sequence[RadarBracketNode]) -> str:
    """Name a bracket side only when PandaScore's round labels agree."""
    labels = " ".join(match.round_name or "" for match in matches).casefold()
    has_upper = "upper" in labels or "верхн" in labels
    has_lower = "lower" in labels or "нижн" in labels
    if has_upper and not has_lower:
        return "ВЕРХНЯЯ СЕТКА"
    if has_lower and not has_upper:
        return "НИЖНЯЯ СЕТКА"
    if has_upper and has_lower:
        return "ВЕРХНЯЯ И НИЖНЯЯ СЕТКИ"
    return _radar_bracket_content_label(matches)


def _radar_round_label(matches: Sequence[RadarBracketNode]) -> str:
    rounds = list(dict.fromkeys(match.round_name.strip() for match in matches if match.round_name))
    if len(rounds) != 1:
        return "РАУНД СЕТКИ"
    translations = {
        "opening round": "ОТКРЫВАЮЩИЙ РАУНД",
        "upper semi-finals": "ВЕРХНИЕ ПОЛУФИНАЛЫ",
        "upper semifinals": "ВЕРХНИЕ ПОЛУФИНАЛЫ",
        "upper final": "ВЕРХНИЙ ФИНАЛ",
        "lower round 1": "НИЖНИЙ РАУНД 1",
        "lower semi-finals": "НИЖНИЕ ПОЛУФИНАЛЫ",
        "lower semifinals": "НИЖНИЕ ПОЛУФИНАЛЫ",
        "lower final": "НИЖНИЙ ФИНАЛ",
        "semifinal": "ПОЛУФИНАЛ",
        "semi-final": "ПОЛУФИНАЛ",
        "quarterfinal": "ЧЕТВЕРТЬФИНАЛ",
        "quarter-final": "ЧЕТВЕРТЬФИНАЛ",
        "final": "ФИНАЛ",
    }
    round_name = rounds[0]
    return translations.get(round_name.casefold(), round_name.upper())


def _radar_bracket_columns(
    matches: Sequence[RadarBracketNode],
) -> tuple[list[list[RadarBracketNode]], dict[str, int]]:
    """Lay explicit predecessor links out from left to right like a bracket."""
    by_id = {match.match_id: match for match in matches}
    predecessors = {
        match.match_id: [match_id for match_id in match.previous_match_ids if match_id in by_id]
        for match in matches
    }
    if not any(predecessors.values()):
        split_at = math.ceil(len(matches) / 2)
        columns = [list(matches[:split_at]), list(matches[split_at:])]
        return [column for column in columns if column], {
            match.match_id: 0 if index < split_at else 1
            for index, match in enumerate(matches)
        }

    depth_cache: dict[str, int] = {}

    def depth(match_id: str, trail: set[str]) -> int:
        if match_id in depth_cache:
            return depth_cache[match_id]
        if match_id in trail:
            return 0
        parents = predecessors[match_id]
        value = 0 if not parents else 1 + max(depth(parent, trail | {match_id}) for parent in parents)
        depth_cache[match_id] = value
        return value

    columns_by_depth: dict[int, list[RadarBracketNode]] = {}
    for match in matches:
        columns_by_depth.setdefault(depth(match.match_id, set()), []).append(match)
    ordered_depths = sorted(columns_by_depth)
    return [columns_by_depth[value] for value in ordered_depths], {
        match.match_id: ordered_depths.index(depth_cache[match.match_id])
        for match in matches
    }


def _draw_radar_bracket_match(canvas, draw, match, box, *, reference=""):
    x0, y0, x1, y1 = box
    draw.rounded_rectangle(box, radius=16, fill=(*PANEL, 245))
    _draw_text_block(draw, x0 + 16, y0 + 25, _radar_round_label([match]), x1 - x0 - 32,
                     24, display=True, alignment="left", fill=MUTED, max_lines=1)
    row_height = (y1 - y0 - 76) / 2
    for index, name, url, fallback in (
        (0, match.team1_name, match.team1_logo_url, match.team1_logo_fallback_url),
        (1, match.team2_name, match.team2_logo_url, match.team2_logo_fallback_url),
    ):
        y = y0 + 44 + row_height * (index + .5)
        _draw_logo(canvas, draw, (x0 + 29, round(y)), 38, name or "?", url,
                   CYAN if index == 0 else AMBER, fallback)
        _draw_text_block(draw, x0 + 62, y, (name or "TBD").upper(), x1 - x0 - 78,
                         38, min_size=36, alignment="left")
    if match.team1_name and match.team2_name:
        reference = ""
    labels = [reference] if reference else []
    if (match.status or "").casefold() == "running":
        labels.append("LIVE")
    if labels:
        _draw_text_block(draw, x0 + 16, y1 - 17, " · ".join(labels), x1 - x0 - 32, 22,
                         display=True, alignment="left", fill=CYAN, max_lines=1)


def _draw_radar_single_pair(canvas, draw, match):
    _draw_fixture_hero(canvas, draw, match, _fixture_page_boxes(1)[0], value_label="VS")
    _draw_text_block(draw, 540, 448, _radar_round_label([match]) if match.round_name else "МАТЧ ТУРНИРА",
                     860, 24, display=True, fill=MUTED, max_lines=1)
    if (match.status or "").casefold() == "running":
        _draw_text_block(draw, 540, 806, "LIVE", 860, 22, display=True, fill=CYAN, max_lines=1)


def _draw_radar_bracket(canvas, draw, matches, page_number, page_count, *, references=None):
    if not matches:
        _draw_text_block(draw, 540, 600, "СЕТКА ТУРНИРА ПОКА НЕ ОПУБЛИКОВАНА", 860, 32, display=True)
        return
    if len(matches) == 1 and _radar_has_only_confirmed_pairs(matches):
        _draw_radar_single_pair(canvas, draw, matches[0])
        return
    columns, _ = _radar_bracket_columns(matches)
    list_layout = len(columns) > 2 or any(len(column) > 3 for column in columns)
    if list_layout:
        # A list of numbered slots keeps text readable for deep or uneven graphs.
        split = math.ceil(len(matches) / 2)
        columns = [list(matches[:split]), list(matches[split:])]
        columns = [column for column in columns if column]
    columns_count = len(columns)
    width = (960 - 24 * (columns_count - 1)) // columns_count
    slots = {}
    for col, members in enumerate(columns):
        height = min(280, (638 - 12 * (len(members) - 1)) // len(members))
        top = 326 + (638 - (height * len(members) + 12 * (len(members) - 1))) // 2
        x0 = 60 + col * (width + 24)
        for i, node in enumerate(members):
            y0 = top + i * (height + 12)
            slots[node.match_id] = (x0, y0, x0 + width, y0 + height)
    # Connectors clarify a compact bracket. In the numbered list, predecessor
    # references carry the links without suggesting a bracket between columns.
    if not list_layout:
        for node in matches:
            target = slots[node.match_id]
            for parent_id in node.previous_match_ids:
                parent = slots.get(parent_id)
                if parent is not None and parent[0] < target[0]:
                    sx, sy = parent[2], (parent[1] + parent[3]) // 2
                    ex, ey = target[0], (target[1] + target[3]) // 2
                    mid = (sx + ex) // 2
                    draw.line((sx, sy, mid, sy, mid, ey, ex, ey), fill=(92, 135, 172, 200), width=2)
    for node in matches:
        _draw_radar_bracket_match(canvas, draw, node, slots[node.match_id],
                                 reference=(references or {}).get(node.match_id, ""))


def _draw_radar_next_match(canvas, draw, radar, timezone_name):
    if not radar.next_matches:
        _draw_text_block(draw, 540, 600, "ПОДТВЕРЖДЁННЫЕ ПАРЫ ПОКА НЕ ОПУБЛИКОВАНЫ", 860, 32, display=True)
        return
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(timezone_name)
    except Exception as exc:
        raise MediaCardError("Radar timezone is invalid") from exc
    match = radar.next_matches[0]
    _draw_fixture_hero(canvas, draw, match, _fixture_page_boxes(1)[0], time_label=_schedule_time(match, tz))


def _radar_bracket_nodes(radar: TournamentRadar) -> Sequence[RadarBracketNode]:
    """Prefer the complete source structure; retain legacy confirmed-only payloads."""
    return radar.bracket_structure or radar.bracket_matches



def _paginate_radar_bracket_nodes(nodes):
    if len({node.match_id for node in nodes}) != len(nodes):
        raise MediaCardError("Radar contains duplicate slot IDs")
    by_id = {node.match_id: node for node in nodes}
    ordered, done, visiting = [], set(), set()
    def visit(node):
        if node.match_id in done:
            return
        if node.match_id in visiting:
            raise MediaCardError("Radar contains a cyclic predecessor link")
        visiting.add(node.match_id)
        for parent_id in node.previous_match_ids:
            if parent_id in by_id:
                visit(by_id[parent_id])
        visiting.remove(node.match_id)
        done.add(node.match_id)
        ordered.append(node)
    for node in nodes:
        visit(node)
    # Stable depth order places independent openings before their next round.
    depth = {}
    for node in ordered:
        depth[node.match_id] = max((depth[p] + 1 for p in node.previous_match_ids if p in depth), default=0)
    ordered.sort(key=lambda node: depth[node.match_id])
    pages = [ordered[i:i + MAX_RADAR_BRACKET_NODES_PER_CARD]
             for i in range(0, len(ordered), MAX_RADAR_BRACKET_NODES_PER_CARD)]
    if len(pages) > MAX_MEDIA_ALBUM_PAGES:
        raise MediaCardError("Radar exceeds the media album limit")
    number = {node.match_id: i for i, node in enumerate(ordered, 1)}
    page = {node.match_id: i for i, members in enumerate(pages, 1) for node in members}
    references = {}
    for node in ordered:
        parents = []
        for parent_id in node.previous_match_ids:
            if parent_id not in number:
                parents.append(f"{parent_id} (ВНЕ СНИМКА)")
            else:
                suffix = f" (С.{page[parent_id]})" if page[parent_id] != page[node.match_id] else ""
                parents.append(f"{number[parent_id]:02d}{suffix}")
        references[node.match_id] = f"МАТЧ {number[node.match_id]:02d}" + (" · ИЗ " + " / ".join(parents) if parents else "")
    return pages, references


def _radar_facts(radar: TournamentRadar, matches: Sequence[RadarBracketNode]) -> str:
    match_count = _format_match_count(radar.bracket_match_count)
    if not _radar_has_only_confirmed_pairs(matches):
        match_count += " В СЕТКЕ"
    return f"{radar.roster_team_count} УЧАСТНИКОВ   ·   {match_count}"


def render_tournament_radar_card(radar, tournament_name, timezone_name, variant="auto"):
    cards = render_tournament_radar_cards(radar, tournament_name, timezone_name, variant)
    if len(cards) != 1:
        raise MediaCardError("Radar needs several pages; use render_tournament_radar_cards")
    return cards[0]


def render_tournament_radar_cards(radar, tournament_name, timezone_name, variant="auto"):
    if variant not in {"auto", "bracket", "next_match"}:
        raise MediaCardError("Unsupported radar card variant")
    nodes = list(_radar_bracket_nodes(radar))
    bracket = variant == "bracket" or (variant == "auto" and bool(nodes))
    pages, refs = _paginate_radar_bracket_nodes(nodes) if bracket else ([[]], {})
    pages = pages or [[]]
    rendered = []
    for i, page in enumerate(pages, 1):
        canvas = _background(SCHEDULE_CARD_SIZE, header_accent_y=90).convert("RGBA")
        draw = ImageDraw.Draw(canvas, "RGBA")
        label = _radar_bracket_content_label(nodes) if bracket else "БЛИЖАЙШИЙ МАТЧ"
        _radar_header(canvas, draw, tournament_name, f"{label} · {_radar_facts(radar, nodes)}")
        if bracket:
            _draw_radar_bracket(canvas, draw, page, i, len(pages), references=refs)
        else:
            _draw_radar_next_match(canvas, draw, radar, timezone_name)
        _card_footer(draw, "PANDASCORE", i, len(pages))
        rendered.append(_as_png(canvas))
    return rendered


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


def _draw_wide_result_match(canvas, draw, match, box, *, show_tournament=True, show_format=True, prominent=False):
    _draw_fixture_hero(canvas, draw, match, box, event=match.tournament_name if show_tournament else None,
                       show_format=show_format, prominent=prominent, subtle=True)


def _draw_compact_result_match(canvas, draw, match, box):
    _draw_fixture_row(canvas, draw, match, box)


def render_result_card(match):
    from .tournament_visuals import accent_color
    branding = _fixture_brand([match])
    canvas = _fixture_canvas(branding, RESULT_CARD_SIZE)
    draw = ImageDraw.Draw(canvas, "RGBA")
    _fixture_header(canvas, draw, "РЕЗУЛЬТАТ МАТЧА", match.tournament_name, "",
                    branding, match.tournament_logo_url)
    _draw_single_result_body(canvas, draw, match)
    _card_footer(draw, match.source, accent=accent_color(branding) if branding else CYAN)
    return _as_png(canvas)


def _confirmed_result_maps(match):
    """Do not display partial, tied or technically awarded map results."""
    maps = match.maps
    if match.forfeit or match.result_type in {"forfeit", "walkover"} or not 1 <= len(maps) <= 5:
        return []
    if any(item.score1 is None or item.score2 is None or item.score1 == item.score2 for item in maps):
        return []
    wins = (sum(item.score1 > item.score2 for item in maps),
            sum(item.score2 > item.score1 for item in maps))
    if match.best_of and (len(maps) > match.best_of or max(wins) != match.best_of // 2 + 1):
        return []
    return maps if wins == (match.score1, match.score2) else []


def _draw_single_result_body(canvas, draw, match, *, show_format=True):
    maps = _confirmed_result_maps(match)
    box = _fixture_page_boxes(1)[0] if maps else (60, 400, 1020, 865)
    _draw_wide_result_match(canvas, draw, match, box, show_tournament=False,
                           show_format=show_format, prominent=not maps)
    if not maps:
        return
    gap = 12
    width = (960 - gap * (len(maps) - 1)) / len(maps)
    for index, item in enumerate(maps):
        left = 60 + index * (width + gap)
        draw.rounded_rectangle((left, 853, left + width, 976), radius=16,
                               fill=(*PANEL, 245))
        center = left + width / 2
        _draw_text_block(draw, center, 883, item.name.upper(), width - 20,
                         26, min_size=22, max_lines=1, fill=MUTED)
        _draw_text_block(draw, center, 931, f"{item.score1}:{item.score2}", width - 20,
                         42, min_size=36, max_lines=1)


def _fixture_brand(matches):
    from .tournament_visuals import branding_for_matches
    if len({_schedule_tournament_key(match) for match in matches}) != 1:
        return None
    return branding_for_matches(matches)


def _fixture_canvas(branding, size):
    from .tournament_visuals import accent_color, add_event_glow
    if branding is None:
        return _background(size, header_accent_y=90).convert("RGBA")
    accent = accent_color(branding)
    canvas = _background(size, header_accent_colors=(accent, accent)).convert("RGBA")
    add_event_glow(canvas, accent)
    return canvas


def _fixture_header(canvas, draw, title, event, meta, branding, logo_url=None):
    if branding is None:
        _card_header(canvas, draw, title, event, meta, logo_url)
        return
    from .tournament_visuals import accent_color
    _branded_tournament_header(canvas, draw, event, branding, accent_color(branding),
                               label=title, fallback_logo_url=logo_url)
    if meta:
        _draw_text_block(draw, 540, 304, meta, 950, 24, display=True, fill=MUTED, max_lines=1)


def _chamfered_panel(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    *,
    cut: int = 22,
    accent: tuple[int, int, int] = AMBER,
    foil: bool = False,
    foil_highlight: tuple[int, int, int] = GOLD_FOIL_HIGHLIGHT,
) -> None:
    points = _chamfered_points(box, cut)
    draw.polygon(points, fill=(10, 19, 29, 245), outline=accent)
    draw.line(points + [points[0]], fill=accent, width=2)
    if foil:
        draw.line(points + [points[0]], fill=(*foil_highlight, 190), width=1)


def _chamfered_points(
    box: tuple[int, int, int, int],
    cut: int,
) -> list[tuple[int, int]]:
    x0, y0, x1, y1 = box
    return [
        (x0 + cut, y0), (x1 - cut, y0), (x1, y0 + cut), (x1, y1 - cut),
        (x1 - cut, y1), (x0 + cut, y1), (x0, y1 - cut), (x0, y0 + cut),
    ]


def _draw_final_foil_panel(
    canvas: Image.Image,
    box: tuple[int, int, int, int],
    *,
    cut: int = 22,
) -> None:
    """Draw a tight reflected edge, not a diffuse neon-like glow."""
    glow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow, "RGBA")
    points = _chamfered_points(box, cut)
    glow_draw.line(points + [points[0]], fill=(*FINAL_GOLD_HIGHLIGHT, 150), width=3)
    canvas.alpha_composite(glow.filter(ImageFilter.GaussianBlur(3)))
    _chamfered_panel(
        ImageDraw.Draw(canvas, "RGBA"),
        box,
        cut=cut,
        accent=FINAL_GOLD,
        foil=True,
        foil_highlight=FINAL_GOLD_HIGHLIGHT,
    )


def _foil_text(canvas: Image.Image, x: float, y: int, text: str, font: ImageFont.FreeTypeFont) -> None:
    """Draw deterministic metal: fine grain, local highlights and a crisp edge."""
    measure = ImageDraw.Draw(canvas)
    text = _bounded_text(measure, text, font)
    left, top, right, bottom = measure.textbbox((x, y), text, font=font)
    left, top, right, bottom = math.floor(left), math.floor(top), math.ceil(right), math.ceil(bottom)
    mask = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(mask).text((x, y), text, font=font, fill=255)

    glow = Image.new("RGBA", canvas.size, (*FINAL_GOLD_HIGHLIGHT, 0))
    glow.putalpha(mask.filter(ImageFilter.GaussianBlur(2)).point(lambda value: value * 0.2))
    canvas.alpha_composite(glow)

    texture = Image.new("RGBA", (right - left, bottom - top), (0, 0, 0, 0))
    pixels = texture.load()
    for texture_y in range(texture.height):
        source_y = top + texture_y
        for texture_x in range(texture.width):
            source_x = left + texture_x
            grain = math.sin(source_x * 12.9898 + source_y * 78.233) * 43758.5453
            grain -= math.floor(grain)
            broad_wave = 0.5 + 0.5 * math.sin(source_x * 0.037 + source_y * 0.081)
            fine_wave = 0.5 + 0.5 * math.sin(source_x * 0.19 - source_y * 0.13 + grain * 2.4)
            glint = max(0.0, math.sin(source_x * 0.023 - source_y * 0.061 + 1.4)) ** 7
            shine = max(
                0.0,
                min(1.0, 0.24 + broad_wave * 0.31 + fine_wave * 0.18 + grain * 0.12 + glint * 0.32),
            )
            pixels[texture_x, texture_y] = tuple(
                round(FINAL_GOLD_SHADOW[index] * (1 - shine) + FINAL_GOLD_HIGHLIGHT[index] * shine)
                for index in range(3)
            ) + (255,)

    foil = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    foil.paste(texture, (left, top), mask.crop((left, top, right, bottom)))
    canvas.alpha_composite(foil)


def _centered_foil_text(
    canvas: Image.Image,
    center_x: int,
    y: int,
    text: str,
    font: ImageFont.FreeTypeFont,
) -> None:
    text = _bounded_text(ImageDraw.Draw(canvas), text, font)
    box = ImageDraw.Draw(canvas).textbbox((0, 0), text, font=font)
    _foil_text(canvas, center_x - (box[2] - box[0]) / 2, y, text, font)


def _centered_foil_text_on_point(
    canvas: Image.Image,
    center_x: int,
    center_y: int,
    text: str,
    font: ImageFont.FreeTypeFont,
) -> None:
    measure = ImageDraw.Draw(canvas)
    text = _bounded_text(measure, text, font)
    box = measure.textbbox((0, 0), text, font=font)
    _foil_text(canvas, center_x - (box[0] + box[2]) / 2,
               center_y - (box[1] + box[3]) / 2, text, font)

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


def _final_event_branding(match):
    from .tournament_visuals import branding_for_match
    return branding_for_match(match)


def _branded_tournament_header(canvas, draw, tournament_name, branding, accent, *, label,
                                fallback_logo_url=None):
    from .tournament_visuals import event_logo, add_event_watermark

    logo = event_logo(branding, ASSET_DIR)
    if logo is None and fallback_logo_url:
        try:
            logo = fetch_team_logo(fallback_logo_url)
        except MediaCardError:
            logo = None
    add_event_watermark(canvas, logo)
    _draw_channel_logo(canvas, draw, (540, 90), 64)
    draw.rounded_rectangle((70, 116, 340, 156), radius=9, fill=accent)
    _draw_text_block(draw, 205, 136, label, 248, 24,
                     display=True, fill=NAVY, max_lines=1, min_size=20)
    title_width = 680 if logo is not None else 940
    _draw_text_block(draw, 70, 230, tournament_name.upper(), title_width, 44,
                     min_size=26, alignment="left", max_lines=2)
    if logo is not None:
        logo.thumbnail((220, 130), Image.Resampling.LANCZOS)
        canvas.alpha_composite(logo, (round(890 - logo.width / 2), round(223 - logo.height / 2)))


def render_final_card(match: MatchNormalized, *, branding=None) -> bytes:
    """Render a deterministic 1080px final card with map scores and champion payout."""
    if not can_render_final_card(match):
        raise MediaCardError("Final card requires confirmed final maps and winner payout")

    from .tournament_visuals import accent_color, add_event_glow

    branding = branding if branding is not None else _final_event_branding(match)
    accent = accent_color(branding) if branding is not None else FINAL_GOLD
    canvas = _background(
        RESULT_CARD_SIZE,
        header_accent_y=90,
        header_accent_colors=(accent, accent),
        header_foil=branding is None,
    ).convert("RGBA")
    if branding is not None:
        add_event_glow(canvas, accent)
    width = RESULT_CARD_SIZE[0]
    row_height = {3: 112, 4: 84, 5: 68}[len(match.maps)]
    table = (190, 510, 890, 510 + row_height * len(match.maps))
    prize_top = table[3] + 30
    prize = (190, prize_top, 890, prize_top + 86)
    if branding is None:
        _draw_final_foil_panel(canvas, table)
    else:
        outline = tuple(round(channel * .65 + background * .35)
                        for channel, background in zip(accent, PANEL))
        _chamfered_panel(ImageDraw.Draw(canvas, "RGBA"), table, accent=outline)
    _draw_final_foil_panel(canvas, prize)

    draw = ImageDraw.Draw(canvas, "RGBA")
    if branding is None:
        _card_header(canvas, draw, "ГРАНД-ФИНАЛ", match.tournament_name,
                     title_fill=FINAL_GOLD, tournament_fill=FINAL_GOLD, foil=True)
    else:
        _branded_tournament_header(canvas, draw, match.tournament_name, branding, accent,
                                   label="ГРАНД-ФИНАЛ")

    score = f"{match.score1}:{match.score2}"
    score_font = _font(150, display=True)
    winner_side = _winner_side(match)
    # The final-card template is intentionally data-first: team names frame the
    # score instead of being reduced to captions beneath logos. This keeps the
    # decisive result readable at a glance and leaves a clear visual path to the
    # map table below, including when official logos are unavailable or low-contrast.
    name_width = 330
    left_team_x = 220
    right_team_x = 860
    left_name, right_name = match.team1_name.upper(), match.team2_name.upper()
    for x, name, side in [(left_team_x, left_name, "left"), (right_team_x, right_name, "right")]:
        _draw_text_block(draw, x, 443, name, name_width, 48, min_size=36,
                         fill=FINAL_GOLD if winner_side == side else WHITE)

    x0, y0, x1, _ = table
    divider_x = (x0 + x1) // 2
    draw.line((divider_x, y0 + 16, divider_x, table[3] - 16), fill=(*accent, 180), width=2)
    map_size = {3: 44, 4: 40, 5: 36}[len(match.maps)]
    score_size = {3: 46, 4: 42, 5: 38}[len(match.maps)]
    for index, item in enumerate(match.maps):
        row_y = y0 + index * row_height
        if index:
            draw.line((x0 + 18, row_y, x1 - 18, row_y), fill=(*accent, 150), width=1)
        text_y = row_y + max(14, (row_height - map_size) // 2 - 3)
        _centered_text(
            draw,
            (x0 + divider_x) // 2,
            text_y,
            item.name,
            _fit_font(draw, item.name, 280, map_size, 16),
            WHITE,
        )
    draw.line((divider_x, prize_top + 16, divider_x, prize_top + 70), fill=(*FINAL_GOLD, 180), width=2)
    amount = _format_usd(match.winner_prize_usd)
    _card_footer(draw, "LIQUIPEDIA", accent=accent if branding is not None else CYAN)

    # Logos and names occupy separate rows and share the same team-column centre.
    # The plate shadow therefore cannot touch the lettering, even for long names.
    _draw_logo(
        canvas,
        draw,
        (left_team_x, 362),
        72,
        match.team1_name,
        match.team1_logo_url,
        FINAL_GOLD,
        match.team1_logo_fallback_url,
        content_scale=0.76,
    )
    _draw_logo(
        canvas,
        draw,
        (right_team_x, 362),
        72,
        match.team2_name,
        match.team2_logo_url,
        FINAL_GOLD,
        match.team2_logo_fallback_url,
        content_scale=0.76,
    )
    _centered_foil_text(canvas, width // 2, 297, score, score_font)
    for index, item in enumerate(match.maps):
        row_y = y0 + index * row_height
        _centered_foil_text(
            canvas,
            (divider_x + x1) // 2,
            row_y + max(14, (row_height - score_size) // 2 - 3),
            f"{item.score1}:{item.score2}",
            _font(score_size),
        )
    prize_center_y = (prize[1] + prize[3]) // 2
    _draw_text_block(ImageDraw.Draw(canvas, "RGBA"), (prize[0] + divider_x) // 2,
                     prize_center_y, "ПРИЗОВЫЕ ПОБЕДИТЕЛЯ", 300, 28,
                     display=True, fill=FINAL_GOLD)
    _centered_foil_text_on_point(
        canvas,
        (divider_x + prize[2]) // 2,
        prize_center_y,
        amount,
        _fit_font(draw, amount, 300, 58, 22, display=True),
    )
    return _as_png(canvas)


def can_render_tournament_standings(placements: Sequence[TournamentPlacement]) -> bool:
    """Only render a complete, source-confirmed standings table."""
    return (
        2 <= len(placements) <= MAX_TOURNAMENT_STANDINGS
        and len(placements) % 2 == 0
        and all(item.placement and item.team_name and item.prize_usd is not None for item in placements)
    )


def _standings_row_color(placement: str) -> tuple[int, int, int]:
    if placement == "1":
        return STANDINGS_GOLD
    if placement == "2":
        return STANDINGS_SILVER
    if placement in {"3", "3-4", "3–4"}:
        return STANDINGS_BRONZE
    return WHITE


def _tournament_table_layout(
    row_count: int,
    *,
    champion_index: int | None = None,
    header_height: int = 62,
) -> tuple[int, list[int]]:
    """Fill the table area while leaving room for the title and source line."""
    available_rows_height = 946 - 340 - header_height
    if row_count <= 5 and champion_index is not None and row_count > 1:
        champion_height = 160 if row_count <= 4 else 128
        other_height = min(128, (available_rows_height - champion_height) // (row_count - 1))
        row_heights = [other_height] * row_count
        row_heights[champion_index] = champion_height
    else:
        row_heights = [min(136, available_rows_height // row_count)] * row_count
    table_height = header_height + sum(row_heights)
    return 340 + (606 - table_height) // 2, row_heights


def _table_team_lines(
    draw: ImageDraw.ImageDraw,
    team_name: str,
    max_width: int,
    preferred_size: int,
    two_line_size: int,
) -> tuple[list[str], ImageFont.FreeTypeFont]:
    """Use the added row height to keep long team names legible."""
    preferred_font = _font(preferred_size)
    if draw.textlength(team_name, font=preferred_font) <= max_width:
        return [team_name], preferred_font
    words = team_name.split()
    if len(words) > 1:
        for size in range(two_line_size, 19, -2):
            font = _font(size)
            candidates = []
            for split in range(1, len(words)):
                lines = [" ".join(words[:split]), " ".join(words[split:])]
                widths = [draw.textlength(line, font=font) for line in lines]
                if max(widths) <= max_width:
                    candidates.append((abs(widths[0] - widths[1]), lines))
            if candidates:
                return min(candidates, key=lambda candidate: candidate[0])[1], font
    return [team_name], _fit_font(draw, team_name, max_width, preferred_size, 16)


def _metallic_shade(
    shadow: tuple[int, int, int],
    base: tuple[int, int, int],
    highlight: tuple[int, int, int],
    x: int,
    y: int,
    width: int,
    height: int,
    *,
    text: bool = False,
) -> tuple[int, int, int]:
    """A soft diagonal reflection, with enough base color to keep text legible."""
    across = x / max(1, width - 1)
    down = y / max(1, height - 1)
    band_center = (0.16 + 0.63 * across) if text else (0.12 + 0.72 * across)
    band_width = 0.22 if text else 0.19
    reflection = math.exp(-((down - band_center) / band_width) ** 2)
    reflection *= 0.50 if text else 0.68
    shade = (0.16 + 0.11 * down) if text else (0.17 + 0.26 * down + 0.08 * across)
    resting = tuple(round(a + (b - a) * shade) for a, b in zip(base, shadow))
    return tuple(round(a + (b - a) * reflection) for a, b in zip(resting, highlight))


def _draw_standings_medal(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    center_x: int,
    row_top: int,
    placement: str,
    *,
    row_height: int = 68,
    prominent: bool = False,
) -> None:
    """Give podium badges a restrained metal highlight without changing their shape."""
    color = _standings_row_color(placement)
    if color == WHITE:
        _centered_text(
            draw,
            center_x,
            row_top + (row_height - 32) // 2,
            placement,
            _fit_font(draw, placement, 110, 32, 16, display=True),
            WHITE,
        )
        return

    is_single_place = placement in {"1", "2", "3"}
    if is_single_place and row_height > 68:
        badge_width = badge_height = 62 if prominent else 54
    elif not is_single_place and row_height > 68:
        badge_width, badge_height = (102, 56) if prominent else (94, 52)
    else:
        badge_width, badge_height = (46, 46) if is_single_place else (84, 44)
    badge_top = row_top + (row_height - badge_height) // 2
    badge_left = center_x - badge_width // 2
    badge_box = (badge_left, badge_top, badge_left + badge_width - 1, badge_top + badge_height - 1)
    shadow, base, highlight = STANDINGS_METAL_PALETTES[color]
    metal = Image.new("RGBA", (badge_width, badge_height))
    pixels = metal.load()
    for y in range(badge_height):
        for x in range(badge_width):
            pixels[x, y] = (*_metallic_shade(shadow, base, highlight, x, y, badge_width, badge_height), 255)
    mask = Image.new("L", metal.size, 0)
    mask_draw = ImageDraw.Draw(mask)
    if is_single_place:
        mask_draw.ellipse((0, 0, badge_width - 1, badge_height - 1), fill=255)
    else:
        mask_draw.rounded_rectangle((0, 0, badge_width - 1, badge_height - 1), radius=badge_height // 2, fill=255)
    metal.putalpha(mask)
    canvas.alpha_composite(metal, (badge_left, badge_top))
    if is_single_place:
        draw.arc(badge_box, 195, 325, fill=(*highlight, 170), width=1)
    else:
        draw.rounded_rectangle(badge_box, radius=badge_height // 2, outline=(*shadow, 145), width=1)
    _centered_text(
        draw,
        center_x,
        badge_top + (badge_height - 30) // 2,
        placement,
        _fit_font(draw, placement, badge_width - 12, 30 if row_height > 68 else 28, 14, display=True),
        STANDINGS_MEDAL_TEXT,
    )


def _draw_standings_metal_text(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    anchor_x: int,
    y: int,
    value: str,
    font: ImageFont.FreeTypeFont,
    color: tuple[int, int, int],
    *,
    centered: bool = False,
) -> None:
    """Apply a soft angled sheen to podium names and prizes."""
    shadow, base, highlight = STANDINGS_METAL_PALETTES[color]
    bounds = draw.textbbox((0, 0), value, font=font)
    x = anchor_x - (bounds[2] - bounds[0]) / 2 if centered else anchor_x - bounds[0]
    left, top, right, bottom = draw.textbbox((x, y), value, font=font)
    left, top, right, bottom = int(left), int(top), int(right) + 1, int(bottom) + 1
    if right <= left or bottom <= top:
        return
    mask = Image.new("L", (right - left, bottom - top), 0)
    ImageDraw.Draw(mask).text((x - left, y - top), value, font=font, fill=255)
    metal = Image.new("RGBA", mask.size)
    pixels = metal.load()
    for line_y in range(mask.height):
        for line_x in range(mask.width):
            pixels[line_x, line_y] = (
                *_metallic_shade(shadow, base, highlight, line_x, line_y, mask.width, mask.height, text=True),
                255,
            )
    metal.putalpha(mask)
    canvas.alpha_composite(metal, (left, top))


def _render_tournament_standings_page(tournament_name, placements, *, source_label, page_number,
                                      page_count, branding=None):
    from .tournament_visuals import accent_color, add_event_glow

    accent = accent_color(branding) if branding is not None else AMBER
    canvas = _background(RESULT_CARD_SIZE, header_accent_y=90,
                          header_accent_colors=(accent, accent) if branding is not None
                          else (CYAN, AMBER)).convert("RGBA")
    if branding is not None:
        add_event_glow(canvas, accent)
    draw = ImageDraw.Draw(canvas, "RGBA")
    if branding is None:
        _card_header(canvas, draw, "ИТОГИ ТУРНИРА", tournament_name)
    else:
        _branded_tournament_header(canvas, draw, tournament_name, branding, accent,
                                   label="ИТОГИ ТУРНИРА")
    header_height = 72
    champion_index = next((i for i, item in enumerate(placements) if item.placement == "1"), None)
    sparse = len(placements) < TOURNAMENT_STANDINGS_PER_CARD
    if sparse:
        top, row_heights = _tournament_table_layout(len(placements), champion_index=champion_index,
                                                  header_height=header_height)
    else:
        top, row_heights = 342, [68] * len(placements)
    table = (64, top, 1016, top + header_height + sum(row_heights))
    _chamfered_panel(draw, table, cut=20, accent=accent)
    x0, y0, x1, y1 = table
    place_divider, prize_divider = 228, 760
    header_points = [(x0 + 20, y0), (x1 - 20, y0), (x1, y0 + 20), (x1, y0 + header_height),
                     (x0, y0 + header_height), (x0, y0 + 20)]
    header_color = STANDINGS_HEADER
    line_color = STANDINGS_HEADER_LINE
    if branding is not None:
        header_color = tuple(round(channel * .12 + background * .88)
                             for channel, background in zip(accent, PANEL))
        line_color = tuple(round(channel * .55 + background * .45)
                           for channel, background in zip(accent, PANEL))
    draw.polygon(header_points, fill=(*header_color, 255))
    draw.line(header_points + [header_points[0]], fill=(*line_color, 230), width=2)
    for divider in (place_divider, prize_divider):
        draw.line((divider, y0 + 14, divider, y1 - 14), fill=(*line_color, 145), width=1)
    for x, value, width in [(146, "МЕСТО", 140), (270, "КОМАНДА", 470), (888, "ПРИЗОВЫЕ", 230)]:
        _draw_text_block(draw, x, y0 + 36, value, width, 27, display=True,
                         alignment="left" if value == "КОМАНДА" else "center", max_lines=1)
    row_top = y0 + header_height
    for index, item in enumerate(placements):
        row_height = row_heights[index]
        champion = len(placements) <= 5 and index == champion_index
        center_y = row_top + row_height / 2
        if champion:
            draw.rectangle((x0 + 2, row_top + 1, x1 - 2, row_top + row_height - 1), fill=(30, 32, 32, 255))
            draw.rectangle((x0 + 4, row_top + 12, x0 + 9, row_top + row_height - 12), fill=(*STANDINGS_GOLD, 255))
            _draw_text_block(draw, 258, row_top + 24, "ПОБЕДИТЕЛЬ", 480, 24,
                             display=True, fill=STANDINGS_GOLD, alignment="left", max_lines=1)
        if index:
            draw.line((x0 + 18, row_top, x1 - 18, row_top), fill=(74, 102, 132, 150), width=1)
        color = _standings_row_color(item.placement)
        _draw_standings_medal(canvas, draw, 146, row_top, item.placement,
                             row_height=row_height, prominent=champion)
        _draw_text_block(draw, 258, row_top + row_height * .62 if champion else center_y,
                         item.team_name.upper(), 480, 46 if champion else 42 if sparse else 38,
                         min_size=36, fill=color, alignment="left")
        amount = _format_usd(item.prize_usd or 0)
        _draw_text_block(draw, 888, center_y, amount, 230, 42 if sparse else 38, min_size=30,
                         fill=color, max_lines=2)
        row_top += row_height
    _card_footer(draw, source_label, page_number, page_count,
                  accent=accent if branding is not None else CYAN)
    return _as_png(canvas)


def render_tournament_standings_cards(
    tournament_name: str,
    placements: Sequence[TournamentPlacement],
    source_label: str = "Liquipedia",
    *,
    branding=None,
) -> list[bytes]:
    """Render every confirmed placement, splitting long tables into an album."""
    if not tournament_name.strip() or not source_label.strip() or not can_render_tournament_standings(placements):
        raise MediaCardError("Tournament standings require complete placements and payouts")

    chunks = [
        placements[index:index + TOURNAMENT_STANDINGS_PER_CARD]
        for index in range(0, len(placements), TOURNAMENT_STANDINGS_PER_CARD)
    ]
    return [
        _render_tournament_standings_page(
            tournament_name,
            chunk,
            source_label=source_label,
            page_number=index,
            page_count=len(chunks),
            branding=branding,
        )
        for index, chunk in enumerate(chunks, start=1)
    ]


def _vrs_delta_text(value: int) -> tuple[str, tuple[int, int, int]]:
    if value > 0:
        return f"+{value}", VRS_UP
    if value < 0:
        return f"-{abs(value)}", VRS_DOWN
    return "0", MUTED


def _vrs_rank_text(value: int) -> tuple[str, tuple[int, int, int]]:
    if value > 0:
        return str(value), VRS_UP
    if value < 0:
        return str(abs(value)), VRS_DOWN
    return "0", MUTED


def _draw_vrs_rank_arrow(
    draw: ImageDraw.ImageDraw,
    center_x: int,
    center_y: int,
    *,
    up: bool,
    color: tuple[int, int, int],
) -> None:
    if up:
        draw.line((center_x, center_y + 11, center_x, center_y - 8), fill=color, width=4)
        draw.polygon(
            [(center_x - 7, center_y - 5), (center_x, center_y - 14), (center_x + 7, center_y - 5)],
            fill=color,
        )
    else:
        draw.line((center_x, center_y - 11, center_x, center_y + 8), fill=color, width=4)
        draw.polygon(
            [(center_x - 7, center_y + 5), (center_x, center_y + 14), (center_x + 7, center_y + 5)],
            fill=color,
        )


def _vrs_snapshot_date(effective_at: str | None, version: str) -> str | None:
    """Use the official effective date, including for older queued Valve snapshots."""
    if effective_at:
        raw_date = effective_at
    else:
        match = re.search(r"standings_[a-z]+_(\d{4})_(\d{2})_(\d{2})\.md(?::|$)", version)
        if not match:
            return None
        raw_date = "-".join(match.groups())
    try:
        return datetime.fromisoformat(raw_date.replace("Z", "+00:00")).strftime("%d.%m.%Y")
    except ValueError:
        return None


def can_render_tournament_vrs(impacts: Sequence[TournamentVRSImpact]) -> bool:
    return 2 <= len(impacts) <= MAX_TOURNAMENT_STANDINGS and len(impacts) % 2 == 0 and all(
        item.placement and item.team_name and item.source and item.before_version and item.after_version
        for item in impacts
    )


def _draw_rank_transition(draw, center_x, center_y, before, after, color):
    font = _fit_font(draw, f"{before}    {after}", 202, 38, 28)
    left_width = draw.textlength(str(before), font=font)
    right_width = draw.textlength(str(after), font=font)
    start = center_x - (left_width + 32 + right_width) / 2
    _draw_text_block(draw, start + left_width / 2, center_y, str(before),
                     left_width + 2, font.size, min_size=font.size, max_lines=1)
    arrow_x = start + left_width + 9
    draw.line((arrow_x, center_y, arrow_x + 14, center_y), fill=color, width=2)
    draw.line((arrow_x + 9, center_y - 4, arrow_x + 14, center_y,
               arrow_x + 9, center_y + 4), fill=color, width=2)
    _draw_text_block(draw, start + left_width + 32 + right_width / 2, center_y,
                     str(after), right_width + 2, font.size, min_size=font.size,
                     max_lines=1, fill=color)


def _render_tournament_vrs_page(tournament_name, impacts, *, source_label, page_number,
                                page_count, branding=None):
    from .tournament_visuals import accent_color
    accent = accent_color(branding) if branding else AMBER
    canvas = _fixture_canvas(branding, RESULT_CARD_SIZE)
    draw = ImageDraw.Draw(canvas, "RGBA")
    before = _vrs_snapshot_date(impacts[0].before_effective_at, impacts[0].before_version)
    after = _vrs_snapshot_date(impacts[0].after_effective_at, impacts[0].after_version)
    _fixture_header(canvas, draw, "VRS ПОСЛЕ ТУРНИРА", tournament_name,
                    f"ДО {before} · ПОСЛЕ {after}" if before and after else "", branding)
    header_height = 72
    sparse = len(impacts) < TOURNAMENT_VRS_PER_CARD
    if sparse:
        top, row_heights = _tournament_table_layout(len(impacts), header_height=header_height)
    else:
        top, row_heights = 342, [68] * len(impacts)
    table = (44, top, 1036, top + header_height + sum(row_heights))
    _chamfered_panel(draw, table, cut=20, accent=accent)
    x0, y0, x1, y1 = table
    place_divider, points_divider, rank_divider = 188, 656, 806
    header = [(x0 + 20, y0), (x1 - 20, y0), (x1, y0 + 20), (x1, y0 + header_height),
              (x0, y0 + header_height), (x0, y0 + 20)]
    draw.polygon(header, fill=(*STANDINGS_HEADER, 255))
    draw.line(header + [header[0]], fill=(*STANDINGS_HEADER_LINE, 230), width=2)
    for divider in (place_divider, points_divider, rank_divider):
        draw.line((divider, y0 + 14, divider, y1 - 14), fill=(*STANDINGS_HEADER_LINE, 145), width=1)
    _draw_text_block(draw, 116, y0 + 36, "МЕСТО В ТУРНИРЕ", 136, 22, min_size=22,
                     display=True, max_lines=2)
    _draw_text_block(draw, 214, y0 + 36, "КОМАНДА", 424, 27, display=True, alignment="left", max_lines=1)
    _draw_text_block(draw, 731, y0 + 36, "ИЗМ. ОЧКОВ", 140, 24, display=True)
    _draw_text_block(draw, 921, y0 + 36, "ПОЗИЦИЯ VRS", 216, 26, display=True)
    row_top = y0 + header_height
    for index, item in enumerate(impacts):
        row_height = row_heights[index]
        center_y = row_top + row_height / 2
        value_size = 42 if sparse else 38
        if index:
            draw.line((x0 + 18, row_top, x1 - 18, row_top), fill=(74, 102, 132, 150), width=1)
        _draw_standings_medal(canvas, draw, 116, row_top, item.placement, row_height=row_height)
        _draw_text_block(draw, 214, center_y, item.team_name.upper(), 424, value_size,
                         min_size=36, alignment="left", fill=_standings_row_color(item.placement))
        points, color = _vrs_delta_text(item.points_delta)
        _draw_text_block(draw, 731, center_y, points, 140, value_size, min_size=30, fill=color, max_lines=1)
        _, color = _vrs_rank_text(item.rank_delta)
        _draw_rank_transition(draw, 921, center_y - 12, item.before_rank, item.after_rank, color)
        _draw_text_block(draw, 921, center_y + 15, f"{item.rank_delta:+d}" if item.rank_delta else "0",
                         210, 26, min_size=24,
                         fill=color, max_lines=1)
        row_top += row_height
    _card_footer(draw, source_label, page_number, page_count,
                 accent=accent if branding else CYAN)
    return _as_png(canvas)


def render_tournament_vrs_cards(
    tournament_name: str,
    impacts: Sequence[TournamentVRSImpact],
    source_label: str = "Official VRS",
    *,
    branding=None,
) -> list[bytes]:
    if not tournament_name.strip() or not source_label.strip() or not can_render_tournament_vrs(impacts):
        raise MediaCardError("Tournament VRS requires complete impacts")
    chunks = [impacts[index:index + TOURNAMENT_VRS_PER_CARD] for index in range(0, len(impacts), TOURNAMENT_VRS_PER_CARD)]
    return [_render_tournament_vrs_page(tournament_name, chunk, source_label=source_label,
                                        page_number=index, page_count=len(chunks), branding=branding)
            for index, chunk in enumerate(chunks, start=1)]


def render_results_card(matches, local_now, *, page_number=1, page_count=1):
    if len({match.source for match in matches}) > 1:
        raise MediaCardError("A results page cannot mix sources")
    if not 1 <= len(matches) <= MAX_RESULT_MATCHES_PER_CARD:
        raise MediaCardError("Results page supports one to four matches; use render_results_cards")
    ordered = sorted(matches, key=lambda x: _fixture_timestamp(x.end_date or x.date or x.start_date or ""))
    branding = _fixture_brand(ordered)
    canvas = _fixture_canvas(branding, RESULT_CARD_SIZE)
    draw = ImageDraw.Draw(canvas, "RGBA")
    shared_format = _shared_fixture_format(ordered)
    events = {match.competition_key or match.tournament_name for match in ordered}
    meta = f"{local_now.day} {MONTH_NAMES[local_now.month]}"
    if shared_format:
        meta += f" · {shared_format}"
    event, logo_url = _schedule_tournament_header(ordered)
    _fixture_header(canvas, draw, "ИТОГИ ДНЯ", event, meta, branding, logo_url)
    for match, box in zip(ordered, _fixture_page_boxes(len(ordered))):
        if len(ordered) == 1:
            _draw_single_result_body(canvas, draw, match, show_format=not shared_format)
        else:
            _draw_fixture_row(canvas, draw, match, box,
                              event=match.tournament_name if len(events) > 1 else None,
                              show_format=not shared_format)
    sources = list(dict.fromkeys(x.source for x in ordered))
    from .tournament_visuals import accent_color
    _card_footer(draw, " / ".join(sources), page_number, page_count,
                 accent=accent_color(branding) if branding else CYAN)
    return _as_png(canvas)


def render_results_cards(matches, local_now):
    pages = _paginate_fixture_groups(matches, lambda x: (x.source, x.competition_key or x.tournament_name),
                                     MAX_RESULT_MATCHES, lambda x: x.end_date or x.date or x.start_date or "")
    return [render_results_card(page, local_now, page_number=i, page_count=len(pages))
            for i, page in enumerate(pages, 1)]


def render_schedule_card(matches, local_now, timezone_name, *, page_number=1, page_count=1):
    if not 1 <= len(matches) <= MAX_SCHEDULE_MATCHES:
        raise MediaCardError("Schedule page supports one to four matches; use render_schedule_cards")
    if page_count < 1 or not 1 <= page_number <= page_count:
        raise MediaCardError("Schedule card page information is invalid")
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(timezone_name)
    except Exception as exc:
        raise MediaCardError("Schedule timezone is invalid") from exc
    ordered = sorted(matches, key=lambda x: _fixture_timestamp(x.scheduled_at))
    branding = _fixture_brand(ordered)
    canvas = _fixture_canvas(branding, SCHEDULE_CARD_SIZE)
    draw = ImageDraw.Draw(canvas, "RGBA")
    event, logo_url = _schedule_tournament_header(ordered)
    meta = f"{local_now.day} {MONTH_NAMES[local_now.month]}"
    if shared_format := _shared_fixture_format(ordered):
        meta += f" · {shared_format}"
    _fixture_header(canvas, draw, "МАТЧИ СЕГОДНЯ" if branding else "МАТЧИ CS2 СЕГОДНЯ",
                    event, meta, branding, logo_url)
    mixed = len({_schedule_tournament_key(x) for x in ordered}) > 1
    show_format = not _shared_fixture_format(ordered)
    for match, box in zip(ordered, _fixture_page_boxes(len(ordered))):
        if len(ordered) == 1:
            _draw_wide_schedule_match(canvas, draw, match, box, tz, show_format=show_format)
        elif mixed:
            _draw_fixture_row(canvas, draw, match, box, time_label=_schedule_time(match, tz),
                              event=_schedule_match_event_label(match), show_format=show_format)
        else:
            _draw_compact_schedule_match(canvas, draw, match, box, tz, show_format=show_format)
    from .tournament_visuals import accent_color
    _card_footer(draw, "PANDASCORE", page_number, page_count,
                 accent=accent_color(branding) if branding else CYAN)
    return _as_png(canvas)


def paginate_schedule_matches(matches):
    if not matches or len(matches) > MAX_SCHEDULE_TOTAL_MATCHES:
        raise MediaCardError("Schedule album supports one to twenty matches")
    return _paginate_fixture_groups(matches, _schedule_tournament_key,
                                     MAX_SCHEDULE_TOTAL_MATCHES, lambda x: x.scheduled_at)


def render_schedule_cards(matches, local_now, timezone_name):
    pages = paginate_schedule_matches(matches)
    return [render_schedule_card(page, local_now, timezone_name, page_number=i, page_count=len(pages))
            for i, page in enumerate(pages, 1)]


def _format_match_count(count: int) -> str:
    if 11 <= count % 100 <= 14:
        noun = "МАТЧЕЙ"
    elif count % 10 == 1:
        noun = "МАТЧ"
    elif 2 <= count % 10 <= 4:
        noun = "МАТЧА"
    else:
        noun = "МАТЧЕЙ"
    return f"{count} {noun}"


def _context_cover_match_count(count: int) -> str:
    if 11 <= count % 100 <= 14:
        noun = "МАТЧЕЙ"
    elif count % 10 == 1:
        noun = "МАТЧ"
    elif 2 <= count % 10 <= 4:
        noun = "МАТЧА"
    else:
        noun = "МАТЧЕЙ"
    return f"{count} {noun}"


def _render_schedule_context_cover(
    matches: Sequence[UpcomingMatchNormalized],
    local_now: datetime,
) -> bytes:
    if not matches:
        raise MediaCardError("Schedule context cover requires at least one match")
    from .tournament_visuals import accent_color
    from .match_sources.config import DISPLAY_TIMEZONE
    from zoneinfo import ZoneInfo

    matches = sorted(matches, key=lambda match: _fixture_timestamp(match.scheduled_at))
    branding = _fixture_brand(matches)
    accent = accent_color(branding) if branding else CYAN
    canvas = _fixture_canvas(branding, SCHEDULE_CONTEXT_COVER_SIZE)
    draw = ImageDraw.Draw(canvas, "RGBA")
    tournament_name, tournament_logo_url = _schedule_tournament_header(matches)
    formats = {match.best_of for match in matches}
    format_label = (
        f"BO{next(iter(formats))}"
        if len(formats) == 1 and next(iter(formats))
        else "СМЕШАННЫЙ ФОРМАТ"
    )
    date_label = f"{local_now.day} {MONTH_NAMES[local_now.month]}"

    # Prefer the registered local mark, then the provider URL; never invent it.
    _fixture_header(canvas, draw, "ПЕРЕД МАТЧАМИ", tournament_name, date_label,
                    branding, tournament_logo_url)
    _draw_text_block(draw, 540, 386, "БЛИЖАЙШИЙ МАТЧ", 880, 28,
                     display=True, fill=accent, max_lines=1)
    _draw_wide_schedule_match(canvas, draw, matches[0], (60, 425, 1020, 843),
                              ZoneInfo(DISPLAY_TIMEZONE), show_format=False)
    stage = matches[0].tournament_name
    if stage != tournament_name and stage.casefold().startswith(tournament_name.casefold()):
        stage = stage[len(tournament_name):].strip(" —-")
        if stage:
            _draw_text_block(draw, 540, 886, stage.upper(), 940, 26,
                             display=_uses_cyrillic(stage), fill=MUTED, max_lines=1)
    zone_label = "МСК" if DISPLAY_TIMEZONE == "Europe/Moscow" else DISPLAY_TIMEZONE
    _draw_text_block(draw, 540, 950,
                     f"{_context_cover_match_count(len(matches))} · {format_label} · ВРЕМЯ {zone_label}",
                     940, 26, display=True, fill=MUTED, max_lines=1)
    _card_footer(draw, "PANDASCORE", accent=accent)
    return _as_png(canvas)


def render_schedule_context_covers(
    matches: Sequence[UpcomingMatchNormalized],
    local_now: datetime,
) -> list[bytes]:
    """Render one context cover per Tier-1 tournament and series format."""
    if not matches:
        raise MediaCardError("Schedule context cover requires at least one match")
    groups: dict[tuple[str, int | None], list[UpcomingMatchNormalized]] = {}
    for match in sorted(matches, key=lambda item: _fixture_timestamp(item.scheduled_at)):
        key = (_schedule_tournament_key(match), match.best_of)
        groups.setdefault(key, []).append(match)
    return [_render_schedule_context_cover(group, local_now) for group in groups.values()]
