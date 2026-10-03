"""Deterministic, data-only vertical schedule reels.

The storyboard deliberately contains no editorial ranking: it shows the same
selected fixtures as the daily schedule, in chronological order.
"""
from __future__ import annotations

import math
import random
import struct
import subprocess
import tempfile
import time
import wave
from array import array
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

from .match_sources.models import UpcomingMatchNormalized
from .media_cards import (
    AMBER,
    MediaCardError,
    CYAN,
    DISPLAY_FONT,
    LATIN_BOLD_FONT,
    MUTED,
    NAVY,
    PANEL,
    WHITE,
    _draw_channel_brand,
    _draw_logo,
    _draw_text_block,
    _uses_cyrillic,
    _draw_fixture_hero,
    _schedule_time,
)


REEL_SIZE = (1080, 1920)
REEL_FPS = 24
ANIMATION_FPS = 12
REEL_LOGO_DIAMETER = 144
REEL_BEAT_BPM = 150
MINIMAL_BEAT_BPM = 120
REEL_AUDIO_FADE_SECONDS = 0.25
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
    featured_match: UpcomingMatchNormalized | None = None


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
        ReelScene("intro", (), INTRO_SECONDS, featured_match=ordered[0]),
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
        _draw_channel_brand(image, draw, center_y=214, logo_diameter=REEL_LOGO_DIAMETER,
                            label_center_y=310, label_size=44)
    except MediaCardError as exc:
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
            image, draw, center, 80, name, url, accent, fallback,
            download_timeout=min(0.75, remaining) if remaining is not None and remaining >= 0.1 else None,
        )
    draw = ImageDraw.Draw(image, "RGBA")
    for left, name in ((True, match.team1_name), (False, match.team2_name)):
        _draw_text_block(draw, x0 + 144 if left else x1 - 144, y + 182,
                         name.upper(), 296, 42, min_size=36,
                         display=_uses_cyrillic(name), alignment="left" if left else "right")



def render_scene(
    scene: ReelScene, local_now: datetime, count: int,
    timezone_name: str = "Europe/Moscow", *, preview_watermark: bool = False,
    logo_deadline: float | None = None,
    hide_matches: bool = False,
) -> Image.Image:
    try:
        tz = ZoneInfo(timezone_name)
    except Exception as exc:
        raise ScheduleReelError("Schedule Reel timezone is invalid") from exc
    image = _base()
    draw = ImageDraw.Draw(image, "RGBA")
    if scene.kind == "intro":
        _heading(draw, local_now, count)
        _center(draw, 650, "БЛИЖАЙШИЙ МАТЧ", _font(DISPLAY_FONT, 46), CYAN)
        if scene.featured_match is not None:
            _draw_fixture_hero(image, draw, scene.featured_match, (80, 760, 1000, 1250),
                               time_label=_schedule_time(scene.featured_match, tz),
                               logo_deadline=logo_deadline, subtle=True)
        else:
            _center(draw, 960, "МАТЧИ CS2 СЕГОДНЯ", _font(DISPLAY_FONT, 64), WHITE)
        _center(draw, 1350, "ВРЕМЯ МСК", _font(DISPLAY_FONT, 35), MUTED)
    elif scene.kind == "matches":
        _heading(draw, local_now, count)
        card_height, gap = 250, 24
        group_height = len(scene.matches) * card_height + (len(scene.matches) - 1) * gap
        start_y = 550 + (1090 - group_height) // 2
        for index, match in enumerate(scene.matches):
            if not hide_matches:
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


def _reveal_opacity(elapsed: float, index: int) -> float:
    value = max(0.0, min(1.0, (elapsed - index * .16) / .22))
    return value * value * (3 - 2 * value)


def _progress(image: Image.Image, fraction: float) -> None:
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((98, 1672, 982, 1682), radius=5, fill=PANEL)
    width = round(884 * max(0.0, min(1.0, fraction)))
    if width:
        draw.rounded_rectangle((98, 1672, 98 + width, 1682), radius=5, fill=CYAN)


