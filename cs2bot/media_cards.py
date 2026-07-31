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
SCHEDULE_CARD_SIZE = (1080, 1350)
MAX_SCHEDULE_MATCHES = 6
MAX_LOGO_BYTES = 2_000_000
MAX_LOGO_PIXELS = 4_000_000
LOGO_HOSTS = {"cdn.pandascore.co"}
ALLOWED_LOGO_TYPES = {
    "application/octet-stream",
    "image/png",
    "image/jpeg",
    "image/webp",
}

logger = logging.getLogger(__name__)

NAVY = (5, 11, 24)
PANEL = (10, 25, 47)
PANEL_LIGHT = (14, 38, 69)
WHITE = (244, 247, 251)
MUTED = (150, 168, 191)
CYAN = (22, 199, 255)
AMBER = (255, 159, 28)

ASSET_DIR = Path(__file__).resolve().parent / "assets" / "fonts"
DISPLAY_FONT = ASSET_DIR / "RussoOne-Regular.ttf"
LATIN_BOLD_FONT = ASSET_DIR / "Rajdhani-Bold.ttf"
LATIN_MEDIUM_FONT = ASSET_DIR / "Rajdhani-Medium.ttf"


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
                int(5 + 4 * glow),
                int(11 + 17 * glow),
                int(24 + 30 * glow),
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
    if len(path_parts) != 2 or path_parts[1].startswith(("thumb_", "normal_")):
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


def _draw_logo(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    center: tuple[int, int],
    diameter: int,
    team_name: str,
    logo_url: str | None,
    accent: tuple[int, int, int],
) -> None:
    x, y = center
    draw.ellipse(
        (x - diameter // 2, y - diameter // 2, x + diameter // 2, y + diameter // 2),
        fill=(7, 16, 31, 235),
        outline=(*accent, 190),
        width=max(3, diameter // 45),
    )
    try:
        logo = fetch_team_logo(logo_url)
    except MediaCardError as exc:
        logger.warning(
            "team_logo_fallback team=%s host=%s error=%s",
            team_name,
            urlparse(logo_url or "").hostname or "missing",
            str(exc),
        )
        logo = None
    if logo is not None:
        contained = ImageOps.contain(logo, (int(diameter * 0.68), int(diameter * 0.68)))
        canvas.alpha_composite(contained, (x - contained.width // 2, y - contained.height // 2))
        return
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

    draw.rounded_rectangle((90, 255, 990, 825), radius=36, fill=(7, 18, 35, 220))
    _draw_logo(canvas, draw, (285, 455), 250, match.team1_name, match.team1_logo_url, CYAN)
    _draw_logo(canvas, draw, (795, 455), 250, match.team2_name, match.team2_logo_url, AMBER)

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
    """Render the daily schedule for up to six featured matches."""
    if not matches or len(matches) > MAX_SCHEDULE_MATCHES:
        raise MediaCardError("Schedule card supports between one and six matches")
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
    _centered_text(draw, width // 2, 78, "МАТЧИ CS2 СЕГОДНЯ", _font(48, display=True), WHITE)
    _centered_text(
        draw,
        width // 2,
        150,
        f"{local_now.day} {month_names[local_now.month]} · ВРЕМЯ МСК",
        _font(30, display=True),
        CYAN,
    )

    area_top = 245
    area_bottom = 1165
    row_height = min(240, (area_bottom - area_top) // len(matches))
    group_height = row_height * len(matches)
    top = area_top + ((area_bottom - area_top) - group_height) // 2
    for index, match in enumerate(
        sorted(matches, key=lambda item: item.scheduled_at)
    ):
        y0 = top + index * row_height
        y1 = y0 + row_height - 14
        center_y = (y0 + y1) // 2
        draw.rounded_rectangle((70, y0, width - 70, y1), radius=28, fill=(8, 21, 41, 225))
        _draw_logo(canvas, draw, (145, center_y), 94, match.team1_name, match.team1_logo_url, CYAN)
        _draw_logo(canvas, draw, (935, center_y), 94, match.team2_name, match.team2_logo_url, AMBER)

        try:
            parsed = datetime.fromisoformat(match.scheduled_at.replace("Z", "+00:00"))
            local_time = parsed.astimezone(display_timezone).strftime("%H:%M")
        except ValueError:
            local_time = "—"
        _centered_text(draw, width // 2, center_y - 44, local_time, _font(54), AMBER)

        team_width = 250
        team1_font = _fit_font(draw, match.team1_name.upper(), team_width, 35, 22)
        team2_font = _fit_font(draw, match.team2_name.upper(), team_width, 35, 22)
        _centered_text(draw, 320, center_y - 31, match.team1_name.upper(), team1_font, WHITE)
        _centered_text(draw, 760, center_y - 31, match.team2_name.upper(), team2_font, WHITE)

        event = match.tournament_name
        event_font = _fit_font(draw, event.upper(), 700, 22, 16, display=True)
        _centered_text(draw, width // 2, center_y + 29, event.upper(), event_font, MUTED)

    _centered_text(
        draw,
        width // 2,
        1240,
        "CS2 TIER-1 · РЕЗУЛЬТАТЫ МАТЧЕЙ",
        _font(24, display=True),
        MUTED,
    )
    return _as_png(canvas)
