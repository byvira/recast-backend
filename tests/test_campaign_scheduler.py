"""Tests for app.workers.campaign_scheduler.run_due_campaign_batches —
Phase 3's recurring auto-generation poller.

No existing test in this codebase exercises an APScheduler-registered job
through APScheduler itself (confirmed: process_scheduled_posts has no
such precedent either) — the established way this codebase tests a
worker-shaped function is to import and await it directly. "Due" is a
plain Mongo timestamp comparison against real datetime.now(), so no time-
mocking library is needed either: tests that need a campaign to be
not-yet-due, or overdue, just write cadence.next_run_at directly via
Mongo.

Runs with the LLM mocked — never a real Groq call.
"""

from datetime import datetime, timedelta, timezone

from app.db.mongo import content_pieces, get_campaigns_collection
from app.workers.campaign_scheduler import run_due_campaign_batches
from tests.conftest import signup_new_user


async def _create_brand(client) -> str:
    res = await client.post("/api/v1/brand/", json={"brand_type": "Person"})
    assert res.status_code in (200, 201), res.text
    brand_id = res.json()["brand_profile_id"]
    from app.db.mongo import brand_profiles
    await brand_profiles.update_one({"id": brand_id}, {"$set": {"is_complete": True}})
    return brand_id


async def _create_campaign(client, brand_id: str, **overrides) -> str:
    body = {
        "name": "Recurring Growth Push",
        "brand_id": brand_id,
        "topic_cluster": "B2B SaaS onboarding friction",
        "platforms": ["LinkedIn"],
        "cadence": {"frequency": "daily", "days_per_batch": 1},
    }
    body.update(overrides)
    res = await client.post("/api/v1/campaigns/", json=body)
    assert res.status_code == 201, res.text
    return res.json()["id"]


async def test_run_due_campaign_batches_generates_for_due_campaign(api_client, mock_llm):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    campaign_id = await _create_campaign(api_client, brand_id)
    # create_campaign already set next_run_at <= now for a daily cadence.

    mock_llm.set_structured({"angles": ["Only angle"]})
    mock_llm.set_plain("Real generated content.")

    await run_due_campaign_batches()

    tagged = await content_pieces.count_documents({"campaign_id": campaign_id})
    assert tagged > 0

    campaign = await get_campaigns_collection().find_one({"id": campaign_id})
    assert campaign["last_generated_at"] is not None
    # Motor returns naive UTC datetimes from Mongo — normalise before
    # comparing against an aware datetime.now(timezone.utc).
    next_run_at = campaign["cadence"]["next_run_at"].replace(tzinfo=timezone.utc)
    assert next_run_at > datetime.now(timezone.utc)


async def test_run_due_campaign_batches_skips_manual_campaigns(api_client, mock_llm):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    campaign_id = await _create_campaign(api_client, brand_id, cadence={"frequency": "manual", "days_per_batch": 1})

    mock_llm.set_structured({"angles": ["Only angle"]})
    mock_llm.set_plain("Real generated content.")

    await run_due_campaign_batches()

    tagged = await content_pieces.count_documents({"campaign_id": campaign_id})
    assert tagged == 0


async def test_run_due_campaign_batches_skips_paused_campaigns(api_client, mock_llm):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    campaign_id = await _create_campaign(api_client, brand_id)

    await api_client.patch(f"/api/v1/campaigns/{campaign_id}", json={"status": "paused"})

    mock_llm.set_structured({"angles": ["Only angle"]})
    mock_llm.set_plain("Real generated content.")

    await run_due_campaign_batches()

    tagged = await content_pieces.count_documents({"campaign_id": campaign_id})
    assert tagged == 0


async def test_run_due_campaign_batches_skips_not_yet_due_campaigns(api_client, mock_llm):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    campaign_id = await _create_campaign(api_client, brand_id)

    future = datetime.now(timezone.utc) + timedelta(days=1)
    await get_campaigns_collection().update_one(
        {"id": campaign_id}, {"$set": {"cadence.next_run_at": future}},
    )

    mock_llm.set_structured({"angles": ["Only angle"]})
    mock_llm.set_plain("Real generated content.")

    await run_due_campaign_batches()

    tagged = await content_pieces.count_documents({"campaign_id": campaign_id})
    assert tagged == 0


async def test_run_due_campaign_batches_continues_after_one_campaign_fails(api_client, mock_llm):
    await signup_new_user(api_client)
    good_brand_id = await _create_brand(api_client)
    bad_brand_id = await _create_brand(api_client)
    good_campaign_id = await _create_campaign(api_client, good_brand_id, name="Healthy Campaign")
    bad_campaign_id = await _create_campaign(api_client, bad_brand_id, name="Broken Campaign")

    # Force the second campaign's brand into a state generate_campaign_batch
    # will reject (ValueError) — simulates a brand that became incomplete
    # after the campaign was created.
    from app.db.mongo import brand_profiles
    await brand_profiles.update_one({"id": bad_brand_id}, {"$set": {"is_complete": False}})

    mock_llm.set_structured({"angles": ["Only angle"]})
    mock_llm.set_plain("Real generated content.")

    await run_due_campaign_batches()

    good_tagged = await content_pieces.count_documents({"campaign_id": good_campaign_id})
    bad_tagged = await content_pieces.count_documents({"campaign_id": bad_campaign_id})
    assert good_tagged > 0
    assert bad_tagged == 0
