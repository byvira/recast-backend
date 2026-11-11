"""Someone who already has an account and types their address a little differently signs in to it; no second account is made."""

import pytest

from app.core.otp import account_key
from app.db.mongo import users
from tests.conftest import read_otp_code, signup_new_user, unique_email, unique_username


@pytest.mark.parametrize("typed, key", [
    ("Jane.Doe@Gmail.com", "janedoe@gmail.com"),
    ("jane.doe+news@gmail.com", "janedoe@gmail.com"),
    ("j.a.n.e.d.o.e@googlemail.com", "janedoe@gmail.com"),
    ("  JANEDOE@gmail.com ", "janedoe@gmail.com"),
    ("jane.doe+x@outlook.com", "jane.doe@outlook.com"),   # only gmail ignores dots
    ("jane.doe+x@company.com", "jane.doe+x@company.com"),  # an unknown provider: the address stays exactly as typed (lower case)
    ("+919876543210", "+919876543210"),
])
def test_the_account_key_ignores_spellings_that_reach_the_same_inbox(typed, key):
    assert account_key(typed) == key


async def _otp_for(client, identifier: str):
    assert (await client.post("/api/v1/auth/request-otp", json={"identifier": identifier, "channel": "email"})).status_code == 200
    code = await read_otp_code(identifier)
    return await client.post("/api/v1/auth/verify-otp", json={"identifier": identifier, "otp": code, "channel": "email"})


async def test_a_different_spelling_of_an_existing_address_is_not_a_new_user(make_client):
    base = f"jane.{unique_username()}@gmail.com"
    first = make_client()
    created = await signup_new_user(first, email=base)

    variant = base.replace("jane.", "j.a.n.e.", 1).replace("@", "+offers@")
    second = make_client()
    verified = await _otp_for(second, variant)
    assert verified.status_code == 200 and verified.json()["is_new_user"] is False

    # signing up under the variant is refused
    refused = await second.post(
        "/api/v1/auth/signup", json={"identifier": variant, "channel": "email", "name": "Again", "username": unique_username()}
    )
    assert refused.status_code == 409

    # and logging in under the variant gives the account they already have
    logged_in = await second.post("/api/v1/auth/login", json={"identifier": variant, "channel": "email"})
    assert logged_in.status_code == 200
    assert logged_in.json()["user"]["id"] == created["id"]
    assert await users.count_documents({"email_key": account_key(base)}) == 1


async def test_an_address_that_is_really_different_still_makes_a_new_account(make_client):
    a = make_client()
    await signup_new_user(a, email=f"sam.{unique_username()}@gmail.com")
    other = make_client()
    verified = await _otp_for(other, unique_email())
    assert verified.json()["is_new_user"] is True


async def test_an_older_account_without_the_key_is_still_found(make_client):
    base = f"older.{unique_username()}@gmail.com"
    client = make_client()
    created = await signup_new_user(client, email=base)
    await users.update_one({"id": created["id"]}, {"$unset": {"email_key": ""}})  # as accounts made before this fix
    verified = await _otp_for(make_client(), base)
    assert verified.json()["is_new_user"] is False  # found by the exact address
