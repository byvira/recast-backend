"""The Security tab: where an account is signed in (and ending any of those), and two-step sign-in with an authenticator app and
backup codes, from turning it on to signing in with it, with each code usable once."""
from __future__ import annotations

import time

import pyotp

from app.core import two_step
from app.db.mongo import users
from tests.conftest import mark_otp_verified, signup_new_user


async def _sign_in(make_client, email: str):
    client = make_client()
    await mark_otp_verified(email)
    return client, await client.post("/api/v1/auth/login", json={"identifier": email, "channel": "email"})


async def _turn_on(client) -> tuple[str, list[str]]:
    setup = (await client.post("/api/v1/auth/two-step/setup")).json()
    assert setup["uri"].startswith("otpauth://totp/") and "Ekavo" in setup["uri"]
    code = pyotp.TOTP(setup["secret"]).now()
    res = await client.post("/api/v1/auth/two-step/enable", json={"code": code})
    assert res.status_code == 200, res.text
    return setup["secret"], res.json()["backup_codes"]


async def test_sessions_list_every_sign_in_and_one_can_be_ended(make_client):
    first = make_client()
    profile = await signup_new_user(first)
    second, res = await _sign_in(make_client, profile["email"])
    assert res.status_code == 200

    mine = (await first.get("/api/v1/auth/sessions")).json()["sessions"]
    assert len(mine) == 2 and sum(s["current"] for s in mine) == 1
    assert mine[0]["current"] is True, "the device in use comes first"
    other = next(s for s in mine if not s["current"])
    assert other["device"] and "last_seen_at" in other

    # the one in use can't be ended from the list; another can, and then stops working
    own = next(s for s in mine if s["current"])
    assert (await first.delete(f"/api/v1/auth/sessions/{own['id']}")).status_code == 400
    assert (await second.get("/api/v1/users/me")).status_code == 200
    assert (await first.delete(f"/api/v1/auth/sessions/{other['id']}")).status_code == 200
    assert (await second.get("/api/v1/users/me")).status_code == 401
    assert len((await first.get("/api/v1/auth/sessions")).json()["sessions"]) == 1
    assert (await first.delete(f"/api/v1/auth/sessions/{other['id']}")).status_code == 404


async def test_nobody_can_end_someone_elses_session(make_client):
    a = make_client()
    await signup_new_user(a)
    b = make_client()
    await signup_new_user(b)
    theirs = (await b.get("/api/v1/auth/sessions")).json()["sessions"][0]["id"]
    assert (await a.delete(f"/api/v1/auth/sessions/{theirs}")).status_code == 404
    assert (await b.get("/api/v1/users/me")).status_code == 200


async def test_signing_out_everywhere_clears_the_list(make_client):
    first = make_client()
    profile = await signup_new_user(first)
    await _sign_in(make_client, profile["email"])
    assert (await first.post("/api/v1/auth/logout-all")).status_code == 200
    time.sleep(1.1)  # tokens carry whole seconds: a sign-in in the same second as the cutoff counts as before it
    again, res = await _sign_in(make_client, profile["email"])
    assert res.status_code == 200
    assert len((await again.get("/api/v1/auth/sessions")).json()["sessions"]) == 1


