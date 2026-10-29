"""Proof of the existing ZIP update defect; asserts observed incorrect behavior."""
import os
from pathlib import Path
import subprocess
import sys
import zipfile

SOURCE = Path(os.getenv("CS2_REVIEW_SOURCE", "/Users/user/.codex/worktrees/review-fixes-20261029/CS2results"))
sys.path.insert(0, str(SOURCE / "tests"))
from test_build_archives import package_source


def test_rebuild_keeps_cache_from_an_ordinary_old_build(package_source, tmp_path):
    builder = package_source / "scripts/build_social_publish_zip.sh"
    current = builder.read_text()
    old = subprocess.check_output(
        ["git", "show", "b363ac0:scripts/build_social_publish_zip.sh"],
        cwd=SOURCE, text=True,
    )
    env = dict(os.environ)
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    env.pop("PYTHONPYCACHEPREFIX", None)
    subprocess.run([sys.executable, "-c", "from cs2bot.match_sources.models import MatchNormalized"],
                   cwd=package_source, env=env, check=True)
    archive = tmp_path / "social.zip"
    builder.write_text(old)
    subprocess.run([str(builder), str(archive)], check=True, capture_output=True, text=True)
    with zipfile.ZipFile(archive) as package:
        old_cache = {name for name in package.namelist() if name.endswith(".pyc")}
        assert old_cache
        assert "cs2bot/main.py" not in package.namelist()
    builder.write_text(current)
    subprocess.run([str(builder), str(archive)], check=True, capture_output=True, text=True)
    with zipfile.ZipFile(archive) as package:
        assert old_cache <= set(package.namelist())
        assert "cs2bot/main.py" not in package.namelist()
