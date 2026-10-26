"""Campaigns and repurpose jobs are stored in the same collection but kept apart.

A campaign is a plan of posts over days; a repurpose is one source turned into posts once, on the Repurpose page. Each list
asks for one kind, a record saved before the kind existed counts as a campaign, and a repurpose can never be put on a schedule.
"""

from datetime import datetime, timezone
from uuid import uuid4

from app.db.mongo import get_campaigns_collection
from tests.conftest import signup_new_user
from tests.test_campaigns import _create_brand, _valid_body


async def _names(client, kind: str | None = None) -> list[str]:
    res = await client.get("/api/v1/campaigns/", params={"kind": kind} if kind else None)
    assert res.status_code == 200, res.text
    return [c["name"] for c in res.json()]


async def test_lists_keep_campaigns_and_repurpose_jobs_apart(api_client):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)

    res = await api_client.post("/api/v1/campaigns/", json=_valid_body(brand_id, name="Launch week"))
    assert res.status_code == 201, res.text
    assert res.json()["kind"] == "campaign"
    res = await api_client.post("/api/v1/campaigns/", json=_valid_body(brand_id, name="From my notes", kind="repurpose"))
    assert res.status_code == 201, res.text
    assert res.json()["kind"] == "repurpose"

    assert await _names(api_client, "campaign") == ["Launch week"]
    assert await _names(api_client, "repurpose") == ["From my notes"]
    assert sorted(await _names(api_client)) == ["From my notes", "Launch week"]


async def test_a_record_saved_without_a_kind_is_a_campaign(api_client):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    res = await api_client.post("/api/v1/campaigns/", json=_valid_body(brand_id, name="Made today"))
    workspace_id = res.json()["workspace_id"]

    now = datetime.now(timezone.utc)
    await get_campaigns_collection().insert_one({
        "id": str(uuid4()), "workspace_id": workspace_id, "brand_id": brand_id, "name": "Saved long ago",
        "topic_cluster": "an older brief", "platforms": ["LinkedIn"], "created_by": "someone",
        "created_at": now, "updated_at": now, "deleted": False,
    })

    assert sorted(await _names(api_client, "campaign")) == ["Made today", "Saved long ago"]
    assert await _names(api_client, "repurpose") == []


async def test_a_repurpose_is_always_made_once(api_client):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)

    res = await api_client.post(
        "/api/v1/campaigns/",
        json=_valid_body(brand_id, kind="repurpose", cadence={"frequency": "daily", "days_per_batch": 7}),
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["cadence"]["frequency"] == "manual"
    assert body["cadence"]["days_per_batch"] == 1
    assert body["cadence"]["next_run_at"] is None

    res = await api_client.patch(f"/api/v1/campaigns/{body['id']}", json={"cadence": {"frequency": "weekly", "days_per_batch": 1}})
    assert res.status_code == 400
    res = await api_client.patch(f"/api/v1/campaigns/{body['id']}", json={"cadence": {"frequency": "manual", "days_per_batch": 5}})
    assert res.status_code == 400
    # anything that keeps it a one-off is still allowed
    res = await api_client.patch(f"/api/v1/campaigns/{body['id']}", json={"name": "Renamed"})
    assert res.status_code == 200, res.text


async def test_search_sends_a_repurpose_to_the_repurpose_page(api_client):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    await api_client.post("/api/v1/campaigns/", json=_valid_body(brand_id, name="Zebra launch"))
    await api_client.post("/api/v1/campaigns/", json=_valid_body(brand_id, name="Zebra notes", kind="repurpose"))

    res = await api_client.get("/api/v1/search/", params={"q": "Zebra"})
    assert res.status_code == 200, res.text
    found = {i["title"]: i for i in res.json()["items"] if i["type"] in ("campaign", "repurpose")}
    assert found["Zebra launch"]["type"] == "campaign"
    assert found["Zebra launch"]["href"].startswith("/dashboard/campaigns/")
    assert found["Zebra notes"]["type"] == "repurpose"
    assert found["Zebra notes"]["href"] == "/dashboard/pipeline"
