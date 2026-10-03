"""A readable event cover, followed by complete participant pages when available."""
from __future__ import annotations

import math
import re
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw

from . import media_cards as media
from .tournament_preview import TournamentPreview, date_range, prize_split_text
from .tournament_visuals import accent_color, event_logo, add_event_glow, add_event_watermark
from .tournament_branding import resolve_branding
from .tournament_theme_patterns import panel_color, preview_background

SIZE = (1080, 1080)
TEAMS_PER_PAGE = 16


def _lines(draw, text, font, width):
    # Word wrapping also handles a very long unbroken official name.
    lines, line = [], ""
    for word in text.split():
        candidate = f"{line} {word}".strip()
        if draw.textlength(candidate, font=font) <= width:
            line = candidate
            continue
        if line:
            lines.append(line)
            line = ""
        for char in word:
            if line and draw.textlength(line + char, font=font) > width:
                lines.append(line)
                line = ""
            line += char
    if line:
        lines.append(line)
    return lines


def _text(draw, text, box, *, size=40, minimum=20, color=media.WHITE, display=False, center=False):
    x0, y0, x1, y1 = box
    for font_size in range(size, minimum - 1, -1):
        font = media._font(font_size, display=display or bool(re.search(r"[А-Яа-яЁё]", text)))
        lines = _lines(draw, text, font, x1 - x0)
        spacing = math.ceil(font_size * 1.28)
        if len(lines) * spacing <= y1 - y0:
            for index, line in enumerate(lines):
                x = (x0 + x1 - draw.textlength(line, font=font)) / 2 if center else x0
                draw.text((x, y0 + index * spacing), line, font=font, fill=color)
            return
    raise media.MediaCardError("Preview text does not fit its reserved region")


def _accent(preview):
    return accent_color(preview.branding)


def _event_logo(preview):
    return event_logo(preview.branding, media.ASSET_DIR)


def _canvas(preview, label):
    accent = _accent(preview)
    image = preview_background(SIZE, preview.branding)
    add_event_glow(image, accent)
    logo = _event_logo(preview)
    add_event_watermark(image, logo)
    draw = ImageDraw.Draw(image)
    media._draw_channel_logo(image, draw, (540, 90), 64)
    if label == "ПЕРЕД СТАРТОМ":
        draw.rounded_rectangle((70, 116, 340, 156), radius=9, fill=accent)
        font = media._font(20, display=True)
        left, top, right, bottom = draw.textbbox((0, 0), label, font=font)
        position = ((70 + 340 - (right - left)) / 2 - left,
                    (116 + 156 - (bottom - top)) / 2 - top)
        draw.text(position, label, font=font, fill=media.NAVY)
    else:
        _text(draw, label, (70, 96, 800, 136), size=24, minimum=24, color=accent, display=True)
    return image, draw


def _panel(draw, box, accent, background=media.PANEL):
    outline = tuple(round(channel * .3 + base * .7) for channel, base in zip(accent, background))
    draw.rounded_rectangle(box, radius=22, fill=background, outline=outline, width=2)


def _location_pin(image, position, accent):
    # Draw the small icon at 4x resolution so it stays crisp without emoji fonts.
    scale = 4
    icon = Image.new("RGBA", (28 * scale, 34 * scale))
    draw = ImageDraw.Draw(icon)
    draw.polygon([(4 * scale, 17 * scale), (14 * scale, 33 * scale),
                  (24 * scale, 17 * scale)], fill=accent)
    draw.ellipse((3 * scale, 1 * scale, 25 * scale, 23 * scale), fill=accent)
    draw.ellipse((10 * scale, 8 * scale, 18 * scale, 16 * scale), fill=media.PANEL)
    image.alpha_composite(icon.resize((28, 34), Image.Resampling.LANCZOS), position)


def _arrow(draw, x0, y, width, accent):
    draw.line((x0, y, x0 + width, y), fill=accent, width=2)
    draw.line((x0 + width - 6, y - 5, x0 + width, y, x0 + width - 6, y + 5), fill=accent, width=2)


