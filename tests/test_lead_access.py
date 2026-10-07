"""Invite-only sign-up, one-time invite codes, expiry and reminders, unsubscribe, the daily digest and the big-team alert."""

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from app.db.mongo import lead_events, lead_settings, signup_invites, users, waitlist_leads
from app.shared import invites
from app.shared.lead_settings import save_lead_settings
from tests.conftest import read_otp_code, signup_new_user, unique_email, unique_username

L = "/api/v1/ops/leads"


@pytest.fixture(autouse=True)
async def _clean():
    await lead_settings.delete_many({})
    yield
    await lead_settings.delete_many({})
    await signup_invites.delete_many({"email": {"$regex": "@example.com$"}})
    await waitlist_leads.delete_many({"email": {"$regex": "@example.com$"}})
    await lead_events.delete_many({"actor_name": {"$in": ["Access Staff", "The person", "Sign-up", "Schedule"]}})


@pytest.fixture
def mail(monkeypatch):
    """Collects every templated email the invite code sends, so a test can read the code out of the link."""
    sent: list[tuple[str, str, dict]] = []

    async def fake(template, to, variables, from_override=None):
        sent.append((template, to, variables))
        return True

    monkeypatch.setattr(invites, "send_templated_email", fake)
    return sent


def _code(variables: dict) -> str:
    return parse_qs(urlparse(variables["INVITE_URL"]).query)["invite"][0]


async def _staff(make_client):
    client = make_client()
    user = await signup_new_user(client, name="Access Staff")
    await users.update_one({"id": user["id"]}, {"$set": {"is_platform_staff": True}})
    return client, user


async def _lead(api_client, **extra):
    email = unique_email()
    await api_client.post("/api/v1/waitlist", json={"email": email, "source": "test", **extra})
    return email, await waitlist_leads.find_one({"email": email.lower()})


async def _signup(client, email, code=None, verify=True):
    """The sign-up steps without the helper's assertions, so a refusal can be checked. A refused sign-up leaves the address verified,
    so a second try for the same address passes verify=False."""
    if verify:
        await client.post("/api/v1/auth/request-otp", json={"identifier": email, "channel": "email"})
        otp = await read_otp_code(email)
        await client.post("/api/v1/auth/verify-otp", json={"identifier": email, "otp": otp, "channel": "email"})
    body = {"identifier": email, "channel": "email", "name": "New Person", "username": unique_username()}
    if code:
        body["invite_code"] = code
    return await client.post("/api/v1/auth/signup", json=body)


async def test_sign_up_is_open_in_development_without_any_code(make_client):
    res = await _signup(make_client(), unique_email())
    assert res.status_code == 200


async def test_in_invite_mode_sign_up_needs_a_valid_code_for_that_address(make_client, api_client, mail):
    staff, _ = await _staff(make_client)
    email, lead = await _lead(api_client)
    await save_lead_settings({"signup_mode": "invite"})
    assert (await api_client.get("/api/v1/auth/signup-mode")).json() == {"mode": "invite"}
    assert (await _signup(make_client(), email)).status_code == 403

    sent = await staff.post(f"{L}/invites", json={"lead_ids": [lead["id"]]})
    assert sent.json()["sent"] == 1
    code = _code(mail[-1][2])
    assert (await _signup(make_client(), unique_email(), code)).status_code == 403  # a code for someone else's address

    ok = await _signup(make_client(), email, code, verify=False)
    assert ok.status_code == 200
    assert (await waitlist_leads.find_one({"id": lead["id"]}))["status"] == "activated"
    assert (await _signup(make_client(), unique_email(), code)).status_code == 403  # used once, never again


async def test_the_code_check_says_who_it_is_for(api_client, make_client, mail):
    staff, _ = await _staff(make_client)
    email, lead = await _lead(api_client)
    await staff.post(f"{L}/invites", json={"lead_ids": [lead["id"]]})
    code = _code(mail[-1][2])
    good = (await api_client.post("/api/v1/auth/invite/check", json={"code": code})).json()
    assert good == {"valid": True, "email": email.lower()}
    assert (await api_client.post("/api/v1/auth/invite/check", json={"code": "nope"})).json() == {"valid": False, "email": None}