def animated_scene_frames(scene, local_now, count, timezone_name, *, elapsed_before,
                          total_seconds, preview_watermark=False, logo_deadline=None):
    """Reuse one raster per scene; fade cards in without repeated logo downloads."""
    full = render_scene(scene, local_now, count, timezone_name,
                        preview_watermark=preview_watermark, logo_deadline=logo_deadline)
    background = (render_scene(scene, local_now, count, timezone_name,
                   preview_watermark=preview_watermark, hide_matches=True)
                  if scene.kind == "matches" else full)
    frames = math.ceil(scene.duration * ANIMATION_FPS)
    group_height = len(scene.matches) * 250 + max(0, len(scene.matches) - 1) * 24
    top = 550 + (1090 - group_height) // 2
    for index in range(frames):
        elapsed = index / ANIMATION_FPS
        frame = full.copy() if elapsed >= .75 or scene.kind != "matches" else background.copy()
        if scene.kind == "matches" and elapsed < .75:
            for row in range(len(scene.matches)):
                y = top + row * 274
                box = (97, y - 1, 984, y + 252)
                opacity = _reveal_opacity(elapsed, row)
                frame.paste(Image.blend(background.crop(box), full.crop(box), opacity), box)
        _progress(frame, (elapsed_before + elapsed + 1 / ANIMATION_FPS) / total_seconds)
        yield frame, min(1 / ANIMATION_FPS, scene.duration - elapsed)


def _write_original_loop(path: Path) -> None:
    """Synthesize a deterministic four-bar esports beat without external audio."""
    sample_rate = 48_000
    beat_seconds = 60 / REEL_BEAT_BPM
    samples = round(sample_rate * beat_seconds * 16)
    mix = array("f", [0.0]) * samples
    noise = random.Random(20261003)

    def instrument(kind: str, duration: float, frequency: float = 0) -> array:
        signal = array("f")
        previous_noise = 0.0
        for index in range(round(sample_rate * duration)):
            t = index / sample_rate
            attack = min(1.0, t * 1200)
            if kind == "kick":
                phase = 2 * math.pi * (48 * t + 110 * (1 - math.exp(-t * 38)) / 38)
                value = math.sin(phase) * math.exp(-t * 13)
                value += noise.uniform(-1, 1) * .1 * math.exp(-t * 180)
            elif kind == "snare":
                value = (.72 * noise.uniform(-1, 1) + .28 * math.sin(2 * math.pi * 185 * t))
                value *= math.exp(-t * 26)
            elif kind == "hat":
                current = noise.uniform(-1, 1)
                value = (current - previous_noise) * .5 * math.exp(-t * 70)
                previous_noise = current
            elif kind == "bass":
                phase = 2 * math.pi * frequency * t
                value = (math.sin(phase) + .35 * math.sin(2 * phase) + .18 * math.sin(3 * phase))
                value = math.tanh(value * 1.5) * math.exp(-t * 9)
            else:  # Short minor-chord stab, not a repeating lead melody.
                value = (math.sin(2 * math.pi * 146.832 * t)
                         + .6 * math.sin(2 * math.pi * 174.614 * t)
                         + .25 * math.sin(2 * math.pi * 293.664 * t))
                value *= math.exp(-t * 23)
            # Bring every hit back to zero before wrapping its tail around the loop.
            signal.append(value * attack * min(1.0, (duration - t) * 200))
        return signal

    def add(signal: array, step: int, gain: float) -> None:
        start = round(step * beat_seconds / 4 * sample_rate)
        for index, value in enumerate(signal):
            position = (start + index) % samples
            mix[position] += value * gain

    kick = instrument("kick", .38)
    snare = instrument("snare", .18)
    hat = instrument("hat", .055)
    bass = {root: instrument("bass", .22, root) for root in (36.708, 43.654)}
    stab = instrument("stab", .14)
    for bar in range(4):
        offset = bar * 16
        for step in (0, 3, 6, 8, 10):
            add(kick, offset + step, .78 if step in (0, 8) else .6)
        for step in (4, 12):
            add(snare, offset + step, .58)
        for step in range(0, 16, 2):
            add(hat, offset + step, .15 if step % 4 else .11)
        for step in ((13, 14, 15) if bar == 3 else (7, 15)):
            add(hat, offset + step, .09)
        for step in (1, 5, 7, 9, 13, 15):
            root = 43.654 if bar == 3 and step >= 13 else 36.708
            add(bass[root], offset + step, .32)
        for step in (2, 11):
            add(stab, offset + step, .12)

    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        chunk = bytearray()
        for index in range(samples):
            # Soft limiting leaves headroom for AAC rather than clipping peaks.
            value = .82 * math.tanh(mix[index] * 1.4)
            chunk.extend(struct.pack("<h", int(value * 32767)))
            if len(chunk) >= 48_000:
                stream.writeframes(chunk)
                chunk.clear()
        if chunk:
            stream.writeframes(chunk)