def _summary(draw, text, box, accent):
    if "→" not in text:
        _text(draw, text, box, size=23, minimum=14)
        return
    # Bundled display fonts have no arrow glyph; use an actual drawn arrow.
    left, right = text.split("→", 1)
    if "→" in right:
        raise media.MediaCardError("Preview summary has too many transitions")
    x0, y0, x1, y1 = box
    for size in range(23, 13, -1):
        font = media._font(size, display=bool(re.search(r"[А-Яа-яЁё]", text)))
        left_width = draw.textlength(left.strip(), font=font)
        right_width = draw.textlength(right.strip(), font=font)
        arrow_width, gap = size, 7
        if left_width + right_width + arrow_width + 2 * gap <= x1 - x0 and math.ceil(size * 1.28) <= y1 - y0:
            draw.text((x0, y0), left.strip(), font=font, fill=media.WHITE)
            bounds = draw.textbbox((x0, y0), left.strip(), font=font)
            middle = (bounds[1] + bounds[3]) / 2
            _arrow(draw, x0 + left_width + gap, middle, arrow_width, accent)
            draw.text((x0 + left_width + arrow_width + 2 * gap, y0), right.strip(), font=font, fill=media.WHITE)
            return
    raise media.MediaCardError("Preview transition does not fit its reserved region")


def _footer(draw, demo, draft=False):
    label = ("МАКЕТ · ДЕМОНСТРАЦИОННЫЕ ДАННЫЕ" if demo else
             "МАКЕТ · ПЕРЕД ПУБЛИКАЦИЕЙ СВЕРИТЬ ДАННЫЕ" if draft else "CS2 RESULTS · ПЕРЕД СТАРТОМ")
    _text(draw, label, (70, 1025, 1010, 1057), size=18, minimum=18,
          color=media.AMBER if demo or draft else media.MUTED, center=True)


