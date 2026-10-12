"""Spend that should not happen twice or at all: a campaign cadence that cannot loop, a batch that resumes where it stopped, and
a deleted campaign that stops spending."""
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.models.campaign import CampaignCadence
from app.pipelines.text import orchestrator
from tests.conftest import signup_new_user
from tests.test_campaigns import _create_brand, _valid_body


def test_a_cadence_must_be_manual_daily_or_weekly():
    assert CampaignCadence(frequency="weekly").frequency == "weekly"
    for bad in ("hourly", "every minute", ""):
        with pytest.raises(ValidationError):
            CampaignCadence(frequency=bad)


def test_days_per_batch_is_one_to_thirty():
    for ok in (1, 7, 30):
        assert CampaignCadence(days_per_batch=ok).days_per_batch == ok
    for bad in (0, -1, 31, 500):
        with pytest.raises(ValidationError):
            CampaignCadence(days_per_batch=bad)


async def test_a_batch_carries_on_from_the_first_missing_day(monkeypatch):
    made = []

    async def planner(prompt, *a, **k):
        return {"angles": ["day one", "day two", "day three"]}

    async def day(**kwargs):
        made.append(kwargs["batch_day_index"])
        return SimpleNamespace(pieces=[])

    monkeypatch.setattr(orchestrator, "call_llm_structured", planner)
    monkeypatch.setattr(orchestrator, "run_text_pipeline", day)
    saved = []

    async def on_day(i, result):
        saved.append(i)

    await orchestrator.run_batch_pipeline(
        topic_cluster="t", platforms=[], brand_id="b", workspace_id="w", user_id="u", extras=None, days=3, start_day=2,
        on_day_complete=on_day,
    )
    assert made == [2] and saved == [2]  # days 0 and 1 were already saved by the failed attempt and are not paid for again


async def test_a_day_by_day_platform_list_is_checked_when_the_campaign_is_made(api_client):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    for bad, text in ((["NotAPlatform"], "Invalid platform"), ([], "no platform")):
        body = _valid_body(brand_id, platforms=["LinkedIn"], platforms_by_day=[["LinkedIn"], bad])
        res = await api_client.post("/api/v1/campaigns/", json=body)
        assert res.status_code == 400 and text in res.text, res.text


async def test_an_automatic_campaign_is_not_due_the_instant_it_is_made(api_client):
    from datetime import datetime, timezone

    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    body = _valid_body(brand_id, platforms=["LinkedIn"], cadence={"frequency": "daily", "days_per_batch": 2})
    res = await api_client.post("/api/v1/campaigns/", json=body)
    assert res.status_code == 201, res.text
    due = res.json()["cadence"]["next_run_at"]
    assert due and datetime.fromisoformat(due.replace("Z", "+00:00")) > datetime.now(timezone.utc)
