"""Shared portrait content bounds; decorative backgrounds may extend beyond them."""
import json
from pathlib import Path

LAYOUT = json.loads((Path(__file__).resolve().parents[1] / "data/reel-safe-zone.json").read_text())
SAFE_BOUNDS = tuple(LAYOUT[key] for key in ("left", "top", "right", "bottom"))
SAFE_CENTER_X = (LAYOUT["left"] + LAYOUT["right"]) // 2
COVER_OFFSET_MS = LAYOUT["coverOffsetMs"]
LAYOUT_VERSION = LAYOUT["version"]