def render_preview_cover(preview: TournamentPreview, *, demo=False, draft=False) -> bytes:
    preview = preview.model_copy(update={"branding": resolve_branding(preview.branding, event_key=preview.key)})
    image, draw = _canvas(preview, "ПЕРЕД СТАРТОМ")
    accent = _accent(preview)
    panel = panel_color(preview.branding, media.PANEL)
    logo = _event_logo(preview)
    title_right = 735 if logo is not None else 1010
    if logo is not None:
        scale = min(235 / logo.width, 236 / logo.height)
        logo = logo.resize((max(1, round(logo.width * scale)), max(1, round(logo.height * scale))),
                           Image.Resampling.LANCZOS)
        image.alpha_composite(logo, (int(892 - logo.width / 2), 144 + (236 - logo.height) // 2))
    title = preview.name.upper()
    season = re.fullmatch(r"(.+?)\s+(SEASON\s+\d+)", title)
    if season:
        _text(draw, season[1], (70, 176, title_right, 287), size=68, minimum=26, display=True)
        _text(draw, season[2], (70, 303, title_right, 359), size=44, minimum=26, display=True)
    else:
        _text(draw, title, (70, 176, title_right, 359), size=68, minimum=26, display=True)
    if preview.branding.headline:
        _text(draw, preview.branding.headline, (70, 372, title_right, 417), size=28, minimum=16,
              color=media.WHITE)
    _text(draw, date_range(preview.start, preview.end), (70, 431, 1010, 488),
          size=44, minimum=30, color=accent)

    _panel(draw, (70, 499, 590, 669), accent, panel)
    _text(draw, "ПРИЗОВЫЕ", (94, 518, 566, 547), size=22, minimum=22,
          color=media.MUTED, display=True)
    money = f"${preview.prize_pool_usd:,}".replace(",", " ")
    _text(draw, money, (94, 550, 566, 629), size=60, minimum=32, display=True)
    if preview.prize_money_usd is not None:
        split = prize_split_text(preview)
        _text(draw, split, (94, 633, 566, 660), size=18, minimum=14)
    _panel(draw, (614, 499, 1010, 669), accent, panel)
    if preview.participant_count:
        _text(draw, "УЧАСТНИКИ", (640, 518, 985, 547), size=22, minimum=22,
              color=media.MUTED, display=True)
        count = preview.participant_count
        noun = "команд" if 11 <= count % 100 <= 14 or count % 10 not in (1, 2, 3, 4) else (
            "команда" if count % 10 == 1 else "команды")
        _text(draw, f"{count} {noun}", (640, 550, 985, 629), size=60, minimum=26, display=True)
    else:
        _text(draw, "COUNTER-STRIKE 2", (640, 530, 985, 636), size=34, minimum=26)

    _location_pin(image, (70, 692), accent)
    locations = " / ".join(preview.locations)
    if len(preview.locations) > 2:
        locations = f"{len(preview.locations)} площадки · подробности в подписи"
    _text(draw, locations, (112, 686, 1010, 758), size=33, minimum=20)

    _text(draw, "ФОРМАТ", (70, 767, 1010, 800), size=22, minimum=22,
          color=media.MUTED, display=True)

    gap = 44 if len(preview.stages) == 2 else 34
    width = (940 - (len(preview.stages) - 1) * gap) / len(preview.stages)
    for index, stage in enumerate(preview.stages):
        x = 70 + index * (width + gap)
        _panel(draw, (x, 810, x + width, 955), accent, panel)
        _text(draw, stage.label.upper(), (x + 18, 826, x + width - 18, 855),
              size=21, minimum=18, color=accent, display=True)
        _text(draw, date_range(stage.start, stage.end), (x + 18, 861, x + width - 18, 914),
              size=30, minimum=20)
        if stage.card_summary:
            _summary(draw, stage.card_summary, (x + 18, 920, x + width - 18, 950), accent)
        if index < len(preview.stages) - 1:
            _arrow(draw, x + width + 10, 883, gap - 20, accent)
    if preview.first_match_at:
        local = preview.first_match_at.astimezone(ZoneInfo("Europe/Moscow"))
        _text(draw, f"Первый матч · {date_range(local.date(), local.date())}, {local:%H:%M} МСК",
              (70, 965, 1010, 1014),
              size=30, minimum=24)
    _footer(draw, demo, draft)
    return media._as_png(image)


def render_preview_cards(preview: TournamentPreview, *, demo=False, draft=False) -> list[bytes]:
    preview = preview.model_copy(update={"branding": resolve_branding(preview.branding, event_key=preview.key)})
    cards = [render_preview_cover(preview, demo=demo, draft=draft)]
    for offset in range(0, len(preview.teams), TEAMS_PER_PAGE):
        teams = preview.teams[offset:offset + TEAMS_PER_PAGE]
        image, draw = _canvas(preview, "УЧАСТНИКИ ТУРНИРА")
        accent = _accent(preview)
        _text(draw, preview.name, (70, 151, 1010, 227), size=34, minimum=18)
        pages = math.ceil(len(preview.teams) / TEAMS_PER_PAGE)
        if pages > 1:
            _text(draw, f"{offset // TEAMS_PER_PAGE + 1} / {pages}", (880, 96, 1010, 132),
                  size=24, minimum=24, color=media.MUTED)
        columns = 1 if len(teams) == 1 else 2 if len(teams) <= 4 else 3 if len(teams) <= 12 else 4
        rows = math.ceil(len(teams) / columns)
        gap = 16
        cell_width = (940 - (columns - 1) * gap) / columns
        cell_height = (740 - (rows - 1) * gap) / rows
        for index, team in enumerate(teams):
            x = 70 + (index % columns) * (cell_width + gap)
            y = 250 + (index // columns) * (cell_height + gap)
            _panel(draw, (x, y, x + cell_width, y + cell_height), accent,
                   panel_color(preview.branding, media.PANEL))
            diameter = int(min(200, cell_width * .58, cell_height * .50))
            center = (int(x + cell_width / 2), int(y + 18 + diameter / 2))
            media._draw_logo(image, draw, center, diameter, team.name, team.logo_url,
                             accent, download_timeout=2)
            _text(draw, team.name, (x + 14, y + diameter + 30, x + cell_width - 14, y + cell_height - 12),
                  size=36 if columns < 3 else 27, minimum=14, center=True)
        _footer(draw, demo, draft)
        cards.append(media._as_png(image))
    return cards
