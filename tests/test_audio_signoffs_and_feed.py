"""Tests for real audio-asset signoffs and the real per-brand podcast RSS
feed — both replace decorative UI that had nothing behind it: 4 checkboxes
that were plain local state, and "Direct RSS Feed Hosting"/"Spotify & Apple
Podcasts Direct Dispatch" badges hardcoded to always show "Connected".
"""
from uuid import uuid4

import pytest

from app.db.mongo import audio_assets
from tests.conftest import invite_and_accept
from tests.test_audio_assets import _brand, _generate, _h, _setup, stubs  # noqa: F401 — fixture reuse


# ── signoffs ───────────────────────────────────────────────────────────────

async def test_signing_off_records_the_real_caller_and_is_idempotent(signup_user, stubs):
    client, profile, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()
    base = f"/api/v1/audio-assets/{asset['id']}"

    res = await client.post(f"{base}/signoff", json={"role": "audio_engineer"}, headers=_h(ws_id))
    assert res.status_code == 200, res.text
    signoffs = res.json()["signoffs"]
    assert len(signoffs) == 1
    assert signoffs[0]["role"] == "audio_engineer"
    assert signoffs[0]["user_id"] == profile["id"]
    assert signoffs[0]["signed_at"]

    # Signing the same role again replaces it, not duplicates it.
    again = await client.post(f"{base}/signoff", json={"role": "audio_engineer"}, headers=_h(ws_id))
    assert len(again.json()["signoffs"]) == 1


async def test_all_four_roles_can_be_signed_independently(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()
    base = f"/api/v1/audio-assets/{asset['id']}"

    for role in ("audio_engineer", "brand_guardian", "executive_producer", "legal_compliance"):
        res = await client.post(f"{base}/signoff", json={"role": role}, headers=_h(ws_id))
        assert res.status_code == 200, res.text

    doc = await audio_assets.find_one({"id": asset["id"]})
    assert {s["role"] for s in doc["signoffs"]} == {
        "audio_engineer", "brand_guardian", "executive_producer", "legal_compliance",
    }


async def test_signoff_rejects_an_unknown_role(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()

    res = await client.post(
        f"/api/v1/audio-assets/{asset['id']}/signoff", json={"role": "not_a_real_role"}, headers=_h(ws_id),
    )
    assert res.status_code == 422


async def test_the_signer_can_undo_their_own_signoff(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()
    base = f"/api/v1/audio-assets/{asset['id']}"
    await client.post(f"{base}/signoff", json={"role": "audio_engineer"}, headers=_h(ws_id))

    res = await client.delete(f"{base}/signoff/audio_engineer", headers=_h(ws_id))
    assert res.status_code == 200, res.text
    assert res.json()["signoffs"] == []


async def test_a_non_signer_editor_cannot_undo_someone_elses_signoff(signup_user, make_client, stubs):
    owner, _, ws_id, brand_id = await _setup(signup_user)
    editor, _ = await invite_and_accept(owner, make_client, ws_id, "editor")
    asset = (await _generate(owner, ws_id, brand_id)).json()
    base = f"/api/v1/audio-assets/{asset['id']}"
    await owner.post(f"{base}/signoff", json={"role": "legal_compliance"}, headers=_h(ws_id))

    res = await editor.delete(f"{base}/signoff/legal_compliance", headers=_h(ws_id))
    assert res.status_code == 403


async def test_an_admin_can_override_someone_elses_signoff(signup_user, make_client, stubs):
    owner, _, ws_id, brand_id = await _setup(signup_user)
    admin, _ = await invite_and_accept(owner, make_client, ws_id, "admin")
    asset = (await _generate(owner, ws_id, brand_id)).json()
    base = f"/api/v1/audio-assets/{asset['id']}"
    await owner.post(f"{base}/signoff", json={"role": "legal_compliance"}, headers=_h(ws_id))

    res = await admin.delete(f"{base}/signoff/legal_compliance", headers=_h(ws_id))
    assert res.status_code == 200, res.text


async def test_undoing_a_role_that_was_never_signed_is_404(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()

    res = await client.delete(f"/api/v1/audio-assets/{asset['id']}/signoff/legal_compliance", headers=_h(ws_id))
    assert res.status_code == 404


# ── podcast feed ─────────────────────────────────────────────────────────────

async def test_enabling_the_feed_returns_a_real_stable_token(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)

    res = await client.post(
        "/api/v1/audio-assets/feed/enable",
        json={"brand_id": brand_id, "title": "My Show", "description": "A real show."},
        headers=_h(ws_id),
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["is_enabled"] is True
    assert body["token"]
    assert body["feed_url"].endswith(f"/api/v1/audio-assets/feed/{body['token']}.xml")

    # Re-enabling doesn't rotate the token — a feed already submitted to a
    # podcast directory has to keep resolving.
    again = await client.post(
        "/api/v1/audio-assets/feed/enable",
        json={"brand_id": brand_id, "title": "My Show", "description": ""},
        headers=_h(ws_id),
    )
    assert again.json()["token"] == body["token"]


async def test_feed_xml_lists_only_approved_episodes_for_that_brand(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    other_brand_id = await _brand(client, ws_id)

    approved = (await _generate(client, ws_id, brand_id, title="Approved Episode")).json()
    await client.patch(f"/api/v1/audio-assets/{approved['id']}/approve", headers=_h(ws_id))

    pending = (await _generate(client, ws_id, brand_id, title="Still Pending")).json()  # never approved

    other = (await _generate(client, ws_id, other_brand_id, title="Different Brand")).json()
    await client.patch(f"/api/v1/audio-assets/{other['id']}/approve", headers=_h(ws_id))

    enable = await client.post(
        "/api/v1/audio-assets/feed/enable",
        json={"brand_id": brand_id, "title": "My Show", "description": ""},
        headers=_h(ws_id),
    )
    token = enable.json()["token"]

    res = await client.get(f"/api/v1/audio-assets/feed/{token}.xml")
    assert res.status_code == 200, res.text
    assert res.headers["content-type"].startswith("application/rss+xml")
    xml = res.text
    assert "Approved Episode" in xml
    assert "Still Pending" not in xml
    assert "Different Brand" not in xml
    assert "<rss" in xml and "<itunes:duration>" in xml


async def test_feed_404s_for_an_unknown_or_disabled_token(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)

    unknown = await client.get("/api/v1/audio-assets/feed/not-a-real-token.xml")
    assert unknown.status_code == 404

    enable = await client.post(
        "/api/v1/audio-assets/feed/enable",
        json={"brand_id": brand_id, "title": "My Show", "description": ""},
        headers=_h(ws_id),
    )
    token = enable.json()["token"]
    await client.patch(f"/api/v1/audio-assets/feed/{brand_id}", json={"is_enabled": False}, headers=_h(ws_id))

    disabled = await client.get(f"/api/v1/audio-assets/feed/{token}.xml")
    assert disabled.status_code == 404


async def test_feed_status_reports_a_real_episode_count(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()
    await client.patch(f"/api/v1/audio-assets/{asset['id']}/approve", headers=_h(ws_id))

    before = await client.get("/api/v1/audio-assets/feed/status", params={"brand_id": brand_id}, headers=_h(ws_id))
    assert before.json()["is_enabled"] is False

    await client.post(
        "/api/v1/audio-assets/feed/enable",
        json={"brand_id": brand_id, "title": "My Show", "description": ""},
        headers=_h(ws_id),
    )
    after = await client.get("/api/v1/audio-assets/feed/status", params={"brand_id": brand_id}, headers=_h(ws_id))
    assert after.json()["episode_count"] == 1
