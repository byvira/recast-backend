"""Audio Promotion Bundle: draft posts and quote pictures from the best moments of a recording. The price is shown first, nothing is
published, and no model or picture provider is reached (the moment finder, the text job and the picture run are stubbed)."""

import pytest

from app.api.v1 import audio_assets as audio_module
from app.db.mongo import audio_assets
from app.models.audio_asset import SuggestedClip
from tests.conftest import invite_and_accept
from tests.test_audio_assets import _h, _setup, stubs  # noqa: F401  (stubs is a fixture)


async def _recording(client, ws_id, brand_id, with_transcript=True) -> str:
    res = await client.post(
        "/api/v1/audio-assets/generate", json={"title": "Episode 1", "brand_id": brand_id, "script": "Welcome to the show."}, headers=_h(ws_id),
    )
    assert res.status_code == 201, res.text
    asset_id = res.json()["id"]
    words = [{"word": f"w{i}", "start_s": float(i), "end_s": i + 0.9} for i in range(40)] if with_transcript else []
    await audio_assets.update_one({"id": asset_id}, {"$set": {"transcript": words}})
    return asset_id


@pytest.fixture
def started(monkeypatch):
    """The moment finder, the text job and the picture run, recorded instead of run."""
    record = {"jobs": [], "pictures": [], "moments": [
        SuggestedClip(start_s=1, end_s=9, quote="Small steps still count.", reason="a clear point"),
        SuggestedClip(start_s=12, end_s=20, quote="Start before you feel ready.", reason="a strong line"),
        SuggestedClip(start_s=25, end_s=33, quote="x" * 300, reason="long"),
    ]}

    async def moments(doc, workspace_id, audio_asset_id):
        return record["moments"]

    async def submit(*, action_name, payload, ctx, client_key=None):
        record["jobs"].append({"action": action_name, "payload": payload})
        return {"id": f"run-{len(record['jobs'])}"}, False

    async def picture(body, ctx):
        record["pictures"].append(body)
        return {"id": f"pic-{len(record['pictures'])}"}

    from app.api.v1 import image_assets
    from app.shared import jobs

    monkeypatch.setattr(audio_module, "_generate_clip_suggestions", moments)
    monkeypatch.setattr(jobs, "submit", submit)
    monkeypatch.setattr(image_assets, "start_image_generation_run", picture)
    return record


async def test_the_estimate_shows_the_whole_price_before_anything_is_made(signup_user, stubs, started):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset_id = await _recording(client, ws_id, brand_id)
    res = await client.get(
        f"/api/v1/audio-assets/{asset_id}/promotion/estimate", params={"platform": "LinkedIn", "count": 3, "with_pictures": True}, headers=_h(ws_id),
    )
    assert res.status_code == 200, res.text
    plan = res.json()
    assert plan["credits"] == plan["finding_credits"] + plan["post_credits"] + plan["picture_credits"] and plan["picture_credits"] > 0
    without = (await client.get(
        f"/api/v1/audio-assets/{asset_id}/promotion/estimate", params={"platform": "LinkedIn", "count": 3, "with_pictures": False}, headers=_h(ws_id),
    )).json()
    assert without["picture_credits"] == 0 and without["credits"] < plan["credits"]
    assert started["jobs"] == [] and started["pictures"] == []


async def test_a_recording_with_no_text_cannot_start(signup_user, stubs, started):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset_id = await _recording(client, ws_id, brand_id, with_transcript=False)
    plan = (await client.get(
        f"/api/v1/audio-assets/{asset_id}/promotion/estimate", params={"platform": "LinkedIn"}, headers=_h(ws_id),
    )).json()
    assert plan["can_start"] is False and any("Transcribe" in b for b in plan["blockers"])


async def test_a_changed_price_stops_it_before_anything_is_made(signup_user, stubs, started):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset_id = await _recording(client, ws_id, brand_id)
    res = await client.post(
        f"/api/v1/audio-assets/{asset_id}/promotion",
        json={"brand_id": brand_id, "platform": "LinkedIn", "count": 2, "with_pictures": True, "confirm_credits": 1}, headers=_h(ws_id),
    )
    assert res.status_code == 409 and "credits" in res.json()["detail"]
    assert started["jobs"] == [] and started["pictures"] == []


async def test_it_makes_a_draft_post_and_a_picture_for_each_moment_and_publishes_nothing(signup_user, stubs, started):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset_id = await _recording(client, ws_id, brand_id)
    plan = (await client.get(
        f"/api/v1/audio-assets/{asset_id}/promotion/estimate", params={"platform": "LinkedIn", "count": 3, "with_pictures": True}, headers=_h(ws_id),
    )).json()
    res = await client.post(
        f"/api/v1/audio-assets/{asset_id}/promotion",
        json={"brand_id": brand_id, "platform": "LinkedIn", "count": 3, "with_pictures": True, "confirm_credits": plan["credits"]}, headers=_h(ws_id),
    )
    assert res.status_code == 202, res.text
    out = res.json()
    assert out["published"] is False and len(out["items"]) == 3
    assert [j["action"] for j in started["jobs"]] == ["text.generate"] * 3
    job = started["jobs"][0]["payload"]
    assert job["platforms"] == ["LinkedIn"] and job["publish_targets"] == [] and job["content"] == "Small steps still count."
    # the third moment is too long for a picture headline: it gets its post and a plain note
    assert len(started["pictures"]) == 2
    assert out["items"][2]["picture_run"] is None and any("too long" in n for n in out["items"][2]["notes"])
    assert out["items"][0]["post_run"] and out["items"][0]["picture_run"]


async def test_without_pictures_only_posts_are_made(signup_user, stubs, started):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset_id = await _recording(client, ws_id, brand_id)
    plan = (await client.get(
        f"/api/v1/audio-assets/{asset_id}/promotion/estimate", params={"platform": "LinkedIn", "count": 1, "with_pictures": False}, headers=_h(ws_id),
    )).json()
    res = await client.post(
        f"/api/v1/audio-assets/{asset_id}/promotion",
        json={"brand_id": brand_id, "platform": "LinkedIn", "count": 1, "with_pictures": False, "confirm_credits": plan["credits"]}, headers=_h(ws_id),
    )
    assert res.status_code == 202 and len(started["jobs"]) == 1 and started["pictures"] == []


async def test_viewers_and_other_workspaces_cannot_use_it(signup_user, make_client, stubs, started):
    owner, _, ws_id, brand_id = await _setup(signup_user)
    asset_id = await _recording(owner, ws_id, brand_id)
    viewer, _ = await invite_and_accept(owner, make_client, ws_id, "viewer")
    body = {"brand_id": brand_id, "platform": "LinkedIn", "count": 1, "with_pictures": False, "confirm_credits": 2}
    assert (await viewer.post(f"/api/v1/audio-assets/{asset_id}/promotion", json=body, headers=_h(ws_id))).status_code == 403
    other, _, other_ws, other_brand = await _setup(signup_user)
    assert (await other.get(
        f"/api/v1/audio-assets/{asset_id}/promotion/estimate", params={"platform": "LinkedIn"}, headers=_h(other_ws),
    )).status_code == 404
    assert started["jobs"] == []