async def test_two_step_turns_on_with_a_code_and_sign_in_then_asks_for_it(make_client):
    client = make_client()
    profile = await signup_new_user(client)
    assert (await client.get("/api/v1/auth/two-step")).json() == {"enabled": False, "backup_left": 0, "enabled_at": None}
    # a wrong code changes nothing
    await client.post("/api/v1/auth/two-step/setup")
    assert (await client.post("/api/v1/auth/two-step/enable", json={"code": "000000"})).status_code == 400
    secret, backups = await _turn_on(client)
    assert len(backups) == 10 and len(set(backups)) == 10
    assert (await client.get("/api/v1/auth/two-step")).json()["backup_left"] == 10
    stored = await users.find_one({"id": profile["id"]})
    assert secret not in str(stored["two_step"]) and all(c not in str(stored["two_step"]) for c in backups), "nothing readable is stored"

    # sign in again: the emailed code is not enough, a ticket is given instead of a session
    other, res = await _sign_in(make_client, profile["email"])
    body = res.json()
    assert res.status_code == 200 and body["two_step_required"] is True and "access_token" not in body
    assert (await other.get("/api/v1/users/me")).status_code == 401
    # a wrong code is refused; the right one signs in
    bad = await other.post("/api/v1/auth/login/two-step", json={"challenge": body["challenge"], "code": "123456"})
    assert bad.status_code == 401
    # a code just used on turning it on can't be used again, so the next step's code is used
    totp = pyotp.TOTP(secret)
    await users.update_one({"id": profile["id"]}, {"$set": {"two_step.last_step": int(time.time() // 30) - 5}})
    good = await other.post("/api/v1/auth/login/two-step", json={"challenge": body["challenge"], "code": totp.now()})
    assert good.status_code == 200, good.text
    assert (await other.get("/api/v1/users/me")).status_code == 200
    assert len((await other.get("/api/v1/auth/sessions")).json()["sessions"]) == 2


async def test_a_code_works_once_and_the_ticket_ends_after_too_many_wrong_codes(make_client):
    client = make_client()
    profile = await signup_new_user(client)
    secret, _ = await _turn_on(client)
    await users.update_one({"id": profile["id"]}, {"$set": {"two_step.last_step": int(time.time() // 30) - 5}})
    other, res = await _sign_in(make_client, profile["email"])
    ticket = res.json()["challenge"]
    code = pyotp.TOTP(secret).now()
    assert (await other.post("/api/v1/auth/login/two-step", json={"challenge": ticket, "code": code})).status_code == 200
    # the same ticket and code again are refused (the ticket is used up)
    assert (await other.post("/api/v1/auth/login/two-step", json={"challenge": ticket, "code": code})).status_code == 401
    # the same code on a new ticket is refused too: a used code is never accepted twice
    third, res = await _sign_in(make_client, profile["email"])
    assert (await third.post("/api/v1/auth/login/two-step", json={"challenge": res.json()["challenge"], "code": code})).status_code == 401
    # five wrong codes end a ticket, even for the right one afterwards
    fourth, res = await _sign_in(make_client, profile["email"])
    ticket = res.json()["challenge"]
    for _ in range(5):
        await fourth.post("/api/v1/auth/login/two-step", json={"challenge": ticket, "code": "111111"})
    await users.update_one({"id": profile["id"]}, {"$set": {"two_step.last_step": 0}})
    assert (await fourth.post("/api/v1/auth/login/two-step", json={"challenge": ticket, "code": pyotp.TOTP(secret).now()})).status_code == 401


async def test_a_backup_code_signs_in_once_then_is_used_up(make_client):
    client = make_client()
    profile = await signup_new_user(client)
    _, backups = await _turn_on(client)
    other, res = await _sign_in(make_client, profile["email"])
    assert (await other.post("/api/v1/auth/login/two-step", json={"challenge": res.json()["challenge"], "code": backups[0]})).status_code == 200
    assert (await client.get("/api/v1/auth/two-step")).json()["backup_left"] == 9
    third, res = await _sign_in(make_client, profile["email"])
    assert (await third.post("/api/v1/auth/login/two-step", json={"challenge": res.json()["challenge"], "code": backups[0]})).status_code == 401
    # the spaced or capitalised way a person types it still works
    fourth, res = await _sign_in(make_client, profile["email"])
    typed = backups[1].upper().replace("-", " ")
    assert (await fourth.post("/api/v1/auth/login/two-step", json={"challenge": res.json()["challenge"], "code": typed})).status_code == 200


async def test_turning_it_off_and_new_backup_codes_need_a_code(make_client):
    client = make_client()
    profile = await signup_new_user(client)
    secret, backups = await _turn_on(client)
    assert (await client.post("/api/v1/auth/two-step/disable", json={"code": "999999"})).status_code == 400
    fresh = await client.post("/api/v1/auth/two-step/backup-codes", json={"code": backups[0]})
    assert fresh.status_code == 200 and len(fresh.json()["backup_codes"]) == 10
    assert set(fresh.json()["backup_codes"]).isdisjoint(backups)
    # the old codes no longer work, the new ones do
    assert (await client.post("/api/v1/auth/two-step/disable", json={"code": backups[1]})).status_code == 400
    assert (await client.post("/api/v1/auth/two-step/disable", json={"code": fresh.json()["backup_codes"][0]})).status_code == 200
    assert (await client.get("/api/v1/auth/two-step")).json()["enabled"] is False
    other, res = await _sign_in(make_client, profile["email"])
    assert "access_token" in res.json(), "with it off, signing in is one step again"


def test_the_matching_step_allows_a_little_clock_drift_and_nothing_older():
    secret = pyotp.random_base32()
    now_step = int(time.time() // 30)
    totp = pyotp.TOTP(secret)
    assert two_step._matching_step(secret, totp.at(now_step * 30), 0) == now_step
    assert two_step._matching_step(secret, totp.at((now_step - 1) * 30), 0) == now_step - 1
    assert two_step._matching_step(secret, totp.at((now_step - 3) * 30), 0) is None
    assert two_step._matching_step(secret, totp.at(now_step * 30), now_step) is None, "a step already used is refused"
    assert two_step._matching_step(secret, "12 34 5", 0) is None
