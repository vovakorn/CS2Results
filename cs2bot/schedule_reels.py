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

from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import media_cards, tournament_visuals
from .match_sources.models import UpcomingMatchNormalized
from .match_sources.config import TOURNAMENT_PREVIEW_PROFILES_PATH
from .tournament_identity import event_for_match
from .tournament_preview import PreviewBranding
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
MAX_INTRO_TEAMS = 6
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
    context_matches: tuple[UpcomingMatchNormalized, ...] = ()


@dataclass(frozen=True)
class ReelTournamentVisual:
    name: str
    branding: PreviewBranding | None
    logo: Image.Image | None

    @property
    def accent(self):
        return tournament_visuals.accent_color(self.branding) if self.branding else CYAN


@dataclass(frozen=True)
class ReelVisualContext:
    shared: ReelTournamentVisual | None
    by_match: dict[str, ReelTournamentVisual]


@dataclass(frozen=True)
class ReelIntroTeam:
    name: str
    logo_url: str | None
    fallback_url: str | None


def _intro_teams(matches: Sequence[UpcomingMatchNormalized]) -> tuple[ReelIntroTeam, ...]:
    """Unique names, earliest fixture order; tied starts never select a hero pair."""
    try:
        ordered = sorted(matches, key=lambda match: (
            datetime.fromisoformat(match.scheduled_at.replace("Z", "+00:00")), match.match_id))
    except (ValueError, TypeError) as exc:
        raise ScheduleReelError("Schedule Reel contains an invalid match time") from exc
    teams: dict[str, ReelIntroTeam] = {}
    for match in ordered:
        for name, url, fallback in (
            (match.team1_name, match.team1_logo_url, match.team1_logo_fallback_url),
            (match.team2_name, match.team2_logo_url, match.team2_logo_fallback_url),
        ):
            name = " ".join(name.split())
            key = name.casefold()
            previous = teams.get(key)
            teams[key] = ReelIntroTeam(previous.name if previous else name,
                (previous.logo_url or url) if previous else url,
                (previous.fallback_url or fallback) if previous else fallback)
    return tuple(teams.values())


def _intro_team_boxes(count: int) -> tuple[tuple[int, int, int, int], ...]:
    """Balanced one/two-row grids; a short final row remains centered."""
    if not 1 <= count <= MAX_INTRO_TEAMS:
        return ()
    columns = 2 if count in (2, 4) else 3
    rows = math.ceil(count / columns)
    width, height, gap = (420 if columns == 2 else 276), (350 if rows == 1 else 292), 28
    top = 970 if rows == 1 else 824
    boxes = []
    for row in range(rows):
        row_count = min(columns, count - row * columns)
        left = (REEL_SIZE[0] - (row_count * width + (row_count - 1) * gap)) // 2
        for column in range(row_count):
            x, y = left + column * (width + gap), top + row * (height + gap)
            boxes.append((x, y, x + width, y + height))
    return tuple(boxes)


