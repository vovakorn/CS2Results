"""Deterministic subdued motifs for the approved tournament-preview themes."""
from __future__ import annotations

import math

from PIL import Image, ImageDraw, ImageFilter


def color(value):
    return tuple(int(value[index:index + 2], 16) for index in (1, 3, 5))


def panel_color(branding, fallback):
    if not branding.background:
        return fallback
    return tuple(round(component * .5 + 23 * .5) for component in color(branding.background))


def preview_background(size, branding):
    """Scale the approved 600x480 atlas artwork beneath the full-size layout."""
    from . import media_cards as media

    if not branding.background:
        return media._background(size, header_accent_colors=(color(branding.accent),) * 2).convert("RGBA")
    accent, secondary = color(branding.accent), color(branding.secondary or branding.accent)
    image = Image.new("RGBA", size, (*color(branding.background), 255))
    motif = Image.new("RGBA", (600, 480))
    p = ImageDraw.Draw(motif)
    line = (*secondary, 28)
    name = branding.motif
    if name in ("diagonal", "ember", "metal"):
        for x in range(180, 1000, 66):
            p.line((x - 320, 480, x, -20), fill=line, width=4 if name == "metal" else 2)
    elif name in ("lines", "grid"):
        for x in range(320, 620, 40):
            p.line((x, 0, x, 480), fill=line, width=1)
        if name == "grid":
            for y in range(0, 480, 40):
                p.line((270, y, 600, y), fill=line, width=1)
    elif name == "target":
        for radius in (60, 110, 160):
            p.ellipse((480 - radius, 180 - radius, 480 + radius, 180 + radius), outline=line, width=2)
        p.line((270, 180, 600, 180), fill=line, width=2)
        p.line((480, 0, 480, 360), fill=line, width=2)
    elif name == "electric-pulse":
        for lane in range(5):
            points = [(x, 218 + lane * 24 + math.sin((x - 325) / 31 + lane * .55) *
                       (34 - lane * 3) * math.exp(-((x - 488) / 76) ** 2)) for x in range(305, 650, 3)]
            p.line(points, fill=(*accent, 30 + lane * 8), width=2, joint="curve")
        for x, y, length in ((387, 126, 32), (518, 157, 54), (445, 318, 42), (552, 352, 31)):
            p.line((x, y, x + length, y), fill=(*secondary, 88), width=2)
            p.ellipse((x - 2, y - 2, x + 2, y + 2), fill=(*accent, 125))
    elif name == "broken-planes":
        pieces = [
            ([(447, -18), (600, -18), (600, 131), (514, 164), (417, 95)], 34, False),
            ([(409, 105), (505, 175), (475, 281), (354, 250)], 25, False),
            ([(517, 177), (611, 140), (616, 300), (488, 281)], 30, True),
            ([(365, 265), (474, 296), (427, 466), (324, 392)], 19, False),
            ([(491, 296), (613, 315), (605, 465), (441, 475)], 22, False),
        ]
        for points, opacity, lime in pieces:
            shade = secondary if lime else accent
            p.polygon(points, fill=(*shade, opacity), outline=(*shade, opacity + 13), width=1)
    elif name == "zigzag":
        p.line([(430, -20), (380, 95), (535, 82), (407, 234), (554, 205), (455, 420)],
               fill=(*secondary, 85), width=20)
    elif name == "star":
        points = []
        for i in range(10):
            angle, radius = -math.pi / 2 + i * math.pi / 5, 140 if i % 2 == 0 else 62
            points.append((490 + math.cos(angle) * radius, 145 + math.sin(angle) * radius))
        p.polygon(points, outline=line, width=3)
    elif name == "medal":
        for radius in (80, 100, 130):
            p.ellipse((480 - radius, 160 - radius, 480 + radius, 160 + radius), outline=line, width=2)
    image.alpha_composite(motif.resize(size, Image.Resampling.LANCZOS))
    glow = Image.new("RGBA", (180, 180))
    ImageDraw.Draw(glow).ellipse((-45, 100, 70, 235), fill=(*secondary, 20))
    image.alpha_composite(glow.filter(ImageFilter.GaussianBlur(25)).resize(size, Image.Resampling.LANCZOS))
    draw = ImageDraw.Draw(image, "RGBA")
    draw.rounded_rectangle((34, 34, size[0] - 34, size[1] - 34), radius=42,
                           outline=(*secondary, 60), width=2)
    for x0, x1 in media._header_accent_segments(size[0]):
        draw.line((x0, 90, x1, 90), fill=(*accent, 220), width=8)
    return image
