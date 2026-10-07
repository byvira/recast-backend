"""The public waitlist: joining, joining twice, referrals, the optional answers and the bot check."""

import pytest

from app.core.config import settings
from app.db.mongo import waitlist_leads
from tests.conftest import unique_email


@pytest.fixture(autouse=True)
async def _clean_waitlist():
    yield
    await waitlist_leads.delete_many({"email": {"$regex": "@example.com$"}})


async def _join(api_client, email, **extra):
    return await api_client.post("/api/v1/waitlist", json={"email": email, "source": "test", **extra})


async def test_join_returns_a_share_code_and_stores_the_lead(api_client):
    email = unique_email()
    res = await _join(api_client, email, utm={"source": "newsletter"})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "joined" and len(body["referral_code"]) >= 6
    lead = await waitlist_leads.find_one({"email": email.lower()})
    assert lead["source"] == "test" and lead["utm"] == {"source": "newsletter"}


async def test_joining_twice_is_not_an_error_and_does_not_hand_out_the_code_again(api_client):
    email = unique_email()
    first = await _join(api_client, email)
    second = await _join(api_client, email.upper())
    assert second.status_code == 200
    assert second.json() == {"status": "already_joined", "referral_code": ""}
    assert first.json()["referral_code"]


async def test_a_bad_email_is_refused(api_client):
    res = await _join(api_client, "not-an-email")
    assert res.status_code == 422


async def test_a_referral_counts_once_and_never_for_yourself(api_client):
    referrer = await _join(api_client, unique_email())
    code = referrer.json()["referral_code"]
    await _join(api_client, unique_email(), referral_code=code)
    lead = await waitlist_leads.find_one({"referral_code": code})
    assert lead["referral_count"] == 1
    # Joining with your own code is ignored: the address does not match itself.
    own = unique_email()
    mine = (await _join(api_client, own)).json()["referral_code"]
    await waitlist_leads.update_one({"referral_code": mine}, {"$set": {"referral_count": 0}})
    await _join(api_client, own, referral_code=mine)
    assert (await waitlist_leads.find_one({"referral_code": mine}))["referral_count"] == 0


async def test_the_optional_answers_are_saved_and_only_known_platforms_are_kept(api_client):
    code = (await _join(api_client, unique_email())).json()["referral_code"]
    res = await api_client.put(
        f"/api/v1/waitlist/{code}/profile",
        json={"role": "team", "team_size": "6-20", "platforms": ["linkedin", "made-up", "youtube"]},
    )
    assert res.status_code == 200
    lead = await waitlist_leads.find_one({"referral_code": code})
    assert lead["role"] == "team" and lead["team_size"] == "6-20" and lead["platforms"] == ["linkedin", "youtube"]


async def test_the_answers_need_a_real_share_code(api_client):
    res = await api_client.put("/api/v1/waitlist/not-a-code/profile", json={"role": "creator"})
    assert res.status_code == 404


async def test_the_bot_check_is_required_once_a_secret_is_set(api_client, monkeypatch):
    monkeypatch.setattr(settings, "TURNSTILE_SECRET_KEY", "secret")
    res = await _join(api_client, unique_email())
    assert res.status_code == 400
    assert "security check" in res.json()["detail"].lower()
