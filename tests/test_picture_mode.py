"""The picture each post gets: AI picture, the brand's own pictures, or none. A run's choice wins, then the brand's default, then an AI picture, and older callers that
only switch the picture off still mean "none". No picture is made and no credits are used."""
import pytest
from pydantic import ValidationError

from app.models.brand_profile import VisualIdentity
from app.models.text import ExtrasConfig
from app.pipelines.media import default_image
from app.pipelines.media.default_image import pick_default_image, resolve_picture_mode


def _brand(mode=None):
    return {"visual_identity": ({"default_picture_mode": mode} if mode else {}) | {"reference_media_ids": ["m1"]}}


def test_a_runs_own_choice_wins_over_everything():
    assert resolve_picture_mode(picture_mode="brand", default_picture=True, brand_profile=_brand("none")) == "brand"
    assert resolve_picture_mode(picture_mode="none", default_picture=True, brand_profile=_brand("ai")) == "none"
    assert resolve_picture_mode(picture_mode="ai", default_picture=False, brand_profile=_brand("none")) == "ai"


def test_switching_the_picture_off_the_old_way_still_means_none():
    assert resolve_picture_mode(picture_mode=None, default_picture=False, brand_profile=_brand("ai")) == "none"


def test_with_no_choice_the_brands_default_applies_and_an_ai_picture_when_it_has_none():
    assert resolve_picture_mode(picture_mode=None, default_picture=True, brand_profile=_brand("brand")) == "brand"
    assert resolve_picture_mode(picture_mode=None, default_picture=True, brand_profile=_brand("none")) == "none"
    assert resolve_picture_mode(picture_mode=None, default_picture=True, brand_profile=_brand()) == "ai"
    assert resolve_picture_mode(picture_mode=None, default_picture=True, brand_profile={}) == "ai"
    assert resolve_picture_mode(picture_mode="rubbish", default_picture=True, brand_profile=_brand("rubbish")) == "ai"


def test_a_choice_that_is_not_one_of_the_three_is_refused_by_the_models():
    assert ExtrasConfig(picture_mode="brand").picture_mode == "brand" and ExtrasConfig().picture_mode is None
    with pytest.raises(ValidationError):
        ExtrasConfig(picture_mode="sideways")
    assert VisualIdentity().default_picture_mode == "ai" and VisualIdentity(default_picture_mode="none").default_picture_mode == "none"
    with pytest.raises(ValidationError):
        VisualIdentity(default_picture_mode="sideways")


class _Calls:
    def __init__(self, monkeypatch, brand_asset, ai_asset):
        self.brand, self.ai = 0, 0

        async def pick_brand(ids, workspace_id):
            self.brand += 1
            return brand_asset

        async def make(**kw):
            self.ai += 1
            return ai_asset

        monkeypatch.setattr(default_image, "_pick_brand_asset", pick_brand)
        monkeypatch.setattr(default_image, "generate_brand_image", make)


async def _pick(mode):
    return await pick_default_image(piece_content="hello", brand_profile=_brand(), workspace_id="w1", user_id="u1", mode=mode)


async def test_none_makes_nothing_and_asks_for_nothing(monkeypatch):
    calls = _Calls(monkeypatch, "BRAND", "AI")
    assert await _pick("none") is None and (calls.brand, calls.ai) == (0, 0)


async def test_brand_only_ever_uses_the_brands_own_picture_and_never_makes_one(monkeypatch):
    calls = _Calls(monkeypatch, "BRAND", "AI")
    assert await _pick("brand") == "BRAND" and (calls.brand, calls.ai) == (1, 0)
    calls = _Calls(monkeypatch, None, "AI")
    assert await _pick("brand") is None and (calls.brand, calls.ai) == (1, 0)             # no credits are spent when it has none


async def test_ai_tries_the_brands_pictures_first_then_makes_one(monkeypatch):
    calls = _Calls(monkeypatch, "BRAND", "AI")
    assert await _pick("ai") == "BRAND" and calls.ai == 0
    calls = _Calls(monkeypatch, None, "AI")
    assert await _pick("ai") == "AI" and calls.ai == 1


# ── every way in carries the setting ─────────────────────────────────────────

def test_every_request_that_makes_posts_carries_the_picture_choice_and_the_tool_schema_shows_it():
    import json

    from pydantic import BaseModel

    import app.models.text as text_models
    from app.tools.registry import get_registry

    carrying = [c for c in vars(text_models).values() if isinstance(c, type) and issubclass(c, BaseModel) and "extras" in c.model_fields]
    assert len(carrying) >= 3                                          # generate, repurpose and batch
    assert all(c.model_fields["extras"].annotation is ExtrasConfig for c in carrying)
    assert "picture_mode" in json.dumps(get_registry().get("text.generate").input_schema())


def test_the_batch_runner_reads_the_campaigns_choice():
    import inspect

    from app.pipelines.campaigns import batch_runner

    assert "picture_mode" in inspect.getsource(batch_runner)
