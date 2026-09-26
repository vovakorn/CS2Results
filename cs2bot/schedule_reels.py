"""Deterministic, data-only vertical schedule reels.

The storyboard deliberately contains no editorial ranking: it shows the same
selected fixtures as the daily schedule, in chronological order.
"""
from __future__ import annotations

import math
import struct
import subprocess
import tempfile
import time
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

from .match_sources.models import UpcomingMatchNormalized
from .media_cards import (
    AMBER,
    CHANNEL_LOGO,
    CYAN,
    DISPLAY_FONT,
    LATIN_BOLD_FONT,
    MUTED,
    NAVY,
    PANEL,
    WHITE,
    _draw_logo,
)


REEL_SIZE = (1080, 1920)
REEL_FPS = 24
MAX_REEL_MATCHES = 20
MATCHES_PER_SCENE = 4
INTRO_SECONDS = 1.5
OUTRO_SECONDS = 2.5
SHORT_SCENE_SECONDS = 4.0
LONG_SCENE_SECONDS = 5.0
MONTHS = (
    "", "ЯНВАРЯ", "ФЕВРАЛЯ", "МАРТА", "АПРЕЛЯ", "МАЯ", "ИЮНЯ",
    "ИЮЛЯ", "АВГУСТА", "СЕНТЯБРЯ", "ОКТЯБРЯ", "НОЯБРЯ", "ДЕКАБРЯ",
)


class ScheduleReelError(RuntimeError):
    """A safe render/encode error; no partial Reel should be published."""


@dataclass(frozen=True)
class ReelScene:
    kind: str
    matches: tuple[UpcomingMatchNormalized, ...]
    duration: float
    page: int = 0
    pages: int = 0


def storyboard(matches: Sequence[UpcomingMatchNormalized]) -> tuple[ReelScene, ...]:
    """Return an immutable, chronological storyboard or reject incomplete days."""
    if not 1 <= len(matches) <= MAX_REEL_MATCHES:
        raise ScheduleReelError("Schedule Reel requires between 1 and 20 matches")
    try:
        ordered = tuple(sorted(matches, key=lambda match: datetime.fromisoformat(
            match.scheduled_at.replace("Z", "+00:00")
        )))
    except ValueError as exc:
        raise ScheduleReelError("Schedule Reel contains an invalid match time") from exc
    pages = math.ceil(len(ordered) / MATCHES_PER_SCENE)
    scene_seconds = LONG_SCENE_SECONDS if len(ordered) > 10 else SHORT_SCENE_SECONDS
    middle = tuple(
        ReelScene(
            "matches", ordered[offset:offset + MATCHES_PER_SCENE], scene_seconds,
            page=offset // MATCHES_PER_SCENE + 1, pages=pages,
        )
        for offset in range(0, len(ordered), MATCHES_PER_SCENE)
    )
    return (
        ReelScene("intro", (), INTRO_SECONDS),
        *middle,
        ReelScene("outro", (), OUTRO_SECONDS),
    )


def _match_noun(count: int) -> str:
    return "матч" if count % 10 == 1 and count % 100 != 11 else (
        "матча" if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14) else "матчей"
    )


def reel_caption(local_now: datetime, count: int) -> str:
    noun = _match_noun(count)
    return (
        f"Матчи CS2 сегодня — {local_now.day} {MONTHS[local_now.month].lower()}. "
        f"{count} {noun}, время по МСК.\n\n"
        "Подписывайся на @cs2results, чтобы не пропустить расписание. "
        "Полный выпуск — в Telegram @cs2_results (ссылка в профиле).\n\n"
        "Источник: PandaScore\n#CS2 #Киберспорт #РасписаниеМатчей"
    )


def _font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(str(path), size)
    except OSError as exc:
        raise ScheduleReelError("Bundled Reel font is unavailable") from exc


def _fit(draw: ImageDraw.ImageDraw, value: str, path: Path, width: int, start: int, floor: int) -> ImageFont.FreeTypeFont:
    for size in range(start, floor - 1, -1):
        font = _font(path, size)
        if draw.textbbox((0, 0), value, font=font)[2] <= width:
            return font
    return _font(path, floor)


def _ellipsis(draw: ImageDraw.ImageDraw, value: str, font: ImageFont.FreeTypeFont, width: int) -> str:
    if draw.textbbox((0, 0), value, font=font)[2] <= width:
        return value
    shortened = value
    while shortened and draw.textbbox((0, 0), shortened + "…", font=font)[2] > width:
        shortened = shortened[:-1]
    return shortened.rstrip() + "…"


