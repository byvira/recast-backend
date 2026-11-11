"""Stage 6, picture audit rows T-10 (atomic version numbers) and I-08 (an unfinished set is marked until it is done)."""

import asyncio

import pytest
from fastapi import HTTPException

from app.api.v1 import image_assets as image_module
from app.db.mongo import image_asset_versions, image_assets
from app.models.image_asset import ImageAsset
from tests.test_image_assets import _generate, _h, _png, _setup, stubs  # noqa: F401 (stubs is a fixture)


async def test_overlapping_saves_each_take_their_own_version_number(signup_user, stubs):  # noqa: F811
    client, profile, ws_id, brand_id = await _setup(signup_user)
    asset = ImageAsset(**(await _generate(client, ws_id, brand_id)).json())
    start = (await image_assets.find_one({"id": asset.id}))["version_count"]

    runs = await asyncio.gather(*[
        image_module._bump_version(asset.id, ws_id, asset.slides, f"edit_{n}", profile["id"]) for n in range(8)
    ])
    assert all(r is not None for r in runs)
    assert (await image_assets.find_one({"id": asset.id}))["version_count"] == start + 8
    numbers = [v["version_number"] for v in await image_asset_versions.find({"image_asset_id": asset.id}).to_list(50)]
    assert len(numbers) == len(set(numbers)), "no two versions share a number"


async def test_two_saves_from_the_same_version_cannot_both_win(signup_user, stubs):  # noqa: F811
    client, profile, ws_id, brand_id = await _setup(signup_user)
    asset = ImageAsset(**(await _generate(client, ws_id, brand_id)).json())
    version = (await image_assets.find_one({"id": asset.id}))["version_count"]

    results = await asyncio.gather(
        *[image_module._bump_version(asset.id, ws_id, asset.slides, "edit", profile["id"], expected_version=version) for _ in range(2)],
        return_exceptions=True,
    )
    refused = [r for r in results if isinstance(r, HTTPException)]
    assert len(refused) == 1 and refused[0].status_code == 409
    assert (await image_assets.find_one({"id": asset.id}))["version_count"] == version + 1


async def test_a_set_says_it_is_unfinished_while_it_is_made_and_clears_when_whole(signup_user, stubs, monkeypatch):  # noqa: F811
    client, profile, ws_id, brand_id = await _setup(signup_user)
    seen: list = []

    original = image_module.generate_image_from_prompt

    async def watching(*, prompt, workspace_id, user_id, target_size, brand_profile, avoid=None):
        doc = await image_assets.find_one({"workspace_id": workspace_id})
        seen.append(doc.get("qa_flag_reason") if doc else None)
        return await original(prompt=prompt, workspace_id=workspace_id, user_id=user_id, target_size=target_size, brand_profile=brand_profile, avoid=avoid)

    monkeypatch.setattr(image_module, "generate_image_from_prompt", watching)
    res = await _generate(client, ws_id, brand_id, count=3)
    assert res.status_code == 201, res.text
    assert any(reason == "Still making the rest of this set." for reason in seen), "the set is marked while the rest is made"
    final = await image_assets.find_one({"id": res.json()["id"]})
    assert final["qa_flagged"] is False and not final.get("qa_flag_reason"), "the mark is gone once the whole set exists"
