"""Exercise packaged code without letting the checkout satisfy missing imports."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def package_source(tmp_path):
    root = tmp_path / "source"
    shutil.copytree(ROOT / "cs2bot", root / "cs2bot",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "data", root / "data")
    for name in ("requirements.txt", "requirements-social-oauth.txt", "tier1_filter.json"):
        shutil.copy2(ROOT / name, root / name)
    (root / "scripts").mkdir()
    for name in ("build_social_publish_zip.sh", "build_social_oauth_zip.sh",
                 "build_admin_function_zip.sh", "fetch_xray_core.sh"):
        shutil.copy2(ROOT / "scripts" / name, root / "scripts" / name)
    # Packaging only copies the executable; this offline fixture never runs it.
    xray = root / "build/xray-core/v26.6.27/xray"
    xray.parent.mkdir(parents=True)
    xray.write_text("offline packaging fixture\n")
    xray.chmod(0o755)
    return root


def test_social_archive_renders_both_neutral_and_branded_cards_without_checkout(package_source, tmp_path):
    archive = tmp_path / "social.zip"
    subprocess.run([str(package_source / "scripts/build_social_publish_zip.sh"), str(archive)],
                   check=True, capture_output=True, text=True)
    extracted = tmp_path / "unpacked"
    with zipfile.ZipFile(archive) as package:
        assert "cs2bot/main.py" not in package.namelist()
        assert not any("__pycache__" in name for name in package.namelist())
        package.extractall(extracted)
    code = f"""
import sys, io
sys.path.insert(0, {str(extracted)!r})
import requests
def forbidden(*args, **kwargs):
    raise AssertionError('packaged renderer must stay offline')
requests.sessions.Session.request = forbidden
from PIL import Image
from cs2bot.social_publish import _threads_visual_test_card
from cs2bot.match_sources.models import UpcomingMatchNormalized
from cs2bot.media_cards import render_schedule_card
from datetime import datetime, timezone
assert Image.open(io.BytesIO(_threads_visual_test_card())).size == (1080, 1350)
match = UpcomingMatchNormalized(match_id='audit', tournament_name='IEM Cologne 2026',
    competition_key='IEM Cologne 2026', team1_name='A', team2_name='B',
    scheduled_at='2026-10-29T12:00:00Z', best_of=3, is_featured=True)
card = render_schedule_card([match], datetime(2026,10,29,tzinfo=timezone.utc), 'Europe/Moscow')
assert Image.open(io.BytesIO(card)).size == (1080, 1350)
"""
    result = subprocess.run([sys.executable, "-I", "-c", code], cwd=extracted,
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_documented_admin_build_command_is_executable_and_builds(package_source, tmp_path):
    script = package_source / "scripts/build_admin_function_zip.sh"
    assert os.access(script, os.X_OK)
    archive = tmp_path / "admin.zip"
    subprocess.run([str(script), str(archive)], check=True, text=True, capture_output=True)
    with zipfile.ZipFile(archive) as package:
        assert "cs2bot/admin_handler.py" in package.namelist()
        assert "requirements.txt" in package.namelist()


@pytest.mark.parametrize("script,entrypoint,xray_enabled", [
    ("build_social_publish_zip.sh", "social_publish", "1"),
    ("build_social_oauth_zip.sh", "social_oauth", "0"),
    ("build_social_oauth_zip.sh", "social_oauth", "1"),
    ("build_admin_function_zip.sh", "admin_handler", "0"),
])
@pytest.mark.parametrize("relative_output", [False, True])
def test_small_builders_replace_stale_entries_and_accept_relative_output(
    package_source, tmp_path, script, entrypoint, xray_enabled, relative_output,
):
    archive = tmp_path / "archives with spaces" / "function.zip"
    output = str(archive.relative_to(tmp_path)) if relative_output else str(archive)
    env = {**os.environ, "XRAY_ENABLED": xray_enabled}
    command = [str(package_source / "scripts" / script), output]
    subprocess.run(command, cwd=tmp_path, env=env, check=True, text=True, capture_output=True)
    with zipfile.ZipFile(archive) as package:
        expected = set(package.namelist())
        assert f"cs2bot/{entrypoint}.py" in expected
        source_requirements = "requirements-social-oauth.txt" if entrypoint == "social_oauth" else "requirements.txt"
        assert package.read("requirements.txt") == (package_source / source_requirements).read_bytes()
        if entrypoint != "admin_handler":
            assert "cs2bot/main.py" not in expected
            assert ("xray/xray" in expected) is (xray_enabled == "1")

    # Older builds can leave cache files or files removed from the next package.
    with zipfile.ZipFile(archive, "a") as package:
        package.writestr("cs2bot/__pycache__/retired.cpython-312.pyc", b"old cache")
        package.writestr("retired-package-file.txt", "old package entry")
        if entrypoint == "social_oauth" and xray_enabled == "0":
            package.writestr("xray/xray", b"binary from an older Xray-enabled package")

    subprocess.run(command, cwd=tmp_path, env=env, check=True, text=True, capture_output=True)
    with zipfile.ZipFile(archive) as package:
        assert set(package.namelist()) == expected
        assert not any("__pycache__" in name for name in package.namelist())
        assert package.testzip() is None
