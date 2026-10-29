"""Offline evidence of observed defects, not regression tests for fixed behavior.

Run with PYTHONPATH pointing at the reviewed b363ac0 checkout.
CS2_REVIEW_SOURCE optionally selects that checkout for archive checks.
"""
import asyncio
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile
from datetime import datetime, timezone

from cs2bot import publication_admin as journal


class ChronologicalJournal:
    def __init__(self):
        old = {f"admin/publications/2026-10-01/post-{i:04}.json": {
            "publication_id": f"old-{i}", "confirmed_at": "2026-10-01T10:00:00Z"
        } for i in range(500)}
        self.items = {**old, "admin/publications/2026-10-28/new.json": {
            "publication_id": "new", "confirmed_at": "2026-10-28T10:00:00Z"
        }}

    def list_objects_v2(self, **kwargs):
        keys = sorted(self.items)
        offset = int(kwargs.get("ContinuationToken", "0"))
        selected = keys[offset:offset + kwargs["MaxKeys"]]
        end = offset + len(selected)
        return {"Contents": [{"Key": k} for k in selected],
                "IsTruncated": end < len(keys), "NextContinuationToken": str(end)}

    def get_object(self, **kwargs):
        return {"Body": io.BytesIO(json.dumps(self.items[kwargs["Key"]]).encode())}


def test_journal_limit_excludes_every_current_day_publication():
    records = asyncio.run(journal.list_publications(client=ChronologicalJournal(), bucket="test"))
    assert len(records) == 500
    assert "new" not in {r["publication_id"] for r in records}
    assert journal.publications_in_period(records, "today", now=datetime(2026, 10, 28, 12, tzinfo=timezone.utc)) == []


def source_root():
    if os.getenv("CS2_REVIEW_SOURCE"):
        return Path(os.environ["CS2_REVIEW_SOURCE"])
    return Path(journal.__file__).resolve().parents[1]


def test_documented_admin_build_command_is_not_executable():
    assert not os.access(source_root() / "scripts/build_admin_function_zip.sh", os.X_OK)


def test_built_social_package_cannot_render_visual_test_card(tmp_path):
    archive = source_root() / "dist/cs2-social-publish.zip"
    assert archive.exists(), "Build the archive before running this evidence check"
    with zipfile.ZipFile(archive) as z:
        z.extractall(tmp_path)
    # -I prevents the full source checkout from hiding missing archive modules.
    code = (f"import sys; sys.path.insert(0, {str(tmp_path)!r}); "
            "from cs2bot.social_publish import _threads_visual_test_card; _threads_visual_test_card()")
    result = subprocess.run([sys.executable, "-I", "-c", code], cwd=tmp_path,
                            text=True, capture_output=True, timeout=15)
    assert result.returncode != 0
    assert "No module named 'cs2bot.tournament_visuals'" in result.stderr
