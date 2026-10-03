from datetime import datetime, timedelta
import math
import struct
import wave
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

from cs2bot import schedule_reels
from cs2bot.match_sources.models import UpcomingMatchNormalized


NOW = datetime(2026, 9, 25, 9, 15, tzinfo=ZoneInfo("Europe/Moscow"))


def fixtures(count):
    return [
        UpcomingMatchNormalized(
            match_id=f"match-{index}", tournament_name="IEM TEST",
            team1_name="Gaimin Gladiators Academy " * 4 if index == 0 else "Spirit",
            team2_name="Vitality", scheduled_at=(NOW + timedelta(minutes=index * 30)).isoformat(),
        )
        for index in range(count)
    ]


@pytest.mark.parametrize("count,pages,duration", [(1, 1, 8), (4, 1, 8), (10, 3, 16), (20, 5, 29)])
def test_storyboard_contains_every_match_in_order(count, pages, duration):
    matches = list(reversed(fixtures(count)))
    scenes = schedule_reels.storyboard(matches)

    assert len(scenes) == pages + 2
    assert sum(scene.duration for scene in scenes) == duration
    assert [match.match_id for scene in scenes for match in scene.matches] == [
        match.match_id for match in fixtures(count)
    ]
    assert all(len(scene.matches) <= 4 for scene in scenes)


@pytest.mark.parametrize("count", [0, 21])
def test_storyboard_does_not_silently_truncate(count):
    with pytest.raises(schedule_reels.ScheduleReelError):
        schedule_reels.storyboard(fixtures(count))


def test_scene_renders_vertical_with_long_names_and_missing_logos():
    scene = schedule_reels.storyboard(fixtures(4))[1]
    image = schedule_reels.render_scene(scene, NOW, 4, preview_watermark=True)

    assert isinstance(image, Image.Image)
    assert image.size == (1080, 1920)
    assert image.mode == "RGB"
    assert image.getpixel((540, 1800)) != image.getpixel((540, 100))


def test_caption_keeps_instagram_primary_and_telegram_in_profile():
    caption = schedule_reels.reel_caption(NOW, 20)

    assert "@cs2results" in caption
    assert "@cs2_results" in caption
    assert "ссылка в профиле" in caption
    assert "PandaScore" in caption


def test_encoder_produces_h264_aac_mp4(tmp_path):
    data = schedule_reels.render_schedule_reel(fixtures(1), NOW, preview_watermark=True)
    output = tmp_path / "reel.mp4"
    output.write_bytes(data)
    probe = schedule_reels.subprocess.run(
        [schedule_reels._ffmpeg_executable(), "-hide_banner", "-i", str(output)],
        capture_output=True, text=True, check=False,
    )

    assert b"ftyp" in data[:32]
    assert len(data) > 100_000
    assert "Video: h264" in probe.stderr
    assert "Audio: aac" in probe.stderr
    assert "1080x1920" in probe.stderr


def test_encoder_failure_is_safe(monkeypatch):
    def fail(*args, **kwargs):
        raise schedule_reels.subprocess.TimeoutExpired("ffmpeg", 110)

    monkeypatch.setattr(schedule_reels.subprocess, "run", fail)
    with pytest.raises(schedule_reels.ScheduleReelError, match="encoding failed"):
        schedule_reels.render_schedule_reel(fixtures(1), NOW)


@pytest.mark.parametrize("kind", ["intro", "matches", "outro"])
def test_larger_channel_logo_stays_centered_on_lines_in_every_scene(monkeypatch, kind):
    calls = []
    original = schedule_reels._draw_channel_brand
    def capture(image, draw, **kwargs):
        calls.append(kwargs)
        return original(image, draw, **kwargs)
    monkeypatch.setattr(schedule_reels, "_draw_channel_brand", capture)
    scene = next(scene for scene in schedule_reels.storyboard(fixtures(4)) if scene.kind == kind)
    assert schedule_reels.render_scene(scene, NOW, 4).size == (1080, 1920)
    assert len(calls) == 1
    assert calls[0]["center_y"] == 214
    assert calls[0]["logo_diameter"] == 144
    # Include the 5px outer rim and retain space before the channel title.
    assert 214 + 144 / 2 + 5 < calls[0]["label_center_y"] - 15


