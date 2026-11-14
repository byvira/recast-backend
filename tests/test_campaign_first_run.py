"""Weekly drafts: a repeating campaign can be given the day and hour of its first batch, and the server checks the time is a sensible one."""
from datetime import datetime, timedelta, timezone

from tests.conftest import signup_new_user
from tests.test_campaigns import _create_brand, _valid_body

WEEKLY = {"frequency": "weekly", "days_per_batch": 3}


def _when(**delta) -> str:
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat()


async def test_a_weekly_campaign_starts_at_the_time_chosen(api_client):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    chosen = datetime.now(timezone.utc) + timedelta(days=3, hours=2)
    res = await api_client.post("/api/v1/campaigns/", json=_valid_body(brand_id, cadence=WEEKLY, first_run_at=chosen.isoformat()))
    assert res.status_code == 201, res.text
    got = datetime.fromisoformat(res.json()["cadence"]["next_run_at"].replace("Z", "+00:00"))
    assert abs((got.replace(tzinfo=got.tzinfo or timezone.utc) - chosen).total_seconds()) < 2


async def test_without_a_chosen_time_it_is_due_a_few_minutes_after_creating(api_client):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    res = await api_client.post("/api/v1/campaigns/", json=_valid_body(brand_id, cadence=WEEKLY))
    got = datetime.fromisoformat(res.json()["cadence"]["next_run_at"].replace("Z", "+00:00"))
    soon = (got.replace(tzinfo=got.tzinfo or timezone.utc) - datetime.now(timezone.utc)).total_seconds()
    assert 0 < soon <= 11 * 60


async def test_a_time_in_the_past_or_too_far_ahead_or_on_a_manual_campaign_is_refused(api_client):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    post = lambda **over: api_client.post("/api/v1/campaigns/", json=_valid_body(brand_id, **over))
    assert (await post(cadence=WEEKLY, first_run_at=_when(hours=-1))).status_code == 422
    assert (await post(cadence=WEEKLY, first_run_at=_when(minutes=2))).status_code == 422          # sooner than the grace period
    assert (await post(cadence=WEEKLY, first_run_at=_when(days=15))).status_code == 422
    assert (await post(first_run_at=_when(days=2))).status_code == 422                              # manual campaigns do not repeat
