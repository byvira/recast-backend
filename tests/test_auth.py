"""Integration tests for the 7 Auth endpoints against a real (isolated) DB."""

import asyncio

import httpx

from app.main import app
from tests.conftest import (
    mark_otp_verified,
    read_otp_code,
    signup_new_user,
    unique_email,
    unique_username,
)


async def test_request_otp_then_verify_succeeds(api_client):
    email = unique_email()
    res = await api_client.post(
        "/api/v1/auth/request-otp", json={"identifier": email, "channel": "email"}
    )
    assert res.status_code == 200
    assert res.json()["cooldown_seconds"] == 60

    otp = await read_otp_code(email)
    res = await api_client.post(
        "/api/v1/auth/verify-otp",
        json={"identifier": email, "otp": otp, "channel": "email"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["valid"] is True
    assert body["is_new_user"] is True


async def test_verify_otp_wrong_code_rejected(api_client):
    email = unique_email()
    await api_client.post(
        "/api/v1/auth/request-otp", json={"identifier": email, "channel": "email"}
    )
    res = await api_client.post(
        "/api/v1/auth/verify-otp",
        json={"identifier": email, "otp": "000000", "channel": "email"},
    )
    assert res.status_code == 401


async def test_verify_otp_locks_after_max_attempts(api_client):
    email = unique_email()
    await api_client.post(
        "/api/v1/auth/request-otp", json={"identifier": email, "channel": "email"}
    )
    # OTP_MAX_ATTEMPTS defaults to 5 — exhaust them with a wrong code.
    last_status = None
    for _ in range(5):
        res = await api_client.post(
            "/api/v1/auth/verify-otp",
            json={"identifier": email, "otp": "000000", "channel": "email"},
        )
        last_status = res.status_code
    assert last_status == 423

    # Locked out even with the correct code now.
    otp = await read_otp_code(email)
    res = await api_client.post(
        "/api/v1/auth/verify-otp",
        json={"identifier": email, "otp": otp, "channel": "email"},
    )
    assert res.status_code == 423


async def test_signup_requires_verified_otp(api_client):
    email = unique_email()
    res = await api_client.post(
        "/api/v1/auth/signup",
        json={
            "identifier": email,
            "channel": "email",
            "name": "No Otp",
            "username": unique_username(),
        },
    )
    assert res.status_code == 401


async def test_signup_creates_account_and_sets_cookies(api_client):
    profile = await signup_new_user(api_client, name="Ada Lovelace")
    assert profile["name"] == "Ada Lovelace"
    assert profile["default_workspace_id"]
    assert "access_token" in api_client.cookies
    assert "refresh_token" in api_client.cookies


async def test_signup_duplicate_identifier_rejected(api_client, make_client):
    profile_client = make_client()
    email = unique_email()

    # First signup succeeds.
    await profile_client.post(
        "/api/v1/auth/request-otp", json={"identifier": email, "channel": "email"}
    )
    otp = await read_otp_code(email)
    await profile_client.post(
        "/api/v1/auth/verify-otp",
        json={"identifier": email, "otp": otp, "channel": "email"},
    )
    res = await profile_client.post(
        "/api/v1/auth/signup",
        json={
            "identifier": email,
            "channel": "email",
            "name": "First",
            "username": unique_username(),
        },
    )
    assert res.status_code == 200

    # A second signup attempt for the same identifier needs its own verified
    # OTP flag; set it directly rather than a real second request-otp round
    # trip, which would hit the 60s per-identifier cooldown immediately
    # after the first signup's own request-otp call.
    await mark_otp_verified(email)
    second_client = make_client()
    res = await second_client.post(
        "/api/v1/auth/signup",
        json={
            "identifier": email,
            "channel": "email",
            "name": "Second",
            "username": unique_username(),
        },
    )
    assert res.status_code == 409


async def test_login_without_account_returns_404(api_client):
    email = unique_email()
    await api_client.post(
        "/api/v1/auth/request-otp", json={"identifier": email, "channel": "email"}
    )
    otp = await read_otp_code(email)
    await api_client.post(
        "/api/v1/auth/verify-otp",
        json={"identifier": email, "otp": otp, "channel": "email"},
    )
    res = await api_client.post(
        "/api/v1/auth/login", json={"identifier": email, "channel": "email"}
    )
    assert res.status_code == 404


async def test_login_succeeds_for_existing_account(make_client):
    signup_client = make_client()
    profile = await signup_new_user(signup_client)
    identifier = profile["email"]

    login_client = make_client()
    await login_client.post(
        "/api/v1/auth/request-otp", json={"identifier": identifier, "channel": "email"}
    )
    otp = await read_otp_code(identifier)
    await login_client.post(
        "/api/v1/auth/verify-otp",
        json={"identifier": identifier, "otp": otp, "channel": "email"},
    )
    res = await login_client.post(
        "/api/v1/auth/login", json={"identifier": identifier, "channel": "email"}
    )
    assert res.status_code == 200
    assert res.json()["user"]["id"] == profile["id"]
    assert "access_token" in login_client.cookies


async def test_login_without_otp_verification_rejected(make_client):
    signup_client = make_client()
    profile = await signup_new_user(signup_client)

    login_client = make_client()
    res = await login_client.post(
        "/api/v1/auth/login", json={"identifier": profile["email"], "channel": "email"}
    )
    assert res.status_code == 401


async def test_refresh_rotates_tokens(api_client):
    await signup_new_user(api_client)
    old_refresh = api_client.cookies.get("refresh_token")

    # JWTs here carry no jti — iat/exp alone identify them, at 1-second
    # granularity. Minting a second token for the same user in the same
    # second reissues a byte-identical token (see final report: flagged as
    # a gap, not fixed in this module). Sleeping avoids that collision so
    # this test verifies actual rotation instead of tripping over it.
    await asyncio.sleep(1.1)

    res = await api_client.post("/api/v1/auth/refresh")
    assert res.status_code == 200
    body = res.json()
    assert body["access_token"]
    assert body["refresh_token"]
    assert api_client.cookies.get("refresh_token") != old_refresh

    # The old refresh token is blacklisted — replaying it must fail.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={"refresh_token": old_refresh},
    ) as client:
        res = await client.post("/api/v1/auth/refresh")
        assert res.status_code == 401


async def test_refresh_without_token_returns_400(api_client):
    res = await api_client.post("/api/v1/auth/refresh")
    assert res.status_code == 400


async def test_logout_requires_auth(api_client):
    res = await api_client.post("/api/v1/auth/logout")
    assert res.status_code == 401


async def test_logout_clears_session(api_client):
    await signup_new_user(api_client)
    res = await api_client.post("/api/v1/auth/logout")
    assert res.status_code == 200

    # The access token cookie was blacklisted — GET /users/me must now 401.
    res = await api_client.get("/api/v1/users/me")
    assert res.status_code == 401


async def test_check_username_available_and_taken(api_client):
    profile = await signup_new_user(api_client)
    taken_username = profile["username"]

    res = await api_client.get(f"/api/v1/auth/check-username/{unique_username()}")
    assert res.status_code == 200
    assert res.json()["available"] is True

    res = await api_client.get(f"/api/v1/auth/check-username/{taken_username}")
    assert res.status_code == 200
    body = res.json()
    assert body["available"] is False
    assert body["suggestion"]


async def test_logout_blacklists_refresh_token_against_replay(api_client):
    """Regression test for a bug found while writing this suite: logout's
    manual refresh-token blacklist decode omitted audience/issuer, so
    python-jose's JWTClaimsError on the `aud` claim was swallowed by a bare
    except and no blacklist entry was ever written — a "logged out" refresh
    token could still mint a fresh session indefinitely."""
    await signup_new_user(api_client)
    refresh_token = api_client.cookies.get("refresh_token")

    res = await api_client.post("/api/v1/auth/logout")
    assert res.status_code == 200

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={"refresh_token": refresh_token},
    ) as replay_client:
        res = await replay_client.post("/api/v1/auth/refresh")
        assert res.status_code == 401


async def test_check_username_rejects_invalid_format(api_client):
    res = await api_client.get("/api/v1/auth/check-username/a")
    assert res.status_code == 400