def test_esports_beat_is_deterministic_loopable_and_has_headroom(tmp_path):
    paths = [tmp_path / f"beat-{index}.wav" for index in range(2)]
    for path in paths:
        schedule_reels._write_original_loop(path)
    assert paths[0].read_bytes() == paths[1].read_bytes()
    with wave.open(str(paths[0]), "rb") as stream:
        assert stream.getframerate() == 48000
        assert stream.getnchannels() == 1
        assert stream.getsampwidth() == 2
        assert stream.getnframes() / 48000 == pytest.approx(16 * 60 / 150)
        pcm = stream.readframes(stream.getnframes())
    values = [item[0] / 32767 for item in struct.iter_unpack("<h", pcm)]
    assert .35 < max(abs(value) for value in values) < .82
    assert .06 < math.sqrt(sum(value * value for value in values) / len(values)) < .3
    assert abs(values[-1] - values[0]) < .01
    assert abs(sum(values) / len(values)) < .01


def test_minimal_beat_is_distinct_deterministic_and_loopable(tmp_path):
    paths = [tmp_path / f"minimal-{index}.wav" for index in range(2)]
    for path in paths:
        schedule_reels._write_minimal_loop(path)
    assert paths[0].read_bytes() == paths[1].read_bytes()
    energetic = tmp_path / "esports.wav"
    schedule_reels._write_original_loop(energetic)
    assert paths[0].read_bytes() != energetic.read_bytes()
    with wave.open(str(paths[0]), "rb") as stream:
        assert stream.getframerate() == 48000 and stream.getnchannels() == 1
        assert stream.getnframes() / 48000 == pytest.approx(16 * 60 / 120)
        pcm = stream.readframes(stream.getnframes())
    values = [item[0] / 32767 for item in struct.iter_unpack("<h", pcm)]
    assert .35 < max(abs(value) for value in values) < .82
    assert .06 < math.sqrt(sum(value * value for value in values) / len(values)) < .3
    assert abs(values[-1] - values[0]) < .01
    assert abs(sum(values) / len(values)) < .01


@pytest.mark.parametrize("style", ["unknown", "melodic", "original"])
def test_unknown_audio_style_is_rejected_before_encoding(monkeypatch, style):
    def unexpected(*args, **kwargs):
        raise AssertionError("Invalid audio style must not start rendering")
    monkeypatch.setattr(schedule_reels, "storyboard", unexpected)
    with pytest.raises(schedule_reels.ScheduleReelError, match="audio style"):
        schedule_reels.render_schedule_reel(fixtures(1), NOW, audio_style=style)


@pytest.mark.parametrize("style", [None, "esports", "minimal"])
def test_audio_selection_preserves_default_and_changes_only_audio(monkeypatch, style):
    calls = []
    original_esports = schedule_reels._write_original_loop
    original_minimal = schedule_reels._write_minimal_loop
    def energetic(path):
        calls.append("esports")
        return original_esports(path)
    def minimal(path):
        calls.append("minimal")
        return original_minimal(path)
    monkeypatch.setattr(schedule_reels, "_write_original_loop", energetic)
    monkeypatch.setattr(schedule_reels, "_write_minimal_loop", minimal)
    kwargs = {} if style is None else {"audio_style": style}
    assert b"ftyp" in schedule_reels.render_schedule_reel(fixtures(1), NOW, **kwargs)[:32]
    assert calls == [style or "esports"]


def test_encoder_fades_beat_in_and_out_without_changing_duration(monkeypatch):
    commands = []
    original = schedule_reels.subprocess.run
    def capture(command, *args, **kwargs):
        commands.append(command)
        return original(command, *args, **kwargs)
    monkeypatch.setattr(schedule_reels.subprocess, "run", capture)
    schedule_reels.render_schedule_reel(fixtures(1), NOW, preview_watermark=True)
    assert len(commands) == 1
    command = commands[0]
    assert command[command.index("-t") + 1] == "8.0"
    assert command[command.index("-af") + 1] == "afade=t=in:st=0:d=0.25,afade=t=out:st=7.75:d=0.25"
def test_runtime_probe_exercises_bundled_tournament_brand_without_publication(monkeypatch):
    from cs2bot import schedule_reels as renderer
    calls = []
    def render(matches, now, **kwargs):
        calls.append((matches, kwargs))
        assert renderer._resolve_reel_visuals(matches).shared is not None
        return b"fake-mp4"
    monkeypatch.setattr(renderer, "render_schedule_reel", render)
    result = renderer.probe_reel_runtime(datetime(2026, 10, 3, 9, tzinfo=ZoneInfo("Europe/Moscow")))
    assert result["tournament_theme"] is True
    assert len(calls[0][0]) == 20
    assert calls[0][1] == {"preview_watermark": True}
