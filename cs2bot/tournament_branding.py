"""Reviewed local visual themes, independent of tournament publication approval."""
from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path
import re
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


class MatchThemeRule(BaseModel):
    """Reviewed name/path prefixes for visuals, never publication or identity."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    theme_key: str = Field(pattern=KEY)
    scope: Literal["event", "series", "organizer"]
    names: list[str] = Field(default_factory=list, max_length=16)
    paths: list[str] = Field(default_factory=list, max_length=16)
    allow_major: bool = False

    @model_validator(mode="after")
    def nonempty_prefixes(self):
        if not self.names and not self.paths:
            raise ValueError("visual rule needs a name or path")
        if any(not _visual_tokens(value) for value in self.names + self.paths):
            raise ValueError("visual prefixes cannot be empty")
        return self


def _visual_tokens(value):
    return tuple(re.findall(r"[^\W_]+", (value or "").casefold()))


class ThemeRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    themes: list[VisualTheme] = Field(min_length=1, max_length=128)
    events: dict[str, str]
    series: dict[str, str]
    organizers: dict[str, str]
    match_rules: list[MatchThemeRule] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def exact_references(self):
        keys = {theme.key for theme in self.themes}
        if len(keys) != len(self.themes):
            raise ValueError("duplicate theme keys")
        if any(value not in keys for table in (self.events, self.series, self.organizers)
               for value in table.values()):
            raise ValueError("unknown theme reference")
        seen = set()
        for rule in self.match_rules:
            if rule.theme_key not in keys:
                raise ValueError("unknown visual rule theme")
            for kind, prefixes in (("name", rule.names), ("path", rule.paths)):
                for prefix in prefixes:
                    key = kind, _visual_tokens(prefix)
                    if key in seen:
                        raise ValueError("duplicate visual prefix")
                    seen.add(key)
        return self


@lru_cache(maxsize=1)
def load_theme_registry() -> ThemeRegistry:
    return ThemeRegistry.model_validate_json(REGISTRY_PATH.read_text(encoding="utf-8"))


def _theme_for_tournament(name, competition_key, parent, registry):
    names = [_visual_tokens(value) for value in (name, competition_key) if value]
    paths = [_visual_tokens(parent)] if parent else []
    major = any("major" in tokens for tokens in names + paths)
    candidates = []
    for rule in registry.match_rules:
        # An unknown Major must not inherit an ordinary organizer/series logo.
        if major and not rule.allow_major:
            continue
        lengths = []
        for values, prefixes in ((names, rule.names), (paths, rule.paths)):
            for raw in prefixes:
                prefix = _visual_tokens(raw)
                if any(value[:len(prefix)] == prefix for value in values):
                    lengths.append(len(prefix))
        if lengths:
            candidates.append(({"event": 3, "series": 2, "organizer": 1}[rule.scope],
                               max(lengths), rule.theme_key))
    if not candidates:
        return "neutral" if major else None
    rank = max(item[:2] for item in candidates)
    keys = {key for priority, length, key in candidates if (priority, length) == rank}
    if len(keys) != 1:
        return None
    return keys.pop()


def branding_for_tournament(name, *, competition_key=None, parent=None, registry=None):
    """Resolve only reviewed, anchored visual aliases; never join event state."""
    from .tournament_preview import PreviewBranding

    registry = registry or load_theme_registry()
    key = _theme_for_tournament(name, competition_key, parent, registry)
    return resolve_branding(PreviewBranding(theme_key=key), registry=registry) if key else None


def resolve_branding(branding, *, event_key=None, tournament_name=None, parent=None, registry=None):
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
    # Complete legacy snapshots explicitly store theme_key=null. Their neutral
    # artwork must not change when a retry revalidates the saved passport.
    can_infer = branding.theme_key is not None or "theme_key" not in branding.model_fields_set
    if theme is None and can_infer:
        key = _theme_for_tournament(tournament_name or event_key, None, parent, registry)
        theme = themes.get(key)
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
