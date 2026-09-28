"""Tests for the AI token budget actually being enforced.

The Quotas tab's "Monthly Token Budget" used to be a settable number that
nothing ever checked. Now app.agents.supervisor.service.assert_ai_budget_available
rejects new token-spending runs (Text generation incl. repurpose/batch/
campaigns, and Audio localization's translation) with a 403 once the last
30 days' usage reaches the cap — the same window the Quotas tab displays.
Image generation and TTS spend no LLM tokens, so they're deliberately exempt.

Real Mongo; usage rows are written directly (never a real LLM call).
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.agents.supervisor.service import assert_ai_budget_available
from app.api.v1 import audio_assets as audio_module
from app.db.mongo import (
    brand_profiles,
    get_campaigns_collection,
    workspace_ai_budgets,
    workspace_ai_usage_daily,
)
from fastapi import HTTPException
from tests.conftest import create_workspace


def _day(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


async def _set_cap(ws_id: str, cap) -> None:
    await workspace_ai_budgets.update_one(
        {"workspace_id": ws_id},
        {"$set": {"monthly_token_budget": cap},
         "$setOnInsert": {"id": ws_id, "workspace_id": ws_id}},
        upsert=True,
    )


async def _use(ws_id: str, tokens: int, days_ago: int = 0) -> None:
    date = _day(days_ago)
    await workspace_ai_usage_daily.update_one(
        {"_id": f"{ws_id}:{date}"},
        {"$inc": {"tokens_used": tokens, "calls": 1},
         "$setOnInsert": {"id": f"{ws_id}:{date}", "workspace_id": ws_id, "date": date}},
        upsert=True,
    )


async def _brand(client, ws_id: str) -> str:
    res = await client.post(
        "/api/v1/brand/", json={"brand_type": "Person"}, headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code in (200, 201), res.text
    brand_id = res.json()["brand_profile_id"]
    await brand_profiles.update_one({"id": brand_id}, {"$set": {"is_complete": True}})
    return brand_id


async def _regenerate(client, ws_id: str, brand_id: str):
    return await client.post(
        "/api/v1/text/regenerate",
        json={"platform": "LinkedIn", "brand_id": brand_id, "content": "Some source content."},
        headers={"X-Workspace-Id": ws_id},
    )


# ── the gate itself ──────────────────────────────────────────────────────────

async def test_no_budget_means_no_cap(signup_user):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Budget None")
    await _use(ws_id, 10_000_000)

    await assert_ai_budget_available(ws_id)  # no budget doc at all
    await _set_cap(ws_id, None)
    await assert_ai_budget_available(ws_id)  # explicit "no cap"


async def test_under_the_cap_is_allowed_and_at_or_over_is_rejected(signup_user):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Budget Cap")
    await _set_cap(ws_id, 1000)

    await _use(ws_id, 999)
    await assert_ai_budget_available(ws_id)

    await _use(ws_id, 1)  # exactly at the cap
    with pytest.raises(HTTPException) as exc:
        await assert_ai_budget_available(ws_id)
    assert exc.value.status_code == 403
    assert "1,000 of 1,000 tokens" in exc.value.detail
    assert "Quotas" in exc.value.detail


async def test_usage_older_than_the_window_does_not_count(signup_user):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Budget Window")
    await _set_cap(ws_id, 1000)

    await _use(ws_id, 5000, days_ago=45)  # outside the 30-day window
    await assert_ai_budget_available(ws_id)

    await _use(ws_id, 400, days_ago=29)  # inside it, sums with today's
    await _use(ws_id, 400, days_ago=0)
    await assert_ai_budget_available(ws_id)  # 800 < 1000

    await _use(ws_id, 200, days_ago=1)
    with pytest.raises(HTTPException):
        await assert_ai_budget_available(ws_id)  # 1000 >= 1000


async def test_one_workspaces_usage_never_counts_against_another(signup_user):
    client, _ = await signup_user()
    busy = await create_workspace(client, "Budget Busy")
    quiet = await create_workspace(client, "Budget Quiet")
    await _set_cap(busy, 100)
    await _set_cap(quiet, 100)
    await _use(busy, 5000)

    await assert_ai_budget_available(quiet)
    with pytest.raises(HTTPException):
        await assert_ai_budget_available(busy)


# ── through the real routes ──────────────────────────────────────────────────

async def test_text_generation_is_blocked_at_the_cap_and_resumes_when_raised(signup_user, mock_llm):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Budget Text")
    brand_id = await _brand(client, ws_id)
    mock_llm.set_plain("Regenerated content within budget.")
    mock_llm.set_structured({})

    await _set_cap(ws_id, 500)
    await _use(ws_id, 500)

    blocked = await _regenerate(client, ws_id, brand_id)
    assert blocked.status_code == 403, blocked.text  # not a masked 500
    assert "AI budget" in blocked.json()["detail"]

    await _set_cap(ws_id, 5000)  # an owner raises it
    assert (await _regenerate(client, ws_id, brand_id)).status_code == 200


async def test_the_owner_can_set_the_budget_through_the_real_endpoint(signup_user, mock_llm):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Budget PUT")
    brand_id = await _brand(client, ws_id)
    headers = {"X-Workspace-Id": ws_id}

    res = await client.put("/api/v1/ops/ai/budget", json={"monthly_token_budget": 100}, headers=headers)
    assert res.status_code == 200, res.text
    await _use(ws_id, 100)

    assert (await _regenerate(client, ws_id, brand_id)).status_code == 403

    usage = (await client.get("/api/v1/ops/ai/usage", headers=headers)).json()
    assert usage["total_tokens"] == 100
    assert usage["enforced"] is True
    assert any(c.startswith("text.generation") for c in usage["metered_coverage"])


async def test_localization_is_gated_and_attributed(signup_user, monkeypatch):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Budget Localize")
    await _set_cap(ws_id, 10)
    await _use(ws_id, 10)

    monkeypatch.setattr(audio_module, "is_language_supported", lambda language: (True, ""))
    res = await client.post(
        "/api/v1/audio-assets/anything/localize", json={"target_language": "French"},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 403, res.text
    assert "AI budget" in res.json()["detail"]


async def test_image_generation_is_exempt_because_it_spends_no_tokens(signup_user):
    """An exhausted token budget must not block Image generation: it gets past
    the gate and fails later for its own reason (unknown brand -> 404)."""
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Budget Image")
    await _set_cap(ws_id, 1)
    await _use(ws_id, 1)

    res = await client.post(
        "/api/v1/image-assets/generate",
        json={"title": "t", "brand_id": "no-such-brand", "prompt": "p", "headline": "h"},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 404, res.text


async def test_scheduled_campaigns_back_off_with_a_readable_reason(signup_user, monkeypatch):
    from app.workers import campaign_scheduler

    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Budget Scheduler")
    await get_campaigns_collection().insert_one({
        "id": "budget-campaign", "workspace_id": ws_id, "created_by": "someone",
        "brand_id": "b", "name": "Weekly", "topic_cluster": "topic", "status": "active", "deleted": False,
        "cadence": {"frequency": "daily", "next_run_at": datetime.now(timezone.utc) - timedelta(minutes=5),
                    "failures": 0},
    })

    async def _over_budget(*args, **kwargs):
        raise HTTPException(status_code=403, detail="This workspace has used its AI budget.")

    monkeypatch.setattr(campaign_scheduler, "generate_campaign_batch", _over_budget)
    await campaign_scheduler.run_due_campaign_batches()

    doc = await get_campaigns_collection().find_one({"id": "budget-campaign"})
    assert doc["cadence"]["failures"] == 1
    assert doc["cadence"]["last_error"] == "This workspace has used its AI budget."  # no "403: " prefix
