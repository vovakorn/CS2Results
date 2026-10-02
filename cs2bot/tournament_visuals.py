"""Local tournament visuals shared by announcements, finals and standings."""
from PIL import Image, ImageDraw, ImageFilter


def branding_for_match(match, profiles_path=None):
    """Resolve a brand only through the exact registered tournament identity."""
    from .match_sources.config import TOURNAMENT_PREVIEW_PROFILES_PATH
    from .tournament_identity import event_for_match

    try:
        profile = event_for_match(match, profiles_path or TOURNAMENT_PREVIEW_PROFILES_PATH)
    except (OSError, ValueError):
        return None
    return profile.branding if profile is not None else None


def accent_color(branding):
    value = branding.accent.lstrip("#")
    return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))


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


def add_event_glow(image, accent):
    glow = Image.new("RGBA", (180, 180))
    ImageDraw.Draw(glow).ellipse((50, -20, 210, 110), fill=(*accent, 44))
    glow = glow.filter(ImageFilter.GaussianBlur(28)).resize(image.size, Image.Resampling.LANCZOS)
    image.alpha_composite(glow)
