"""Tests for /api/v1/campaigns — Phase 1 of the "bulk campaigns" architecture.

A campaign groups multiple generation runs under one tracked entity, with
real aggregate progress computed from the pieces it generated. The
Campaign model previously existed as a dead scaffold with no API route
anywhere. Phase 1 is text-only, single-platform-set per campaign.

Runs with the LLM mocked — never a real Groq call.
"""

from tests.conftest import create_workspace, invite_and_accept, signup_new_user


async def _create_brand(client, ws_id: str | None = None) -> str:
    headers = {"X-Workspace-Id": ws_id} if ws_id else {}
    res = await client.post("/api/v1/brand/", json={"brand_type": "Person"}, headers=headers)
    assert res.status_code in (200, 201), res.text
    brand_id = res.json()["brand_profile_id"]
    from app.db.mongo import brand_profiles
    await brand_profiles.update_one({"id": brand_id}, {"$set": {"is_complete": True}})
    return brand_id


def _valid_body(brand_id: str, **overrides) -> dict:
    body = {
        "name": "Q1 Growth Push",
        "brand_id": brand_id,
        "topic_cluster": "B2B SaaS onboarding friction",
        "platforms": ["LinkedIn"],
    }
    body.update(overrides)
    return body


async def test_create_and_list_campaign(api_client):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    # default workspace — X-Workspace-Id omitted, matches other test files' pattern

    res = await api_client.post("/api/v1/campaigns/", json=_valid_body(brand_id))
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["name"] == "Q1 Growth Push"
    assert body["status"] == "draft"
    assert body["content_types"] == ["text"]
    assert body["piece_ids"] == []

    res = await api_client.get("/api/v1/campaigns/")
    assert res.status_code == 200
    names = [c["name"] for c in res.json()]
    assert "Q1 Growth Push" in names


async def test_create_campaign_404_for_nonexistent_brand(api_client):
    await signup_new_user(api_client)

    res = await api_client.post("/api/v1/campaigns/", json=_valid_body("does-not-exist"))
    assert res.status_code == 404


async def test_create_campaign_400_for_invalid_platform(api_client):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)

    res = await api_client.post(
        "/api/v1/campaigns/", json=_valid_body(brand_id, platforms=["NotARealPlatform"]),
    )
    assert res.status_code == 400


