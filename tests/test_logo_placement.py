"""Where a brand's logo goes on its pictures and the plate behind it: five spots, top left as before by default, bottom spots above the
headline band, and a plate that takes the opposite shade of what is under the logo."""
import io

import pytest
from PIL import Image
from pydantic import ValidationError

from app.models.brand_profile import BrandWatermark
from app.models.image_asset import Layer
from app.pipelines.media import logo_placement as lp
from app.pipelines.media.image_layers import default_layers, render_layers
from app.pipelines.media.image_render import BrandTokens

SIZE = (600, 750)


def _png(size, colour) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", size, colour).save(out, format="PNG")
    return out.getvalue()


def test_each_spot_is_where_it_says_and_a_bad_spot_falls_back_to_the_start():
    canvas, logo, margin = (600, 750), (60, 60), (36, 45)
    assert lp.place("top_left", canvas, logo, margin) == (36, 45)
    assert lp.place("top_right", canvas, logo, margin) == (600 - 36 - 60, 45)
    assert lp.place("bottom_left", canvas, logo, margin) == (36, 750 - 45 - 60)
    assert lp.place("bottom_right", canvas, logo, margin) == (600 - 36 - 60, 750 - 45 - 60)
    assert lp.place("center", canvas, logo, margin) == (270, 345)
    assert lp.place("sideways", canvas, logo, margin) == (36, 45) and lp.clean_position(None) == "top_left"
    assert lp.clean_backing("glow") == "none" and lp.clean_backing("auto") == "auto"


def test_a_bottom_spot_stays_above_the_headline_band():
    x, y = lp.place("bottom_left", (600, 750), (60, 60), (36, 45), floor=500)
    assert y + 60 <= 500 and x == 36, "the logo's bottom edge is kept above the words"
    assert lp.place("bottom_left", (600, 750), (60, 60), (36, 45), floor=9999)[1] == 750 - 45 - 60, "a floor below the margin changes nothing"


def test_the_plate_is_the_opposite_shade_of_what_is_under_the_logo():
    dark, light = Image.new("RGBA", (200, 200), (15, 15, 30, 255)), Image.new("RGBA", (200, 200), (245, 245, 235, 255))
    assert lp.plate_colour("auto", lp.brightness_under(dark, (10, 10, 60, 60)))[:3] == (255, 255, 255), "a white plate on a dark picture"
    assert lp.plate_colour("auto", lp.brightness_under(light, (10, 10, 60, 60)))[:3] == (20, 17, 15), "a dark plate on a light picture"
    assert lp.plate_colour("none", 0.9) is None
    assert lp.plate_colour("light", 0.0)[:3] == (255, 255, 255) and lp.plate_colour("dark", 1.0)[:3] == (20, 17, 15)
    assert lp.brightness_under(dark, (500, 500, 600, 600)) == 0.0, "a box off the picture measures nothing and does not fail"


def test_the_brand_settings_are_checked_and_start_as_before():
    mark = BrandWatermark()
    assert mark.logo_position == "top_left" and mark.logo_backing == "none"
    assert BrandWatermark(logo_position="bottom_right", logo_backing="auto").logo_backing == "auto"
    with pytest.raises(ValidationError):
        BrandWatermark(logo_position="middle")
    with pytest.raises(ValidationError):
        BrandWatermark(logo_backing="shiny")
    with pytest.raises(ValidationError):
        Layer(id="l", type="logo", backing="shiny")


def test_the_starting_design_puts_the_logo_where_the_brand_chose():
    base = dict(size=SIZE, headline="Good systems scale", has_logo=True)
    start = next(l for l in default_layers(brand=BrandTokens(), **base) if l.type == "logo")
    assert start.x == start.y, "top left is exactly the margin on both sides, as it always was"
    right = next(l for l in default_layers(brand=BrandTokens(logo_position="top_right"), **base) if l.type == "logo")
    assert right.x > 0.5 and abs(right.y - start.y) < 0.01, "top right is the same height, to the pixel"
    layers = default_layers(brand=BrandTokens(logo_position="bottom_left", logo_backing="auto"), **base)
    bottom = next(l for l in layers if l.type == "logo")
    band = next(l for l in layers if l.name == "Headline")
    assert bottom.y + bottom.h <= band.y + 1e-6, "a bottom logo sits above the headline, never over it"
    assert bottom.backing == "auto" and start.backing == "none"


def test_a_logo_with_a_plate_is_drawn_on_a_plate_and_one_without_is_not():
    dark_background = _png((64, 64), (10, 10, 20, 255))
    logo = _png((40, 40), (200, 30, 30, 255))
    brand = BrandTokens()

    def draw(backing):
        layer = Layer(id="logo1", type="logo", x=0.4, y=0.4, w=0.2, h=0.16, backing=backing)
        out = render_layers(size=SIZE, background_bytes=dark_background, layers=[layer], brand=brand, assets={"logo1": logo})
        return Image.open(io.BytesIO(out)).convert("RGB")

    # a point just outside the logo but inside where the plate extends
    inside_logo = (int(0.5 * SIZE[0]), int(0.48 * SIZE[1]))
    edge = (int(0.4 * SIZE[0]) - 6, int(0.48 * SIZE[1]))
    assert draw("none").getpixel(edge) != (255, 255, 255) and max(draw("none").getpixel(edge)) < 60, "no plate: the dark picture shows"
    plated = draw("auto")
    assert min(plated.getpixel(edge)) > 180, "a light plate sits behind the logo on a dark picture"
    assert plated.getpixel(inside_logo)[0] > 150 and plated.getpixel(inside_logo)[1] < 100, "the logo itself is still drawn on top"


def test_the_brands_saved_choice_reaches_the_picture_renderer_and_a_bad_one_falls_back():
    from app.api.v1.image_assets import _brand_tokens_from

    chosen = _brand_tokens_from({"watermark": {"logo_position": "bottom_right", "logo_backing": "auto"}})
    assert (chosen.logo_position, chosen.logo_backing) == ("bottom_right", "auto")
    for stored in ({}, {"watermark": None}, {"watermark": {"logo_position": "up", "logo_backing": "shiny"}}):
        plain = _brand_tokens_from(stored)
        assert (plain.logo_position, plain.logo_backing) == ("top_left", "none"), "no choice, or a bad one, is how pictures looked before"
