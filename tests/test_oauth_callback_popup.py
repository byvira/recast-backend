"""Tests for the OAuth callback popup fix (FINDINGS.md R2-1).

Every OAuth callback route (meta/threads/google/generic-LinkedIn) used to
return a bare JSON dict on both success and every error branch. The popup
window openOAuthPopup() (Frontend/Recast/lib/api/social.connect.ts) opens
never reads that response body — it only polls popup.closed, then
re-fetches the real connected-accounts list — so the popup was left
permanently showing raw JSON with nothing to close it.

Scoped the same way tests/test_oauth_accounts.py already scopes itself:
the real success path talks to a third-party provider and isn't exercised
here. Every error branch below fails BEFORE any external call, so these
are real HTTP round-trips through the actual route, not mocks.
"""

from app.api.v1.oauth import _oauth_popup_response
from tests.conftest import create_workspace, signup_new_user

CLOSE_SCRIPT = "window.close()"


# ─────────────────────────────────────────────────────────────────────────────
# The shared helper itself
# ─────────────────────────────────────────────────────────────────────────────

def test_popup_response_success_is_html_with_close_script():
    resp = _oauth_popup_response(True, "linkedin connected successfully.")
    assert resp.status_code == 200
    assert resp.media_type == "text/html"
    body = resp.body.decode()
    assert CLOSE_SCRIPT in body
    assert "linkedin connected successfully." in body


def test_popup_response_error_is_html_with_close_script():
    resp = _oauth_popup_response(False, "Invalid or expired OAuth state.")
    assert resp.status_code == 400
    body = resp.body.decode()
    assert CLOSE_SCRIPT in body
    assert "Invalid or expired OAuth state." in body


def test_popup_response_escapes_message_html():
    """The message can echo a provider-supplied error query param —
    must never be reflected unescaped."""
    resp = _oauth_popup_response(False, "<script>alert(1)</script>")
    body = resp.body.decode()
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


# ─────────────────────────────────────────────────────────────────────────────
# Generic callback (covers LinkedIn) — error branches, real HTTP round-trip
# ─────────────────────────────────────────────────────────────────────────────

async def test_generic_callback_denied_returns_closing_html(api_client):
    await signup_new_user(api_client)
    res = await api_client.get(
        "/api/v1/oauth/linkedin/callback",
        params={"code": "x", "state": "x", "error": "access_denied"},
    )
    assert res.status_code == 400
    assert res.headers["content-type"].startswith("text/html")
    assert CLOSE_SCRIPT in res.text


async def test_generic_callback_invalid_state_returns_closing_html(api_client):
    await signup_new_user(api_client)
    res = await api_client.get(
        "/api/v1/oauth/linkedin/callback",
        params={"code": "x", "state": "not-a-real-state"},
    )
    assert res.status_code == 400
    assert CLOSE_SCRIPT in res.text
    assert "Invalid or expired OAuth state" in res.text


# ─────────────────────────────────────────────────────────────────────────────
# Meta / Threads / Google callbacks — error branches, real HTTP round-trip
# ─────────────────────────────────────────────────────────────────────────────

async def test_meta_callback_error_returns_closing_html(api_client):
    await signup_new_user(api_client)
    res = await api_client.get(
        "/api/v1/oauth/meta/callback",
        params={"error": "access_denied", "error_message": "User denied"},
    )
    assert res.status_code == 400
    assert CLOSE_SCRIPT in res.text
    assert "Meta OAuth error" in res.text


async def test_meta_callback_missing_code_returns_closing_html(api_client):
    await signup_new_user(api_client)
    res = await api_client.get("/api/v1/oauth/meta/callback")
    assert res.status_code == 400
    assert CLOSE_SCRIPT in res.text


async def test_threads_callback_denied_returns_closing_html(api_client):
    await signup_new_user(api_client)
    res = await api_client.get(
        "/api/v1/oauth/threads/callback", params={"error": "access_denied"},
    )
    assert res.status_code == 400
    assert CLOSE_SCRIPT in res.text
    assert "Threads OAuth denied" in res.text


async def test_google_callback_denied_returns_closing_html(api_client):
    await signup_new_user(api_client)
    res = await api_client.get(
        "/api/v1/oauth/google/callback", params={"error": "access_denied"},
    )
    assert res.status_code == 400
    assert CLOSE_SCRIPT in res.text
    assert "Google OAuth denied" in res.text


# ─────────────────────────────────────────────────────────────────────────────
# The pop-up reports back to the website that started the connection
# ─────────────────────────────────────────────────────────────────────────────

def test_the_popup_reports_to_the_website_that_started_the_connection(monkeypatch):
    from app.api.v1 import oauth
    from app.core.config import settings

    monkeypatch.setattr(settings, "FRONTEND_URL", "https://recastbyvira.vercel.app")
    token = oauth._popup_origin.set("https://recast.byvirastudio.com")
    try:
        body = oauth._oauth_popup_response(True, "Connected").body.decode()
    finally:
        oauth._popup_origin.reset(token)
    assert '"https://recast.byvirastudio.com"' in body and "recastbyvira.vercel.app" not in body


def test_without_a_remembered_website_the_popup_uses_the_frontend_address(monkeypatch):
    from app.api.v1 import oauth
    from app.core.config import settings

    monkeypatch.setattr(settings, "FRONTEND_URL", "https://recastbyvira.vercel.app/")
    body = oauth._oauth_popup_response(False, "Nope").body.decode()
    assert '"https://recastbyvira.vercel.app"' in body


def test_only_allowed_websites_are_remembered(monkeypatch):
    from app.api.v1 import oauth
    from app.core.config import settings

    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://recastbyvira.vercel.app")
    monkeypatch.setattr(settings, "ALLOWED_ORIGINS", [])
    assert oauth._known_origin("https://recast.byvirastudio.com") == "https://recast.byvirastudio.com"
    assert oauth._known_origin("https://recast.byvirastudio.com/") == "https://recast.byvirastudio.com"
    assert oauth._known_origin("https://evil.example.com") is None
    assert oauth._known_origin("") is None and oauth._known_origin(None) is None


async def test_the_website_survives_the_round_trip_through_the_saved_state(monkeypatch):
    from app.api.v1 import oauth
    from app.core.config import settings

    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://recastbyvira.vercel.app")
    state = await oauth._create_state("u1", "meta", "w1", "https://recast.byvirastudio.com")
    data = await oauth._consume_state(state)
    assert data["origin"] == "https://recast.byvirastudio.com"
    assert oauth._popup_origin.get() == "https://recast.byvirastudio.com"
    # A connection started from an address that is not allowed is remembered as nothing.
    state = await oauth._create_state("u1", "meta", "w1", "https://evil.example.com")
    await oauth._consume_state(state)
    assert oauth._popup_origin.get() is None
