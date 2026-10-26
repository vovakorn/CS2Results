"""Score cells and team groups must share explicit symmetric axes."""
from io import BytesIO

from PIL import Image, ImageDraw
import pytest

from cs2bot import media_cards as media
from cs2bot.match_sources.models import MatchNormalized, MapResult


@pytest.mark.parametrize("scores", [(2, 1), (1, 2), (3, 0), (0, 3), (13, 9), (9, 13), (None, None)])
def test_visible_score_cells_are_mirrored_around_the_colon(scores, monkeypatch):
    draw = ImageDraw.Draw(Image.new("RGBA", (1080, 1080)))
    boxes = []
    original = ImageDraw.ImageDraw.text

    def capture(surface, xy, value, **kwargs):
        boxes.append(surface.textbbox(xy, value, font=kwargs["font"]))
        return original(surface, xy, value, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)
    media._draw_fixture_score(draw, 540, 600, *scores, size=164)
    centres = [((b[0] + b[2]) / 2, (b[1] + b[3]) / 2) for b in boxes]
    assert len(boxes) == 3
    assert centres[1] == pytest.approx((540, 600), abs=.5)
    assert centres[0][0] + centres[2][0] == pytest.approx(1080, abs=1)
    assert all(y == pytest.approx(600, abs=.5) for x, y in centres)
    assert boxes[0][2] < boxes[1][0] < boxes[1][2] < boxes[2][0]


@pytest.mark.parametrize("names", [("Vitality", "NAVI"), ("NAVI", "Gaimin Gladiators Academy"),
    ("Natus Vincere Junior", "MOUZ"), ("Команда тестирования", "NAVI")])
@pytest.mark.parametrize("with_maps", [True, False])
def test_team_score_and_caption_rows_are_centred_in_the_panel(names, with_maps, monkeypatch):
    logos, captions, scores = [], [], []
    block, score = media._draw_text_block, media._draw_fixture_score
    monkeypatch.setattr(media, "_draw_logo", lambda c, d, center, diameter, *a, **kw:
                        logos.append((center, diameter)))

    def capture_block(draw, x, y, text, width, size=38, **kwargs):
        if text in {name.upper() for name in names}:
            captions.append((x, y, size))
        return block(draw, x, y, text, width, size, **kwargs)

    def capture_score(draw, x, y, a, b, **kwargs):
        if kwargs["size"] > 100:
            scores.append((x, y))
        return score(draw, x, y, a, b, **kwargs)

    monkeypatch.setattr(media, "_draw_text_block", capture_block)
    monkeypatch.setattr(media, "_draw_fixture_score", capture_score)
    match = MatchNormalized(source="pandascore", tournament_name="PGL Masters Bucharest 2026",
        team1_name=names[0], team2_name=names[1], score1=2, score2=1, best_of=3,
        maps=[MapResult(name=n, score1=a, score2=b) for n, a, b in
              [("Mirage", 13, 9), ("Nuke", 8, 13), ("Dust II", 13, 10)]] if with_maps else [])
    with Image.open(BytesIO(media.render_result_card(match))) as image:
        assert image.size == (1080, 1080)
    assert len(logos) == len(captions) == 2 and len(scores) == 1
    assert logos[0][0][0] + logos[1][0][0] == 1080
    assert scores[0] == (540, logos[0][0][1]) == (540, logos[1][0][1])
    assert captions[0][0] == logos[0][0][0] and captions[1][0] == logos[1][0][0]
    assert captions[0][1:] == captions[1][1:]
    top = logos[0][0][1] - logos[0][1] / 2
    # The tallest name fixes the bottom of a shared caption area.
    name_height = 2 * (captions[0][1] - logos[0][0][1] - logos[0][1] / 2 - 23)
    bottom = captions[0][1] + name_height / 2
    panel_center = (425 + 825) / 2 if with_maps else (400 + 865) / 2
    assert (top + bottom) / 2 == pytest.approx(panel_center, abs=1)


def test_transparent_logo_padding_does_not_shift_the_visible_mark(monkeypatch):
    mark = Image.new("RGBA", (300, 200))
    ImageDraw.Draw(mark).rectangle((190, 20, 250, 80), fill="white")
    canvas = Image.new("RGBA", (400, 400))
    monkeypatch.setattr(media, "fetch_team_logo", lambda *a, **kw: mark)
    monkeypatch.setattr(media, "_draw_logo_plate", lambda *a, **kw: None)
    media._draw_logo(canvas, ImageDraw.Draw(canvas), (200, 200), 200, "Demo", "https://example.com/logo.png", media.CYAN)
    box = canvas.getchannel("A").getbbox()
    assert ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2) == pytest.approx((200, 200), abs=.5)
    assert box[2] - box[0] == box[3] - box[1] == 128