async def test_only_a_hash_of_the_code_is_stored(api_client, make_client, mail):
    staff, _ = await _staff(make_client)
    _, lead = await _lead(api_client)
    await staff.post(f"{L}/invites", json={"lead_ids": [lead["id"]]})
    code = _code(mail[-1][2])
    stored = await signup_invites.find_one({"lead_id": lead["id"]})
    assert code not in str(stored) and stored["code_hash"] == invites.hash_code(code)


async def test_inviting_moves_a_lead_to_invited_and_cannot_be_repeated(api_client, make_client, mail):
    staff, _ = await _staff(make_client)
    _, lead = await _lead(api_client)
    first = (await staff.post(f"{L}/invites", json={"lead_ids": [lead["id"]]})).json()
    again = (await staff.post(f"{L}/invites", json={"lead_ids": [lead["id"]]})).json()
    assert first["sent"] == 1 and again["sent"] == 0 and "invited" in again["skipped"][0]["reason"]
    assert (await waitlist_leads.find_one({"id": lead["id"]}))["status"] == "invited"
    assert mail[-1][0] == "waitlist-invite" and "UNSUBSCRIBE_URL" in mail[-1][2]


async def test_the_daily_limit_stops_a_batch(api_client, make_client, mail):
    staff, _ = await _staff(make_client)
    await save_lead_settings({"daily_invite_cap": 1})
    _, first = await _lead(api_client)
    _, second = await _lead(api_client)
    res = (await staff.post(f"{L}/invites", json={"lead_ids": [first["id"], second["id"]]})).json()
    assert res["sent"] == 1 and res["left_today"] == 0 and "limit" in res["skipped"][0]["reason"]


async def test_an_email_that_fails_leaves_the_person_not_invited(api_client, make_client, monkeypatch):
    staff, _ = await _staff(make_client)
    _, lead = await _lead(api_client)

    async def fail(*args, **kwargs):
        return False

    monkeypatch.setattr(invites, "send_templated_email", fail)
    res = (await staff.post(f"{L}/invites", json={"lead_ids": [lead["id"]]})).json()
    assert res["sent"] == 0
    assert (await waitlist_leads.find_one({"id": lead["id"]}))["status"] == "joined"
    assert (await signup_invites.find_one({"lead_id": lead["id"]}))["revoked"] is True


async def test_staff_can_invite_someone_not_on_the_waitlist(make_client, mail):
    staff, _ = await _staff(make_client)
    email = unique_email()
    res = await staff.post(f"{L}/invites/direct", json={"email": email})
    assert res.status_code == 200
    lead = await waitlist_leads.find_one({"email": email.lower()})
    assert lead["source"] == "staff" and lead["status"] == "invited"


async def test_resending_replaces_the_old_code_and_revoking_ends_it(api_client, make_client, mail):
    staff, _ = await _staff(make_client)
    _, lead = await _lead(api_client)
    await staff.post(f"{L}/invites", json={"lead_ids": [lead["id"]]})
    old = _code(mail[-1][2])
    invite_id = (await signup_invites.find_one({"lead_id": lead["id"], "revoked": False}))["id"]
    assert (await staff.post(f"{L}/invites/{invite_id}/resend")).status_code == 200
    new = _code(mail[-1][2])
    assert (await invites.find_open_invite(old)) is None and (await invites.find_open_invite(new)) is not None
    new_id = (await signup_invites.find_one({"code_hash": invites.hash_code(new)}))["id"]
    assert (await staff.post(f"{L}/invites/{new_id}/revoke")).status_code == 200
    assert (await invites.find_open_invite(new)) is None


async def test_an_invite_that_runs_out_is_expired_by_the_sweep(api_client, make_client, mail):
    staff, _ = await _staff(make_client)
    _, lead = await _lead(api_client)
    await staff.post(f"{L}/invites", json={"lead_ids": [lead["id"]]})
    await signup_invites.update_one({"lead_id": lead["id"]}, {"$set": {"expires_at": datetime.now(timezone.utc) - timedelta(hours=1)}})
    result = await invites.sweep_invites()
    assert result["expired"] >= 1
    assert (await waitlist_leads.find_one({"id": lead["id"]}))["status"] == "expired"
    # and an expired person can be invited again
    again = (await staff.post(f"{L}/invites", json={"lead_ids": [lead["id"]]})).json()
    assert again["sent"] == 1


