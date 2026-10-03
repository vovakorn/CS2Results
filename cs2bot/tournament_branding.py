"""Reviewed local visual themes, independent of tournament publication approval."""
from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

REGISTRY_PATH = Path(__file__).resolve().parents[1] / "data" / "tournament_branding_themes.json"
COLOR = r"^#[0-9A-Fa-f]{6}$"
KEY = r"^[a-z0-9][a-z0-9-]{0,99}$"


class VisualTheme(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    key: str = Field(pattern=KEY)
    accent: str = Field(pattern=COLOR)
    secondary: str = Field(pattern=COLOR)
    background: str = Field(pattern=COLOR)
    motif: Literal["lines", "grid", "diagonal", "ember", "metal", "target",
                   "electric-pulse", "broken-planes", "zigzag", "star", "medal"]
    logo_asset: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]{0,99}\.png$")
    logo_source_url: HttpUrl | None = None
    source_urls: list[HttpUrl] = Field(min_length=1, max_length=8)
    verified_on: date
    evidence_status: Literal["confirmed", "adapted", "proposed", "awaiting"]
    design_approved: Literal[True]

    @model_validator(mode="after")
    def sourced_logo(self):
        if bool(self.logo_asset) != bool(self.logo_source_url):
            raise ValueError("theme logo requires its official source")
        return self


class ThemeRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    themes: list[VisualTheme] = Field(min_length=1, max_length=128)
    events: dict[str, str]
    series: dict[str, str]
    organizers: dict[str, str]

    @model_validator(mode="after")
    def exact_references(self):
        keys = {theme.key for theme in self.themes}
        if len(keys) != len(self.themes):
            raise ValueError("duplicate theme keys")
        if any(value not in keys for table in (self.events, self.series, self.organizers)
               for value in table.values()):
            raise ValueError("unknown theme reference")
        return self


@lru_cache(maxsize=1)
def load_theme_registry() -> ThemeRegistry:
    return ThemeRegistry.model_validate_json(REGISTRY_PATH.read_text(encoding="utf-8"))


def resolve_branding(branding, *, event_key=None, registry=None):
    """Use exact event/series/organizer keys; explicit legacy visuals take priority.

    Resolved colors and sources enter the snapshot, so serialization/replay does
    not change inheritance. A theme never enables an editorial event profile.
    """
    from .tournament_preview import PreviewBranding

    registry = registry or load_theme_registry()
    themes = {theme.key: theme for theme in registry.themes}
    candidates = [branding.theme_key, registry.events.get(event_key),
                  registry.series.get(branding.series_key),
                  registry.organizers.get(branding.organizer_key)]
    theme = next((themes[key] for key in candidates if key in themes), None)
    if theme is None:
        # Keep old profiles byte-for-byte visually compatible without selectors.
        return branding.model_copy(update={"accent": branding.accent or "#16C7FF"})
    data = branding.model_dump()
    for field in ("accent", "secondary", "background", "motif"):
        data[field] = data[field] or getattr(theme, field)
    if not branding.logo_asset:
        data["logo_asset"] = theme.logo_asset
        data["logo_source_url"] = theme.logo_source_url
    data["theme_key"] = theme.key
    data["theme_source_urls"] = branding.theme_source_urls or theme.source_urls
    return PreviewBranding.model_validate(data)