def _write_minimal_loop(path: Path) -> None:
    """Synthesize a sparse electronic beat: steady kick, warm bass, quiet hats."""
    sample_rate = 48_000
    beat_seconds = 60 / MINIMAL_BEAT_BPM
    samples = round(sample_rate * beat_seconds * 16)
    mix = array("f", [0.0]) * samples
    noise = random.Random(20261004)

    def instrument(kind: str, duration: float) -> array:
        signal = array("f")
        previous_noise = 0.0
        for index in range(round(sample_rate * duration)):
            t = index / sample_rate
            if kind == "kick":
                phase = 2 * math.pi * (52 * t + 65 * (1 - math.exp(-t * 45)) / 45)
                value = math.sin(phase) * math.exp(-t * 15) * min(1.0, t * 1000)
            elif kind == "bass":
                phase = 2 * math.pi * 55 * t
                value = (math.sin(phase) + .2 * math.sin(2 * phase))
                value *= math.exp(-t * 7) * min(1.0, t * 60)
            elif kind == "rim":
                value = (.45 * math.sin(2 * math.pi * 1700 * t)
                         + .25 * math.sin(2 * math.pi * 2600 * t)
                         + .12 * noise.uniform(-1, 1))
                value *= math.exp(-t * 85) * min(1.0, t * 1500)
            else:
                current = noise.uniform(-1, 1)
                value = (current - previous_noise) * .5 * math.exp(-t * 95)
                value *= min(1.0, t * 1500)
                previous_noise = current
            signal.append(value * min(1.0, (duration - t) * 200))
        return signal

    def add(signal: array, step: int, gain: float) -> None:
        start = round(step * beat_seconds / 2 * sample_rate)
        for index, value in enumerate(signal):
            mix[(start + index) % samples] += value * gain

    kick, bass = instrument("kick", .3), instrument("bass", .34)
    rim, hat = instrument("rim", .065), instrument("hat", .045)
    for beat in range(16):
        add(kick, beat * 2, .82)
        add(bass, beat * 2 + 1, .47)
        add(hat, beat * 2 + 1, .075)
        if beat % 2:
            add(rim, beat * 2, .19)

    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        chunk = bytearray()
        for value in mix:
            chunk.extend(struct.pack("<h", int(.82 * math.tanh(value * 1.4) * 32767)))
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
    audio_style: str = "esports",
) -> bytes:
    """Encode complete cached scene frames with one of the original audio loops."""
    if audio_style not in {"esports", "minimal"}:
        raise ScheduleReelError("Unknown Reel audio style")
    scenes = storyboard(matches)
    total_seconds = sum(scene.duration for scene in scenes)
    with tempfile.TemporaryDirectory(prefix="cs2-reel-") as temp_dir:
        work = Path(temp_dir)
        logo_deadline = time.monotonic() + 15
        concat_lines = ["ffconcat version 1.0"]
        elapsed_before = 0.0
        frame_index = 0
        last_frame_name = None
        for index, scene in enumerate(scenes):
            for frame, duration in animated_scene_frames(scene, local_now, len(matches), timezone_name,
                    elapsed_before=elapsed_before, total_seconds=total_seconds,
                    preview_watermark=preview_watermark, logo_deadline=logo_deadline):
                last_frame_name = f"frame-{frame_index}.png"
                frame.save(work / last_frame_name)
                concat_lines.extend((f"file '{last_frame_name}'", f"duration {duration:.9f}"))
                frame_index += 1
            elapsed_before += scene.duration
        # Repeat the last still so the concat demuxer honours its duration.
        concat_lines.append(f"file '{last_frame_name}'")
        concat_path = work / "scenes.ffconcat"
        concat_path.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")
        audio_path = work / "original-loop.wav"
        if audio_style == "minimal":
            _write_minimal_loop(audio_path)
        else:
            _write_original_loop(audio_path)
        output = work / "schedule-reel.mp4"
        command = [
            _ffmpeg_executable(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat_path),
            "-stream_loop", "-1", "-i", str(audio_path),
            "-vf", f"fps={REEL_FPS},fade=t=in:st=0:d=0.22,fade=t=out:st={total_seconds - 0.22:.2f}:d=0.22,format=yuv420p",
            "-map", "0:v:0", "-map", "1:a:0", "-t", str(total_seconds),
            "-af", (f"afade=t=in:st=0:d={REEL_AUDIO_FADE_SECONDS},"
                    f"afade=t=out:st={total_seconds - REEL_AUDIO_FADE_SECONDS:.2f}:"
                    f"d={REEL_AUDIO_FADE_SECONDS}"),
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
