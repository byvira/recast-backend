"""Icons and the illustration accent on image cards (bundled Lucide icon font).

Pure rendering tests: real Pillow, real bundled fonts, no database or network.
"""

import io

import pytest
from PIL import Image

from app.models.image_asset import LayoutPreset
from app.pipelines.media import icons
from app.pipelines.media.image_render import BrandTokens, SlideTextContent, render_slide

TOKENS = BrandTokens(primary_hex="#101020", accent_hex="#38bdf8")


def _render(**text) -> bytes:
    return render_slide(
        layout=LayoutPreset.QUOTE_1_1,
        base_image_bytes=None,
        brand_tokens=TOKENS,
        text_content=SlideTextContent(headline="Clarity beats scale", accent_keyword="Clarity", **text),
    )


def _pixels(png: bytes) -> list:
    return list(Image.open(io.BytesIO(png)).convert("RGB").getdata())


# ── The icon list ────────────────────────────────────────────────────────────

def test_the_icon_list_is_large_and_searchable():
    all_icons = icons.list_icons()
    assert len(all_icons) > 1000
    sample = next(i for i in all_icons if i["name"] == "rocket")
    assert isinstance(sample["cp"], int)
    assert isinstance(sample["tags"], list)


def test_known_and_unknown_icons():
    assert icons.is_known_icon("sparkles")
    assert icons.is_known_icon("rocket")
    assert not icons.is_known_icon("not-a-real-icon")
    assert not icons.is_known_icon("")
    assert not icons.is_known_icon(None)


def test_each_icon_is_one_character_and_the_default_exists():
    assert len(icons.icon_char("rocket")) == 1
    assert icons.is_known_icon(icons.DEFAULT_ACCENT_ICON)


def test_every_listed_icon_can_be_loaded_as_a_character():
    for item in icons.list_icons():
        assert icons.icon_char(item["name"]) == chr(item["cp"])


# ── Rendering ────────────────────────────────────────────────────────────────

def test_an_icon_changes_the_image():
    assert _pixels(_render()) != _pixels(_render(icon_name="rocket"))


def test_different_icons_draw_differently():
    assert _pixels(_render(icon_name="rocket")) != _pixels(_render(icon_name="star"))


def test_the_illustration_accent_changes_the_image():
    plain = _render()
    with_accent = _render(illustration_accent=True)
    assert _pixels(plain) != _pixels(with_accent)


def test_the_illustration_accent_uses_the_chosen_icon_or_a_default():
    default_accent = _render(illustration_accent=True)
    rocket_accent = _render(icon_name="rocket", illustration_accent=True)
    assert _pixels(default_accent) != _pixels(rocket_accent)


def test_an_unknown_icon_is_ignored_and_never_crashes():
    assert _pixels(_render(icon_name="not-a-real-icon")) == _pixels(_render())


def test_the_output_is_still_a_normal_card_size():
    for text in ({}, {"icon_name": "rocket"}, {"illustration_accent": True}, {"icon_name": "star", "illustration_accent": True}):
        assert Image.open(io.BytesIO(_render(**text))).size == (1200, 1200)


@pytest.mark.parametrize("layout", list(LayoutPreset))
def test_icons_render_on_every_layout(layout):
    png = render_slide(
        layout=layout,
        base_image_bytes=None,
        brand_tokens=TOKENS,
        text_content=SlideTextContent(headline="Ship it", icon_name="rocket", illustration_accent=True),
    )
    assert Image.open(io.BytesIO(png)).size[0] > 0