async def test_a_week_old_unused_invite_gets_one_reminder(api_client, make_client, mail):
    staff, _ = await _staff(make_client)
    _, lead = await _lead(api_client)
    await staff.post(f"{L}/invites", json={"lead_ids": [lead["id"]]})
    original = _code(mail[-1][2])
    await signup_invites.update_one({"lead_id": lead["id"]}, {"$set": {"sent_at": datetime.now(timezone.utc) - timedelta(days=8)}})
    first = await invites.sweep_invites()
    assert first["reminded"] == 1 and mail[-1][0] == "waitlist-reminder"
    reminder_code = _code(mail[-1][2])
    assert (await invites.find_open_invite(original)) is None and (await invites.find_open_invite(reminder_code)) is not None
    assert (await invites.sweep_invites())["reminded"] == 0  # only once


async def test_unsubscribing_needs_the_button_and_stops_every_email(api_client, make_client, mail):
    staff, _ = await _staff(make_client)
    email, lead = await _lead(api_client)
    token = invites.unsubscribe_token(lead["id"])
    info = (await api_client.get("/api/v1/waitlist/unsubscribe", params={"t": token})).json()
    assert info["email"].endswith("@example.com") and "***" in info["email"]
    assert (await waitlist_leads.find_one({"id": lead["id"]}))["status"] == "joined"  # opening the link changes nothing
    assert (await api_client.post("/api/v1/waitlist/unsubscribe", json={"token": token})).status_code == 200
    assert (await waitlist_leads.find_one({"id": lead["id"]}))["status"] == "unsubscribed"
    blocked = (await staff.post(f"{L}/invites", json={"lead_ids": [lead["id"]]})).json()
    assert blocked["sent"] == 0 and "unsubscribed" in blocked["skipped"][0]["reason"]


async def test_a_forged_unsubscribe_link_is_refused(api_client):
    _, lead = await _lead(api_client)
    forged = f"{lead['id']}.{'0' * 32}"
    assert (await api_client.post("/api/v1/waitlist/unsubscribe", json={"token": forged})).status_code == 404
    assert (await api_client.get("/api/v1/waitlist/unsubscribe", params={"t": "garbage"})).status_code == 404


async def test_the_daily_digest_goes_to_the_team_only_when_there_is_news(api_client, mail):
    await save_lead_settings({"digest_recipients": ["team@example.com"]})
    await _lead(api_client)
    assert await invites.lead_digest() is True
    digest = [m for m in mail if m[0] == "lead-digest"][-1]
    assert digest[1] == "team@example.com" and int(digest[2]["NEW_WAITLIST"]) >= 1
    await save_lead_settings({"digest_enabled": False})
    assert await invites.lead_digest() is False


async def test_a_team_of_six_or_more_alerts_the_team_once(api_client, mail):
    await save_lead_settings({"digest_recipients": ["team@example.com"]})
    email = unique_email()
    joined = (await api_client.post("/api/v1/waitlist", json={"email": email, "source": "test"})).json()
    code = joined["referral_code"]
    await api_client.put(f"/api/v1/waitlist/{code}/profile", json={"team_size": "solo"})
    assert not [m for m in mail if m[0] == "lead-alert"]
    await api_client.put(f"/api/v1/waitlist/{code}/profile", json={"team_size": "6-20", "role": "team"})
    await api_client.put(f"/api/v1/waitlist/{code}/profile", json={"team_size": "21+"})
    assert len([m for m in mail if m[0] == "lead-alert"]) == 1


async def test_settings_can_be_changed_by_staff_and_are_validated(make_client):
    staff, _ = await _staff(make_client)
    assert (await staff.put(f"{L}/settings", json={"signup_mode": "sometimes"})).status_code == 422
    saved = (await staff.put(f"{L}/settings", json={"signup_mode": "invite", "daily_invite_cap": 40, "digest_recipients": ["Team@Example.com", "team@example.com"]})).json()
    assert saved["effective_signup_mode"] == "invite" and saved["daily_invite_cap"] == 40 and saved["digest_recipients"] == ["team@example.com"]


async def test_only_staff_can_invite_or_change_settings(make_client, api_client):
    client = make_client()
    await signup_new_user(client, name="Not Staff")
    assert (await client.post(f"{L}/invites", json={"lead_ids": ["x"]})).status_code in (401, 403)
    assert (await client.put(f"{L}/settings", json={"signup_mode": "open"})).status_code in (401, 403)
    assert (await api_client.get(f"{L}/settings")).status_code in (401, 403)
