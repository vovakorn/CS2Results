"""Shared visuals, independent of editorial approval and delivery identity."""
from PIL import Image, ImageDraw, ImageFilter


def branding_for_match(match, profiles_path=None):
    """Prefer exact profiles, then reviewed visual aliases without enabling jobs."""
    from .match_sources.config import TOURNAMENT_PREVIEW_PROFILES_PATH
    from .tournament_identity import event_for_match

    try:
        profile = event_for_match(match, profiles_path or TOURNAMENT_PREVIEW_PROFILES_PATH)
    except ValueError:
        # Conflicting or invalid profiles must not be hidden by an alias.
        return None
    except OSError:
        profile = None
    if profile is not None:
        return profile.branding
    from .tournament_branding import branding_for_tournament
    return branding_for_tournament(match.tournament_name,
        competition_key=getattr(match, "competition_key", None),
        parent=getattr(match, "tournament_parent", None))


def visual_group_key(match, profile=None):
    """Group a visual page, never replace the canonical publication identity."""
    if profile is not None:
        return ("event", profile.key)
    source = getattr(match, "source", "pandascore")
    refs = getattr(match, "source_refs", None)
    if getattr(refs, "serie_id", None):
        return (source, "serie", refs.serie_id)
    if getattr(match, "tournament_parent", None):
        return (source, "parent", match.tournament_parent)
    if getattr(refs, "tournament_id", None):
        return (source, "tournament", refs.tournament_id)
    return (source, "label", match.tournament_name, getattr(match, "competition_key", None))


def accent_color(branding):
    value = (branding.accent or "#16C7FF").lstrip("#")
    return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))


def branding_for_matches(matches, profiles_path=None):
    """Brand a homogeneous page; a shared theme alone never joins events."""
    from .match_sources.config import TOURNAMENT_PREVIEW_PROFILES_PATH
    from .tournament_identity import event_for_match
    try:
        profiles = [event_for_match(match, profiles_path or TOURNAMENT_PREVIEW_PROFILES_PATH)
                    for match in matches]
    except ValueError:
        return None
    except OSError:
        profiles = [None] * len(matches)
    if not profiles:
        return None
    if len({visual_group_key(match, profile) for match, profile in zip(matches, profiles)}) != 1:
        return None
    brands = [profile.branding if profile else branding_for_match(match, profiles_path)
              for match, profile in zip(matches, profiles)]
    if any(brand is None for brand in brands) or any(brand != brands[0] for brand in brands):
        return None
    return brands[0]


def event_logo(branding, asset_dir):
    if not branding.logo_asset:
        return None
    try:
        with Image.open(asset_dir / "tournament-preview" / branding.logo_asset) as source:
            logo = source.convert("RGBA")
        bounds = logo.getbbox()
        return logo.crop(bounds) if bounds else None
    except OSError:
        return None


def add_event_watermark(image, logo, *, position=(690, 110), size=(365, 440), opacity=.065):
    """Add the same subdued logo backdrop without modifying the foreground logo."""
    if logo is None:
        return
    watermark = logo.copy()
    watermark.thumbnail(size, Image.Resampling.LANCZOS)
    watermark.putalpha(watermark.getchannel("A").point(lambda value: round(value * opacity)))
    image.alpha_composite(watermark, position)


def add_event_glow(image, accent):
    glow = Image.new("RGBA", (180, 180))
    ImageDraw.Draw(glow).ellipse((50, -20, 210, 110), fill=(*accent, 44))
    glow = glow.filter(ImageFilter.GaussianBlur(28)).resize(image.size, Image.Resampling.LANCZOS)
    image.alpha_composite(glow)
