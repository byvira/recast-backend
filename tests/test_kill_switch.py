"""Tests for Odette's emergency kill switch (POST /api/v1/supervisor/kill-switch).

It used to be pure frontend state with no backend at all. Now it's a real,
owner-only, persisted workspace flag that every Text/Audio/Image generation
entry point checks (app.agents.supervisor.service.assert_generation_allowed).

These go through the real HTTP layer on purpose — a direct call to the guard
passed even while every Text route was quietly turning its 403 into a generic
500 ("except Exception" around run_text_pipeline).
"""

from datetime import datetime, timedelta, timezone

from app.db.mongo import brand_profiles, get_campaigns_collection
from tests.conftest import create_workspace, invite_and_accept

_PAUSED = "Generation is paused"


async def _create_brand(client, ws_id: str) -> str:
    res = await client.post(
        "/api/v1/brand/", json={"brand_type": "Person"}, headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code in (200, 201), res.text
    brand_id = res.json()["brand_profile_id"]
    await brand_profiles.update_one({"id": brand_id}, {"$set": {"is_complete": True}})
    return brand_id


async def _set_halted(client, ws_id: str, halted: bool):
    return await client.post(
        "/api/v1/supervisor/kill-switch", json={"halted": halted}, headers={"X-Workspace-Id": ws_id},
    )


async def test_default_is_not_halted(signup_user):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "KS Default")

    res = await client.get("/api/v1/supervisor/dashboard", headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 200, res.text
    assert res.json()["generation_halted"] is False


async def test_owner_can_arm_and_disarm_and_state_persists(signup_user):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "KS Toggle")

    res = await _set_halted(client, ws_id, True)
    assert res.status_code == 200, res.text
    assert res.json()["generation_halted"] is True

    dash = await client.get("/api/v1/supervisor/dashboard", headers={"X-Workspace-Id": ws_id})
    assert dash.json()["generation_halted"] is True

    res = await _set_halted(client, ws_id, False)
    assert res.json()["generation_halted"] is False
    dash = await client.get("/api/v1/supervisor/dashboard", headers={"X-Workspace-Id": ws_id})
    assert dash.json()["generation_halted"] is False


async def test_non_owner_cannot_arm_it(signup_user, make_client):
    owner, _ = await signup_user()
    ws_id = await create_workspace(owner, "KS RBAC", tier="duo")
    editor, _ = await invite_and_accept(owner, make_client, ws_id, "editor")

    res = await _set_halted(editor, ws_id, True)
    assert res.status_code == 403, res.text

    dash = await owner.get("/api/v1/supervisor/dashboard", headers={"X-Workspace-Id": ws_id})
    assert dash.json()["generation_halted"] is False


async def test_text_routes_return_403_not_a_masked_500_when_halted(signup_user, mock_llm):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "KS Text")
    brand_id = await _create_brand(client, ws_id)
    headers = {"X-Workspace-Id": ws_id}

    await _set_halted(client, ws_id, True)

    repurpose = await client.post(
        "/api/v1/text/repurpose",
        json={
            "source_content": "Most founders overestimate scale.",
            "source_platform": "LinkedIn",
            "target_platforms": ["Twitter/X"],
            "brand_id": brand_id,
        },
        headers=headers,
    )
    assert repurpose.status_code == 403, repurpose.text
    assert _PAUSED in repurpose.json()["detail"]

    regenerate = await client.post(
        "/api/v1/text/regenerate",
        json={"platform": "LinkedIn", "brand_id": brand_id, "content": "Some source content."},
        headers=headers,
    )
    assert regenerate.status_code == 403, regenerate.text
    assert _PAUSED in regenerate.json()["detail"]


async def test_text_generation_works_again_once_disarmed(signup_user, mock_llm):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "KS Text Resume")
    brand_id = await _create_brand(client, ws_id)
    headers = {"X-Workspace-Id": ws_id}
    mock_llm.set_plain("Regenerated content after the switch was released.")
    mock_llm.set_structured({})

    await _set_halted(client, ws_id, True)
    await _set_halted(client, ws_id, False)

    res = await client.post(
        "/api/v1/text/regenerate",
        json={"platform": "LinkedIn", "brand_id": brand_id, "content": "Some source content."},
        headers=headers,
    )
    assert res.status_code == 200, res.text


async def test_image_and_audio_generation_return_403_when_halted(signup_user):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "KS Media")
    headers = {"X-Workspace-Id": ws_id}
    await _set_halted(client, ws_id, True)

    image = await client.post(
        "/api/v1/image-assets/generate",
        json={"title": "t", "brand_id": "any", "prompt": "a calm sunrise", "headline": "Calm"},
        headers=headers,
    )
    assert image.status_code == 403, image.text
    assert _PAUSED in image.json()["detail"]

    audio = await client.post(
        "/api/v1/audio-assets/generate",
        json={"title": "t", "brand_id": "any", "script": "Hello there."},
        headers=headers,
    )
    assert audio.status_code == 403, audio.text

    dialogue = await client.post(
        "/api/v1/audio-assets/dialogue",
        json={"title": "t", "brand_id": "any", "turns": [{"speaker": "A", "text": "Hi."}]},
        headers=headers,
    )
    assert dialogue.status_code == 403, dialogue.text

    localize = await client.post(
        "/api/v1/audio-assets/does-not-matter/localize",
        json={"target_language": "French"},
        headers=headers,
    )
    assert localize.status_code == 403, localize.text


async def test_halt_is_scoped_to_its_own_workspace(signup_user):
    client, _ = await signup_user()
    halted_ws = await create_workspace(client, "KS Halted")
    other_ws = await create_workspace(client, "KS Other")
    await _set_halted(client, halted_ws, True)

    res = await client.get("/api/v1/supervisor/dashboard", headers={"X-Workspace-Id": other_ws})
    assert res.json()["generation_halted"] is False


async def test_scheduler_skips_a_halted_workspace_without_backing_off(signup_user, monkeypatch):
    """A paused workspace is not a failing campaign: the due-campaign job
    must leave it untouched (no backoff, no failure count) so it runs on the
    first tick after the owner disarms."""
    from app.workers import campaign_scheduler

    client, _ = await signup_user()
    ws_id = await create_workspace(client, "KS Scheduler")
    await _set_halted(client, ws_id, True)

    due_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    await get_campaigns_collection().insert_one({
        "id": "ks-campaign", "workspace_id": ws_id, "created_by": "someone",
        "brand_id": "b", "name": "Paused campaign", "topic_cluster": "topic",
        "status": "active", "deleted": False,
        "cadence": {"frequency": "daily", "next_run_at": due_at, "failures": 0},
    })

    calls: list[str] = []

    async def _should_not_run(*args, **kwargs):
        calls.append("ran")

    monkeypatch.setattr(campaign_scheduler, "generate_campaign_batch", _should_not_run)

    await campaign_scheduler.run_due_campaign_batches()

    assert calls == [], "a halted workspace's campaign must not generate"
    doc = await get_campaigns_collection().find_one({"id": "ks-campaign"})
    assert doc["cadence"]["failures"] == 0
    assert "last_error" not in doc["cadence"]
    # Still due (not pushed into the future by a backoff).
    next_run = doc["cadence"]["next_run_at"]
    if next_run.tzinfo is None:
        next_run = next_run.replace(tzinfo=timezone.utc)
    assert next_run < datetime.now(timezone.utc)
