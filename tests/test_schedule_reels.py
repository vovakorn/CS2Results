from datetime import datetime, timedelta
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
            team1_name="Очень длинное название команды " * 4 if index == 0 else "Spirit",
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