def _base() -> Image.Image:
    image = Image.new("RGBA", REEL_SIZE, (*NAVY, 255))
    draw = ImageDraw.Draw(image, "RGBA")
    for y in range(REEL_SIZE[1]):
        blend = y / REEL_SIZE[1]
        draw.line((0, y, REEL_SIZE[0], y), fill=(7 + int(7 * blend), 17 + int(13 * blend), 32 + int(18 * blend), 255))
    draw.rounded_rectangle((52, 180, 1028, 1710), radius=54, outline=(*CYAN, 95), width=3)
    draw.line((90, 214, 415, 214), fill=(*CYAN, 230), width=6)
    draw.line((665, 214, 990, 214), fill=(*AMBER, 230), width=6)
    try:
        logo = Image.open(CHANNEL_LOGO).convert("RGBA")
        logo.thumbnail((116, 116), Image.Resampling.LANCZOS)
        logo_mask = Image.new("L", logo.size, 0)
        ImageDraw.Draw(logo_mask).ellipse((0, 0, logo.width - 1, logo.height - 1), fill=255)
        logo.putalpha(logo_mask)
        image.alpha_composite(logo, ((1080 - logo.width) // 2, 155))
    except OSError as exc:
        raise ScheduleReelError("Bundled channel logo is unavailable") from exc
    return image


def _center(draw: ImageDraw.ImageDraw, y: int, text: str, font: ImageFont.FreeTypeFont, fill: tuple[int, ...]) -> None:
    box = draw.textbbox((0, 0), text, font=font)
    draw.text(((REEL_SIZE[0] - (box[2] - box[0])) / 2, y), text, font=font, fill=fill)


def _heading(draw: ImageDraw.ImageDraw, local_now: datetime, count: int) -> None:
    _center(draw, 325, "МАТЧИ CS2 СЕГОДНЯ", _font(DISPLAY_FONT, 62), WHITE)
    _center(draw, 422, f"{local_now.day} {MONTHS[local_now.month]}  ·  {count} {_match_noun(count).upper()}", _font(DISPLAY_FONT, 36), AMBER)


def _match_card(
    image: Image.Image, match: UpcomingMatchNormalized, y: int, tz: ZoneInfo,
    logo_deadline: float | None,
) -> None:
    draw = ImageDraw.Draw(image, "RGBA")
    x0, x1, bottom = 98, 982, y + 250
    draw.rounded_rectangle((x0, y, x1, bottom), radius=30, fill=(*PANEL, 242), outline=(*CYAN, 100), width=2)
    draw.line((x0 + 28, y + 3, x0 + 245, y + 3), fill=(*CYAN, 210), width=4)
    draw.line((x1 - 245, y + 3, x1 - 28, y + 3), fill=(*AMBER, 210), width=4)
    try:
        scheduled = datetime.fromisoformat(match.scheduled_at.replace("Z", "+00:00"))
        if scheduled.tzinfo is None:
            raise ValueError("timezone missing")
        time_label = scheduled.astimezone(tz).strftime("%H:%M")
    except ValueError as exc:
        raise ScheduleReelError("Schedule Reel contains an invalid match time") from exc
    tournament = match.tournament_name.upper()
    tournament_font = _fit(draw, tournament, DISPLAY_FONT, 745, 29, 21)
    draw.text((x0 + 30, y + 25), _ellipsis(draw, tournament, tournament_font, 745), font=tournament_font, fill=MUTED)
    time_font = _font(LATIN_BOLD_FONT, 59)
    _center(draw, y + 80, time_label, time_font, AMBER)
    logo_y = y + 171
    for center, name, url, fallback, accent in (
        ((x0 + 93, logo_y), match.team1_name, match.team1_logo_url, match.team1_logo_fallback_url, CYAN),
        ((x1 - 93, logo_y), match.team2_name, match.team2_logo_url, match.team2_logo_fallback_url, AMBER),
    ):
        remaining = logo_deadline - time.monotonic() if logo_deadline is not None else None
        if remaining is not None and remaining < 0.1:
            url = fallback = None
        _draw_logo(
            image, draw, center, 92, name, url, accent, fallback,
            download_timeout=min(0.75, remaining) if remaining is not None and remaining >= 0.1 else None,
        )
    draw = ImageDraw.Draw(image, "RGBA")
    team1_font_path = DISPLAY_FONT if not match.team1_name.isascii() else LATIN_BOLD_FONT
    team2_font_path = DISPLAY_FONT if not match.team2_name.isascii() else LATIN_BOLD_FONT
    team_font_left = _fit(draw, match.team1_name.upper(), team1_font_path, 262, 39, 23)
    team_font_right = _fit(draw, match.team2_name.upper(), team2_font_path, 262, 39, 23)
    left = _ellipsis(draw, match.team1_name.upper(), team_font_left, 262)
    right = _ellipsis(draw, match.team2_name.upper(), team_font_right, 262)
    draw.text((x0 + 165, y + 151), left, font=team_font_left, fill=WHITE)
    right_width = draw.textbbox((0, 0), right, font=team_font_right)[2]
    draw.text((x1 - 165 - right_width, y + 151), right, font=team_font_right, fill=WHITE)
    _center(draw, y + 166, "VS", _font(LATIN_BOLD_FONT, 25), MUTED)


def render_scene(
    scene: ReelScene, local_now: datetime, count: int,
    timezone_name: str = "Europe/Moscow", *, preview_watermark: bool = False,
    logo_deadline: float | None = None,
) -> Image.Image:
    try:
        tz = ZoneInfo(timezone_name)
    except Exception as exc:
        raise ScheduleReelError("Schedule Reel timezone is invalid") from exc
    image = _base()
    draw = ImageDraw.Draw(image, "RGBA")
    if scene.kind == "intro":
        _center(draw, 650, "МАТЧИ CS2", _font(DISPLAY_FONT, 99), WHITE)
        _center(draw, 790, "СЕГОДНЯ", _font(DISPLAY_FONT, 99), CYAN)
        _center(draw, 990, f"{local_now.day} {MONTHS[local_now.month]}", _font(DISPLAY_FONT, 50), AMBER)
        _center(draw, 1110, f"{count} {_match_noun(count).upper()}  ·  ВРЕМЯ МСК", _font(DISPLAY_FONT, 35), MUTED)
    elif scene.kind == "matches":
        _heading(draw, local_now, count)
        card_height, gap = 250, 24
        group_height = len(scene.matches) * card_height + (len(scene.matches) - 1) * gap
        start_y = 550 + (1090 - group_height) // 2
        for index, match in enumerate(scene.matches):
            _match_card(image, match, start_y + index * (card_height + gap), tz, logo_deadline)
        draw = ImageDraw.Draw(image, "RGBA")
        _center(draw, 1640, f"{scene.page} / {scene.pages}  ·  ИСТОЧНИК: PANDASCORE", _font(DISPLAY_FONT, 25), MUTED)
    elif scene.kind == "outro":
        _center(draw, 700, "НЕ ПРОПУСКАЙ", _font(DISPLAY_FONT, 77), WHITE)
        _center(draw, 805, "МАТЧИ", _font(DISPLAY_FONT, 105), CYAN)
        _center(draw, 1015, "ПОДПИШИСЬ", _font(DISPLAY_FONT, 69), WHITE)
        _center(draw, 1115, "@CS2RESULTS", _font(DISPLAY_FONT, 72), AMBER)
    else:
        raise ScheduleReelError("Unknown Reel scene")
    if preview_watermark:
        draw = ImageDraw.Draw(image, "RGBA")
        draw.rounded_rectangle((238, 1770, 842, 1840), radius=20, fill=(127, 31, 31, 235))
        _center(draw, 1781, "ДЕМО · НЕ ПУБЛИКОВАТЬ", _font(DISPLAY_FONT, 30), WHITE)
    return image.convert("RGB")


def _write_original_loop(path: Path) -> None:
    """Synthesize a quiet four-bar instrumental loop with seamless boundaries."""
    sample_rate = 48_000
    duration = 8.0
    samples = int(sample_rate * duration)
    melody = (220.0, 261.63, 329.63, 293.66, 220.0, 261.63, 392.0, 329.63)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        chunk = bytearray()
        for index in range(samples):
            t = index / sample_rate
            beat = int(t * 2) % len(melody)
            phase = (t * 2) % 1
            envelope = min(1.0, phase * 16) * max(0.0, 1 - phase ** 1.6)
            bass = 0.17 * math.sin(2 * math.pi * (melody[beat] / 2) * t) * envelope
            bell = 0.095 * math.sin(2 * math.pi * melody[beat] * t) * envelope
            air = 0.025 * math.sin(2 * math.pi * 440 * t) * (1 - phase) ** 3
            edge = min(1.0, t * 8, (duration - t) * 8)
            value = max(-1.0, min(1.0, (bass + bell + air) * edge))
            chunk.extend(struct.pack("<h", int(value * 32767)))
            if len(chunk) >= 48_000:
                stream.writeframes(chunk)
                chunk.clear()
        if chunk:
            stream.writeframes(chunk)


def _ffmpeg_executable() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise ScheduleReelError("Bundled FFmpeg is unavailable") from exc


def render_schedule_reel(
    matches: Sequence[UpcomingMatchNormalized],
    local_now: datetime,
    timezone_name: str = "Europe/Moscow",
    *,
    preview_watermark: bool = False,
) -> bytes:
    """Encode one complete MP4, using static scenes rather than frame buffers."""
    scenes = storyboard(matches)
    total_seconds = sum(scene.duration for scene in scenes)
    with tempfile.TemporaryDirectory(prefix="cs2-reel-") as temp_dir:
        work = Path(temp_dir)
        logo_deadline = time.monotonic() + 15
        concat_lines = ["ffconcat version 1.0"]
        for index, scene in enumerate(scenes):
            scene_path = work / f"scene-{index}.png"
            render_scene(
                scene, local_now, len(matches), timezone_name,
                preview_watermark=preview_watermark,
                logo_deadline=logo_deadline,
            ).save(scene_path)
            concat_lines.extend((f"file 'scene-{index}.png'", f"duration {scene.duration}"))
        # Repeat the last still so the concat demuxer honours its duration.
        concat_lines.append(f"file 'scene-{len(scenes) - 1}.png'")
        concat_path = work / "scenes.ffconcat"
        concat_path.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")
        audio_path = work / "original-loop.wav"
        _write_original_loop(audio_path)
        output = work / "schedule-reel.mp4"
        command = [
            _ffmpeg_executable(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat_path),
            "-stream_loop", "-1", "-i", str(audio_path),
            "-vf", f"fps={REEL_FPS},fade=t=in:st=0:d=0.22,fade=t=out:st={total_seconds - 0.22:.2f}:d=0.22,format=yuv420p",
            "-map", "0:v:0", "-map", "1:a:0", "-t", str(total_seconds),
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "25", "-pix_fmt", "yuv420p",
            "-r", str(REEL_FPS), "-threads", "1", "-c:a", "aac", "-b:a", "128k",
            "-ar", "48000", "-movflags", "+faststart", str(output),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, timeout=110)
            data = output.read_bytes()
        except subprocess.CalledProcessError as exc:
            diagnostic = (exc.stderr or b"").decode("utf-8", "replace").replace(str(work), "<temporary>")
            raise ScheduleReelError(f"Schedule Reel encoding failed (exit {exc.returncode}): {diagnostic[-350:]}") from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ScheduleReelError("Schedule Reel encoding failed") from exc
        if not data or b"ftyp" not in data[:32]:
            raise ScheduleReelError("Schedule Reel output is not a valid MP4")
        return data


def probe_reel_runtime(local_now: datetime) -> dict[str, int]:
    """Exercise the packaged FFmpeg on synthetic data without uploading media."""
    start = local_now.replace(hour=10, minute=0, second=0, microsecond=0)
    from datetime import timedelta

    fixtures = [
        UpcomingMatchNormalized(
            match_id=f"probe-{index}",
            tournament_name="DEMO RUNTIME PROBE",
            team1_name=f"DEMO TEAM {index * 2 + 1}",
            team2_name=f"DEMO TEAM {index * 2 + 2}",
            scheduled_at=(start + timedelta(minutes=25 * index)).isoformat(),
        )
        for index in range(MAX_REEL_MATCHES)
    ]
    started = time.monotonic()
    video = render_schedule_reel(fixtures, local_now, preview_watermark=True)
    result = {
        "render_ms": round((time.monotonic() - started) * 1000),
        "mp4_bytes": len(video),
        "duration_seconds": 29,
    }
    try:
        import resource
        import sys

        if sys.platform.startswith("linux"):
            result["process_max_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except ImportError:
        pass
    return result
