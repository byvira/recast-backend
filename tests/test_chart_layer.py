"""The bar-chart picture layer: its limits and what it draws."""
import io

import pytest
from PIL import Image
from pydantic import ValidationError

from app.models.image_asset import Layer
from app.pipelines.media.image_layers import _value_text, render_layers
from app.pipelines.media.image_render import BrandTokens

BRAND = BrandTokens(primary_hex="#312e81", secondary_hex="#0f172a", accent_hex="#f59e0b")
SIZE = (600, 600)


def _chart(values, **extra):
    bars = [{"label": f"L{i}", "value": v} for i, v in enumerate(values)]
    return Layer(id="c", type="chart", x=0.1, y=0.1, w=0.8, h=0.6, size=0.04, fill="#F59E0B", color="#FFFFFF", bars=bars, **extra)


def _draw(layer):
    bg = io.BytesIO()
    Image.new("RGB", (64, 64), (20, 20, 20)).save(bg, "PNG")
    return Image.open(io.BytesIO(render_layers(size=SIZE, background_bytes=bg.getvalue(), layers=[layer], brand=BRAND))).convert("RGB")


def test_more_than_six_bars_or_a_bad_value_is_refused():
    with pytest.raises(ValidationError):
        _chart([1] * 7)
    with pytest.raises(ValidationError):
        _chart([-1])
    with pytest.raises(ValidationError):
        Layer(id="c", type="chart", bars=[{"label": "x" * 25, "value": 1}])
    assert len(_chart([1] * 6).bars) == 6


def test_the_bars_are_drawn_in_the_chosen_colour_and_in_proportion():
    img = _draw(_chart([100, 50]))
    amber = lambda p: p[0] > 200 and 120 < p[1] < 200 and p[2] < 80  # noqa: E731
    cols = [sum(1 for y in range(SIZE[1]) if amber(img.getpixel((x, y)))) for x in range(SIZE[0])]
    tall, short = max(cols[:300]), max(cols[300:])
    assert tall > 0 and short > 0
    assert 1.8 < tall / short < 2.2


def test_a_chart_with_no_bars_draws_nothing():
    img = _draw(Layer(id="c", type="chart", w=0.8, h=0.5))
    assert img.getcolors(10) is not None and len(img.getcolors(10)) == 1


def test_figures_read_short():
    assert _value_text(1200) == "1,200"
    assert _value_text(12500) == "12.5K"
    assert _value_text(1_500_000) == "1.5M"
    assert _value_text(4.5) == "4.5"
