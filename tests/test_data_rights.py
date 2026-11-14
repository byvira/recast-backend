"""What a person agreed to at sign-up is recorded, an older app build can still sign up, and each use of a data right (a copy of
their data, deleting their account) leaves a record with no name on it that only staff can read."""
from __future__ import annotations

from app.db.mongo import get_db, users
from app.shared import data_requests
from app.shared.account_export import _clean
from tests.conftest import mark_otp_verified, unique_email, unique_username
from tests.test_ops_team import _master


async def _signup(client, **extra):
    email = unique_email()
    await mark_otp_verified(email)
    res = await client.post(
        "/api/v1/auth/signup", json={"identifier": email, "channel": "email", "name": "Consent Test", "username": unique_username(), **extra},
    )
    return email, res


async def test_signup_records_the_version_agreed_to(make_client):
    email, res = await _signup(make_client(), terms_version="2026-10-19")
    assert res.status_code == 200, res.text
    doc = await users.find_one({"email": email})
    assert len(doc["consents"]) == 1
    consent = doc["consents"][0]
    assert consent["kind"] == "terms_and_privacy" and consent["version"] == "2026-10-19" and consent["source"] == "signup"
    assert consent["at"] == doc["created_at"]


async def test_an_older_app_build_that_sends_no_version_still_signs_up(make_client):
    email, res = await _signup(make_client())
    assert res.status_code == 200, res.text
    assert "consents" not in await users.find_one({"email": email})


async def test_a_version_that_is_not_a_plain_label_is_refused(make_client):
    _, res = await _signup(make_client(), terms_version="<script>")
    assert res.status_code == 422
    _, res = await _signup(make_client(), terms_version="v" * 41)
    assert res.status_code == 422


def test_what_was_agreed_to_is_part_of_the_data_copy():
    consents = [{"kind": "terms_and_privacy", "version": "2026-10-19", "source": "signup"}]
    assert _clean({"id": "u1", "consents": consents}) == {"id": "u1", "consents": consents}


async def test_taking_a_copy_and_deleting_the_account_each_leave_a_record_with_no_name(signup_user):
    me, profile = await signup_user("Rights")
    who = data_requests.who_digest(profile)
    col = get_db()["data_requests"]

    assert (await me.get("/api/v1/users/me/export")).status_code == 200
    export_rows = await col.find({"who": who, "kind": "export"}).to_list(10)
    assert len(export_rows) == 1 and export_rows[0]["status"] == "done" and export_rows[0]["done_at"] >= export_rows[0]["asked_at"]

    assert (await me.request("DELETE", "/api/v1/users/me", json={"confirm": "DELETE"})).status_code == 200
    # the record outlives the account, and holds nothing that names the person
    rows = await col.find({"who": who}).to_list(10)
    assert sorted(r["kind"] for r in rows) == ["erase", "export"]
    for row in rows:
        text = " ".join(str(v) for v in row.values())
        assert profile["email"] not in text and profile["id"] not in text


async def test_only_staff_can_read_the_list_of_requests(signup_user):
    member, _ = await signup_user("Not Staff")
    assert (await member.get("/api/v1/users/me/export")).status_code == 200
    assert (await member.get("/api/v1/ops/people/data-requests")).status_code in (401, 403)

    master, _ = await _master(signup_user)
    res = await master.get("/api/v1/ops/people/data-requests", params={"limit": 5})
    assert res.status_code == 200, res.text
    rows = res.json()["requests"]
    assert rows and len(rows) <= 5 and all(set(r) == {"id", "kind", "who", "asked_at", "done_at", "status"} for r in rows)
    assert [r["asked_at"] for r in rows] == sorted((r["asked_at"] for r in rows), reverse=True)


async def test_an_unknown_kind_is_never_written():
    import pytest

    with pytest.raises(ValueError):
        await data_requests.record("something else", {"id": "u1"})