def _draw_day_intro(image, draw, fixtures, count, tz, accent, logo_deadline):
    _draw_text_block(draw, 540, 640, "ТВОЯ КОМАНДА", 884, 72,
                     min_size=64, display=True, max_lines=1)
    _draw_text_block(draw, 540, 718, "ИГРАЕТ СЕГОДНЯ?", 884, 72,
                     min_size=64, display=True, max_lines=1, fill=accent)
    teams = _intro_teams(fixtures)
    shown = teams[:MAX_INTRO_TEAMS]
    for team, (x0, y0, x1, y1) in zip(shown, _intro_team_boxes(len(shown))):
        draw.rounded_rectangle((x0, y0, x1, y1), radius=24,
                               fill=(*PANEL, 242), outline=(*accent, 105), width=2)
        center = (x0 + x1) // 2
        diameter = 184 if y1 - y0 > 300 else 144
        remaining = logo_deadline - time.monotonic() if logo_deadline is not None else None
        url, fallback = team.logo_url, team.fallback_url
        if remaining is not None and remaining < .1:
            url = fallback = None
        # Two URL attempts must fit inside the remaining shared logo budget.
        attempts = max(1, len(set(filter(None, (url, fallback)))))
        timeout = min(.75, remaining / attempts) if remaining is not None and remaining >= .1 else .75
        _draw_logo(image, draw, (center, y0 + diameter // 2 + 26), diameter,
                   team.name, url, accent, fallback, download_timeout=timeout, subtle=True)
        draw = ImageDraw.Draw(image, "RGBA")
        _draw_text_block(draw, center, y1 - 62, team.name.upper(), x1 - x0 - 24,
                         42, min_size=36, display=_uses_cyrillic(team.name), max_lines=2)
    hidden = len(teams) - len(shown)
    if hidden:
        noun = "КОМАНД" if 11 <= hidden % 100 <= 14 else (
            "КОМАНДА" if hidden % 10 == 1 else "КОМАНДЫ" if hidden % 10 in (2, 3, 4) else "КОМАНД")
        _draw_text_block(draw, 540, 1480, f"ЕЩЁ {hidden} {noun}", 884, 32,
                         display=True, max_lines=1, fill=MUTED)
    meta = f"{count} {_match_noun(count).upper()}"
    if fixtures:
        earliest = min(fixtures, key=lambda match: datetime.fromisoformat(match.scheduled_at.replace("Z", "+00:00")))
        zone = "МСК" if tz.key == "Europe/Moscow" else tz.key
        meta += f" · СТАРТ В {_schedule_time(earliest, tz)} {zone}"
    _draw_text_block(draw, 540, 1554, meta, 884, 40,
                     min_size=32, display=True, max_lines=2, fill=accent)
    _draw_text_block(draw, 540, 1626, "РАСПИСАНИЕ — ДАЛЬШЕ", 884, 28,
                     display=True, max_lines=1, fill=MUTED)


def _resolve_reel_visuals(matches, logo_deadline=None):
    """Resolve exact event IDs once; local marks precede bounded provider URLs."""
    grouped, keys, profiles = {}, {}, {}
    for match in matches:
        try:
            profile = event_for_match(match, TOURNAMENT_PREVIEW_PROFILES_PATH)
        except (OSError, ValueError):
            profile = None
        # Without an exact profile, a shared city/competition label is not enough
        # to borrow another event's logo.
        key = (f"event:{profile.key}" if profile else
               ("source", match.tournament_name, match.competition_key))
        grouped.setdefault(key, []).append(match)
        keys[match.match_id] = key
        profiles[key] = profile
    visuals = {}
    for key, fixtures in grouped.items():
        profile = profiles[key]
        branding = profile.branding if profile else None
        name = (media_cards._schedule_tournament_header(fixtures)[0] if profile
                else fixtures[0].tournament_name)
        logo = tournament_visuals.event_logo(branding, media_cards.ASSET_DIR) if branding else None
        if logo is None:
            urls = dict.fromkeys(match.tournament_logo_url for match in fixtures if match.tournament_logo_url)
            for url in urls:
                remaining = logo_deadline - time.monotonic() if logo_deadline is not None else .75
                if remaining < .1:
                    break
                try:
                    logo = media_cards.fetch_team_logo(url, timeout=min(.75, remaining))
                except MediaCardError:
                    continue
                if logo is not None:
                    break
        visuals[key] = ReelTournamentVisual(name, branding, logo)
    shared = next(iter(visuals.values())) if len(visuals) == 1 and all(profiles.values()) else None
    return ReelVisualContext(shared, {match_id: visuals[key] for match_id, key in keys.items()})


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
            context_matches=ordered,
        )
        for offset in range(0, len(ordered), MATCHES_PER_SCENE)
    )
    return (
        ReelScene("intro", (), INTRO_SECONDS, context_matches=ordered),
        *middle,
        ReelScene("outro", (), OUTRO_SECONDS, context_matches=ordered),
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


def _base(visual=None) -> Image.Image:
    image = Image.new("RGBA", REEL_SIZE, (*NAVY, 255))
    draw = ImageDraw.Draw(image, "RGBA")
    for y in range(REEL_SIZE[1]):
        blend = y / REEL_SIZE[1]
        draw.line((0, y, REEL_SIZE[0], y), fill=(7 + int(7 * blend), 17 + int(13 * blend), 32 + int(18 * blend), 255))
    accent = visual.accent if visual else CYAN
    if visual:
        tournament_visuals.add_event_glow(image, accent)
        tournament_visuals.add_event_watermark(image, visual.logo, position=(704, 352), size=(290, 290))
    draw.rounded_rectangle((52, 180, 1028, 1710), radius=54, outline=(*accent, 95), width=3)
    draw.line((90, 214, 415, 214), fill=(*accent, 230), width=6)
    draw.line((665, 214, 990, 214), fill=(*(accent if visual else AMBER), 230), width=6)
    try:
        _draw_channel_brand(image, draw, center_y=214, logo_diameter=REEL_LOGO_DIAMETER,
                            label_center_y=310, label_size=44)
    except MediaCardError as exc:
        raise ScheduleReelError("Bundled channel logo is unavailable") from exc
    if visual and visual.logo is not None:
        mark = ImageOps.contain(visual.logo, (180, 128))
        image.alpha_composite(mark, (896 - mark.width // 2, 462 - mark.height // 2))
    return image


def _center(draw: ImageDraw.ImageDraw, y: int, text: str, font: ImageFont.FreeTypeFont, fill: tuple[int, ...]) -> None:
    box = draw.textbbox((0, 0), text, font=font)
    draw.text(((REEL_SIZE[0] - (box[2] - box[0])) / 2, y), text, font=font, fill=fill)


def _heading(draw: ImageDraw.ImageDraw, local_now: datetime, count: int, visual=None) -> None:
    if visual:
        draw.rounded_rectangle((98, 356, 392, 406), radius=10, fill=visual.accent)
        _draw_text_block(draw, 245, 381, "МАТЧИ СЕГОДНЯ", 270, 26,
                         min_size=24, display=True, fill=NAVY, max_lines=1)
        _draw_text_block(draw, 98, 466, visual.name.upper(), 650 if visual.logo else 884,
                         44, min_size=32, alignment="left", max_lines=2)
        _draw_text_block(draw, 98, 528,
                         f"{local_now.day} {MONTHS[local_now.month]} · {count} {_match_noun(count).upper()}",
                         650 if visual.logo else 884, 27, display=True,
                         alignment="left", fill=MUTED, max_lines=1)
        return
    _center(draw, 325, "МАТЧИ CS2 СЕГОДНЯ", _font(DISPLAY_FONT, 62), WHITE)
    _center(draw, 422, f"{local_now.day} {MONTHS[local_now.month]}  ·  {count} {_match_noun(count).upper()}", _font(DISPLAY_FONT, 36), AMBER)


def _match_card(
    image: Image.Image, match: UpcomingMatchNormalized, y: int, tz: ZoneInfo,
    logo_deadline: float | None,
    visual=None,
) -> None:
    draw = ImageDraw.Draw(image, "RGBA")
    x0, x1, bottom = 98, 982, y + 250
    branded = visual is not None and visual.branding is not None
    accent = visual.accent if branded else CYAN
    panel = tuple(round(PANEL[i] * .96 + accent[i] * .04) for i in range(3)) if branded else PANEL
    draw.rounded_rectangle((x0, y, x1, bottom), radius=30, fill=(*panel, 242), outline=(*accent, 100), width=2)
    draw.line((x0 + 28, y + 3, x0 + 245, y + 3), fill=(*accent, 210), width=4)
    draw.line((x1 - 245, y + 3, x1 - 28, y + 3), fill=(*(accent if branded else AMBER), 210), width=4)
    title_x = x0 + 30
    if visual and visual.logo is not None:
        tournament_visuals.add_event_watermark(image, visual.logo,
            position=(x0 + 20, y + 18), size=(125, 130), opacity=.05)
        mark = ImageOps.contain(visual.logo, (48, 42))
        image.alpha_composite(mark, (x0 + 53 - mark.width // 2, y + 43 - mark.height // 2))
        title_x = x0 + 92
    try:
        scheduled = datetime.fromisoformat(match.scheduled_at.replace("Z", "+00:00"))
        if scheduled.tzinfo is None:
            raise ValueError("timezone missing")
        time_label = scheduled.astimezone(tz).strftime("%H:%M")
    except ValueError as exc:
        raise ScheduleReelError("Schedule Reel contains an invalid match time") from exc
    tournament = match.tournament_name.upper()
    title_width = x1 - 28 - title_x
    tournament_font = _fit(draw, tournament, DISPLAY_FONT, title_width, 29, 21)
    draw.text((title_x, y + 25), _ellipsis(draw, tournament, tournament_font, title_width), font=tournament_font, fill=MUTED)
    time_font = _font(LATIN_BOLD_FONT, 59)
    _center(draw, y + 80, time_label, time_font, accent if branded else AMBER)
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
            subtle=branded,
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
    visuals: ReelVisualContext | None = None,
) -> Image.Image:
    try:
        tz = ZoneInfo(timezone_name)
    except Exception as exc:
        raise ScheduleReelError("Schedule Reel timezone is invalid") from exc
    if visuals is None:
        fixtures = scene.context_matches or scene.matches
        visuals = _resolve_reel_visuals(fixtures, logo_deadline)
    shared = visuals.shared
    accent = shared.accent if shared else CYAN
    image = _base(shared)
    draw = ImageDraw.Draw(image, "RGBA")
    if scene.kind == "intro":
        _heading(draw, local_now, count, shared)
        _draw_day_intro(image, draw, scene.context_matches or scene.matches, count, tz, accent, logo_deadline)
    elif scene.kind == "matches":
        _heading(draw, local_now, count, shared)
        card_height, gap = 250, 24
        group_height = len(scene.matches) * card_height + (len(scene.matches) - 1) * gap
        start_y = 550 + (1090 - group_height) // 2
        for index, match in enumerate(scene.matches):
            if not hide_matches:
                _match_card(image, match, start_y + index * (card_height + gap), tz, logo_deadline,
                            visuals.by_match.get(match.match_id))
        draw = ImageDraw.Draw(image, "RGBA")
        _center(draw, 1640, f"{scene.page} / {scene.pages}  ·  ИСТОЧНИК: PANDASCORE", _font(DISPLAY_FONT, 25), MUTED)
    elif scene.kind == "outro":
        if shared:
            _heading(draw, local_now, count, shared)
        _center(draw, 700, "НЕ ПРОПУСКАЙ", _font(DISPLAY_FONT, 77), WHITE)
        _center(draw, 805, "МАТЧИ", _font(DISPLAY_FONT, 105), accent)
        _center(draw, 1015, "ПОДПИШИСЬ", _font(DISPLAY_FONT, 69), WHITE)
        _center(draw, 1115, "@CS2RESULTS", _font(DISPLAY_FONT, 72), accent if shared else AMBER)
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


def _progress(image: Image.Image, fraction: float, accent=CYAN) -> None:
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((98, 1672, 982, 1682), radius=5, fill=PANEL)
    width = round(884 * max(0.0, min(1.0, fraction)))
    if width:
        draw.rounded_rectangle((98, 1672, 98 + width, 1682), radius=5, fill=accent)


def animated_scene_frames(scene, local_now, count, timezone_name, *, elapsed_before,
                          total_seconds, preview_watermark=False, logo_deadline=None, visuals=None):
    """Reuse one raster per scene; fade cards in without repeated logo downloads."""
    if visuals is None:
        fixtures = scene.context_matches or scene.matches
        visuals = _resolve_reel_visuals(fixtures, logo_deadline)
    full = render_scene(scene, local_now, count, timezone_name,
                        preview_watermark=preview_watermark, logo_deadline=logo_deadline, visuals=visuals)
    background = (render_scene(scene, local_now, count, timezone_name,
                   preview_watermark=preview_watermark, hide_matches=True, visuals=visuals)
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
        _progress(frame, (elapsed_before + elapsed + 1 / ANIMATION_FPS) / total_seconds,
                  visuals.shared.accent if visuals.shared else CYAN)
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
        visuals = _resolve_reel_visuals(matches, logo_deadline)
        concat_lines = ["ffconcat version 1.0"]
        elapsed_before = 0.0
        frame_index = 0
        last_frame_name = None
        for index, scene in enumerate(scenes):
            for frame, duration in animated_scene_frames(scene, local_now, len(matches), timezone_name,
                    elapsed_before=elapsed_before, total_seconds=total_seconds,
                    preview_watermark=preview_watermark, logo_deadline=logo_deadline, visuals=visuals):
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
            "-vf", f"fps={REEL_FPS},fade=t=out:st={total_seconds - 0.22:.2f}:d=0.22,format=yuv420p",
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


def probe_reel_runtime(local_now: datetime) -> dict[str, int | bool]:
    """Exercise branded animation/FFmpeg on synthetic data without uploading media."""
    start = local_now.replace(hour=10, minute=0, second=0, microsecond=0)
    from datetime import timedelta
    from .match_sources.config import TOURNAMENT_PREVIEW_PROFILES_PATH
    from .match_sources.models import SourceReferences
    from .tournament_preview import load_profiles

    profile = next((profile for profile in load_profiles(TOURNAMENT_PREVIEW_PROFILES_PATH)
                    if profile.pandascore_serie_id is not None), None)

    fixtures = [
        UpcomingMatchNormalized(
            match_id=f"probe-{index}",
            tournament_name="DEMO RUNTIME PROBE",
            source_refs=SourceReferences(serie_id=str(profile.pandascore_serie_id)) if profile else None,
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
        "tournament_theme": profile is not None,
    }
    try:
        import resource
        import sys

        if sys.platform.startswith("linux"):
            result["process_max_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except ImportError:
        pass
    return result
