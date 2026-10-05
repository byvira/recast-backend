"""A flagged picture holds a post for review, and a logo, mascot or added picture that could not be loaded is noted on the picture."""
import pytest

from app.api.v1 import image_assets as image_module
from app.models.image_asset import Layer
from app.pipelines.media.image_render import BrandTokens
from app.pipelines.publish.spine import review_reason

BRAND = BrandTokens(primary_hex="#312e81", secondary_hex="#0f172a", accent_hex="#f59e0b", heading_font="Poppins")


def test_a_post_with_a_flagged_picture_needs_review_and_says_why():
    piece = {"quality_passed": True, "media": [{"id": "m1", "qa_flagged": True}]}
    assert review_reason(piece) == "The attached picture was flagged for review."


def test_a_post_with_clean_media_or_none_needs_no_review_on_that_account():
    assert review_reason({"quality_passed": True, "media": [{"id": "m1"}]}) is None
    assert review_reason({"quality_passed": True, "media": []}) is None
    assert review_reason({"quality_passed": True}) is None


def test_the_posts_own_flags_are_reported_before_the_pictures():
    assert review_reason({"flagged_for_review": True, "media": [{"qa_flagged": True}]}) == "This post was flagged for review."
    assert review_reason({"quality_passed": False, "media": []}) == "This post didn't pass the quality check."


async def _draw_with_nothing_loaded(monkeypatch, layers):
    stored: dict = {}

    async def _no_assets(layers_, brand, workspace_id):
        return {}

    async def _capture(data, **kwargs):
        stored.update(kwargs)
        return object()

    monkeypatch.setattr(image_module, "_fetch_layer_assets", _no_assets)
    monkeypatch.setattr(image_module, "_store_image", _capture)
    await image_module._draw_and_store(
        size=(400, 500), background_bytes=None, layers=layers, brand={}, brand_tokens=BRAND, workspace_id="w1", user_id="u1",
    )
    return stored


async def test_a_logo_that_could_not_be_loaded_is_noted_on_the_picture(monkeypatch):
    stored = await _draw_with_nothing_loaded(monkeypatch, [Layer(id="logo1", type="logo")])
    assert "The brand logo couldn't be loaded, so it is missing from this picture." in stored["flagged_reason"]


async def test_each_kind_of_missing_picture_is_named(monkeypatch):
    stored = await _draw_with_nothing_loaded(
        monkeypatch, [Layer(id="a", type="logo"), Layer(id="b", type="mascot"), Layer(id="c", type="image")],
    )
    reason = stored["flagged_reason"].lower()
    assert "the brand logo" in reason and "the mascot" in reason and "an added picture" in reason
    assert "couldn't be loaded, so it is missing from this picture" in reason


async def test_a_hidden_layer_that_is_missing_does_not_raise_a_note(monkeypatch):
    stored = await _draw_with_nothing_loaded(monkeypatch, [Layer(id="logo1", type="logo", hidden=True)])
    note = stored["flagged_reason"] or ""
    assert "couldn't be loaded" not in note


async def test_text_layers_alone_never_raise_a_missing_picture_note(monkeypatch):
    stored = await _draw_with_nothing_loaded(monkeypatch, [Layer(id="t1", type="text", text="Hello")])
    assert "couldn't be loaded" not in (stored["flagged_reason"] or "")


def test_the_starting_design_includes_the_logo_and_mascot_only_when_asked_for():
    from app.pipelines.media.image_layers import default_layers

    both = default_layers(size=(600, 750), brand=BRAND, headline="Hello", has_logo=True, has_mascot=True)
    assert {"logo", "mascot"} <= {layer.type for layer in both}
    neither = default_layers(size=(600, 750), brand=BRAND, headline="Hello")
    assert not ({"logo", "mascot"} & {layer.type for layer in neither})
    logo_only = default_layers(size=(600, 750), brand=BRAND, headline="Hello", has_logo=True)
    assert "logo" in {layer.type for layer in logo_only} and "mascot" not in {layer.type for layer in logo_only}