async def test_get_campaign_includes_progress(api_client):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    res = await api_client.post("/api/v1/campaigns/", json=_valid_body(brand_id))
    campaign_id = res.json()["id"]

    res = await api_client.get(f"/api/v1/campaigns/{campaign_id}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["progress"]["total"] == 0
    assert body["progress"]["drafting"] == 0


async def test_get_campaign_404_for_nonexistent(api_client):
    await signup_new_user(api_client)
    res = await api_client.get("/api/v1/campaigns/does-not-exist")
    assert res.status_code == 404


async def test_update_campaign(api_client):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    res = await api_client.post("/api/v1/campaigns/", json=_valid_body(brand_id))
    campaign_id = res.json()["id"]

    res = await api_client.patch(f"/api/v1/campaigns/{campaign_id}", json={"status": "paused"})
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "paused"


async def test_update_campaign_400_when_nothing_provided(api_client):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    res = await api_client.post("/api/v1/campaigns/", json=_valid_body(brand_id))
    campaign_id = res.json()["id"]

    res = await api_client.patch(f"/api/v1/campaigns/{campaign_id}", json={})
    assert res.status_code == 400


async def test_delete_campaign_is_soft(api_client):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    res = await api_client.post("/api/v1/campaigns/", json=_valid_body(brand_id))
    campaign_id = res.json()["id"]

    res = await api_client.delete(f"/api/v1/campaigns/{campaign_id}")
    assert res.status_code == 204

    res = await api_client.get(f"/api/v1/campaigns/{campaign_id}")
    assert res.status_code == 404


async def test_campaigns_are_scoped_to_workspace(api_client):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    res = await api_client.post("/api/v1/campaigns/", json=_valid_body(brand_id))
    campaign_id = res.json()["id"]

    ws_id = await create_workspace(api_client, "Other Workspace", tier="duo")
    res = await api_client.get(f"/api/v1/campaigns/{campaign_id}", headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 404


async def test_generate_next_batch_tags_pieces_and_updates_progress(api_client, mock_llm):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    res = await api_client.post(
        "/api/v1/campaigns/", json=_valid_body(brand_id, platforms=["LinkedIn"]),
    )
    campaign_id = res.json()["id"]

    mock_llm.set_structured({"angles": ["Angle one", "Angle two"]})
    mock_llm.set_plain("Real generated content for this campaign day.")

    # days_per_batch defaults to 7, but batch_angles only returned 2 angles —
    # run_batch_pipeline's angles[:days] slice just runs what's available,
    # same fallback behaviour test_batch_mode.py documents.
    res = await api_client.post(f"/api/v1/campaigns/{campaign_id}/generate-next-batch")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["pieces_generated_this_run"] > 0
    assert body["status"] == "active"
    assert body["progress"]["total"] == body["pieces_generated_this_run"]

    # Every generated piece is real and tagged with this campaign.
    from app.db.mongo import content_pieces
    tagged = await content_pieces.count_documents({"campaign_id": campaign_id})
    assert tagged == body["pieces_generated_this_run"]


async def test_generate_next_batch_respects_days_per_batch(api_client, mock_llm):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    res = await api_client.post(
        "/api/v1/campaigns/",
        json=_valid_body(brand_id, platforms=["LinkedIn"], cadence={"frequency": "manual", "days_per_batch": 1}),
    )
    campaign_id = res.json()["id"]

    mock_llm.set_structured({"angles": ["Only angle"]})
    mock_llm.set_plain("Real generated content.")

    res = await api_client.post(f"/api/v1/campaigns/{campaign_id}/generate-next-batch")
    assert res.status_code == 200, res.text
    # 1 day * 1 platform = exactly 1 piece.
    assert res.json()["pieces_generated_this_run"] == 1


async def test_generate_next_batch_404_for_nonexistent_campaign(api_client, mock_llm):
    await signup_new_user(api_client)
    res = await api_client.post("/api/v1/campaigns/does-not-exist/generate-next-batch")
    assert res.status_code == 404


async def test_generate_next_batch_400_when_brand_incomplete(api_client, mock_llm):
    await signup_new_user(api_client)
    res = await api_client.post("/api/v1/brand/", json={"brand_type": "Person"})
    brand_id = res.json()["brand_profile_id"]  # never marked complete

    res = await api_client.post("/api/v1/campaigns/", json=_valid_body(brand_id))
    campaign_id = res.json()["id"]

    res = await api_client.post(f"/api/v1/campaigns/{campaign_id}/generate-next-batch")
    assert res.status_code == 400


async def test_create_campaign_requires_create_content_permission(api_client, make_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Campaign Perms", tier="large")
    brand_id = await _create_brand(api_client, ws_id)
    viewer_client, _ = await invite_and_accept(api_client, make_client, ws_id, "viewer")

    res = await viewer_client.post(
        "/api/v1/campaigns/", json=_valid_body(brand_id), headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 403


async def test_generate_next_batch_requires_create_content_permission(api_client, make_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Campaign Perms", tier="large")
    brand_id = await _create_brand(api_client, ws_id)
    res = await api_client.post(
        "/api/v1/campaigns/", json=_valid_body(brand_id), headers={"X-Workspace-Id": ws_id},
    )
    campaign_id = res.json()["id"]

    viewer_client, _ = await invite_and_accept(api_client, make_client, ws_id, "viewer")
    res = await viewer_client.post(
        f"/api/v1/campaigns/{campaign_id}/generate-next-batch", headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 403
