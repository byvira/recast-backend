"""Packs: several images from one request. The prompt rules are pure; the route tests use real
Pillow rendering with the background provider and upload stubbed (same as test_image_assets)."""

import pytest

from app.api.v1 import image_assets as image_module
from app.pipelines.media import image_pack as pack
from tests.test_image_assets import _brand, _generate, _png, stubs  # noqa: F401 (stubs is a fixture)
from tests.conftest import create_workspace


def test_the_first_prompt_is_exactly_what_was_written():
    prompts = pack.pack_prompts("  a calm sunrise  ", 4)
    assert prompts[0] == "a calm sunrise"
    assert len(prompts) == 4


def test_later_prompts_are_different_from_each_other_and_keep_the_idea():
    prompts = pack.pack_prompts("a calm sunrise", 5)
    assert len(set(prompts)) == 5
    assert all("a calm sunrise" in p for p in prompts)
    assert "Variation 2 of 5" in prompts[1]


def test_one_image_is_just_the_prompt_and_counts_are_clamped():
    assert pack.pack_prompts("x", 1) == ["x"]
    assert pack.pack_prompts("x", None) == ["x"]
    assert pack.pack_prompts("x", 0) == ["x"]
    assert len(pack.pack_prompts("x", 99)) == pack.MAX_PACK_SIZE
    assert pack.clamp_pack_size(-3) == 1


def test_variations_never_run_out_for_a_large_pack():
    prompts = pack.pack_prompts("x", pack.MAX_PACK_SIZE)
    assert len(set(prompts)) == pack.MAX_PACK_SIZE


async def _setup(signup_user):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Pack WS")
    return client, ws_id, await _brand(client, ws_id)


async def test_a_pack_makes_that_many_real_images(signup_user, stubs):  # noqa: F811
    client, ws_id, brand_id = await _setup(signup_user)

    res = await _generate(client, ws_id, brand_id, count=3)
    assert res.status_code == 201, res.text
    slides = res.json()["slides"]
    assert [s["slide_number"] for s in slides] == [1, 2, 3]
    assert len({s["media_id"] for s in slides}) == 3
    assert len(stubs["uploads"]) == 6  # a clean picture and a finished picture for each of the 3 images
    # The background prompts really differ, so the pictures are not copies.
    assert len({b["prompt"] for b in stubs["backgrounds"]}) == 3
    assert stubs["backgrounds"][0]["prompt"] == "a calm sunrise over hills"


async def test_the_default_is_one_image(signup_user, stubs):  # noqa: F811
    client, ws_id, brand_id = await _setup(signup_user)
    res = await _generate(client, ws_id, brand_id)
    assert len(res.json()["slides"]) == 1
    assert len(stubs["uploads"]) == 2  # the clean picture and the finished picture


async def test_a_count_outside_the_range_is_rejected_before_any_work(signup_user, stubs):  # noqa: F811
    client, ws_id, brand_id = await _setup(signup_user)
    for bad in (0, 11, -1):
        assert (await _generate(client, ws_id, brand_id, count=bad)).status_code == 422
    assert stubs["uploads"] == []


async def test_a_failure_partway_keeps_what_was_made(signup_user, stubs, monkeypatch):  # noqa: F811
    client, ws_id, brand_id = await _setup(signup_user)
    calls = {"n": 0}

    async def flaky(*, prompt, workspace_id, user_id, target_size, brand_profile, avoid=None):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("provider quota used up")
        return _png(target_size)

    monkeypatch.setattr(image_module, "generate_image_from_prompt", flaky)

    res = await _generate(client, ws_id, brand_id, count=4)
    assert res.status_code == 201, res.text
    assert len(res.json()["slides"]) == 2, "images 1 and 2 were made before the failure and must be kept"
