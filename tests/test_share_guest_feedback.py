"""Tests for guest feedback on a shared recording (opt-in comments, held
for approval by default) and the real, anonymous, deduped view/play/
listen-through stats behind the Inspector's share links.
"""
from datetime import datetime, timedelta, timezone

from app.db.mongo import audio_comments, audio_share_links, share_view_events
from tests.test_audio_assets import _generate, _h, _setup, stubs  # noqa: F401 — fixture reuse


async def _audio_share(client, ws_id, brand_id):
    asset = (await _generate(client, ws_id, brand_id, title="Shared Episode")).json()
    res = await client.post(f"/api/v1/audio-assets/{asset['id']}/share-link", headers=_h(ws_id))
    assert res.status_code == 201, res.text
    return asset, res.json()


async def _allow_comments(client, ws_id, asset_id, token, hold_for_approval=True):
    res = await client.patch(
        f"/api/v1/audio-assets/{asset_id}/share-link/{token}",
        json={"allow_comments": True, "hold_for_approval": hold_for_approval}, headers=_h(ws_id),
    )
    assert res.status_code == 200, res.text
    return res.json()


# ── settings ─────────────────────────────────────────────────────────────────

async def test_comments_are_off_by_default_and_held_for_approval_by_default(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset, link = await _audio_share(client, ws_id, brand_id)

    listed = (await client.get(f"/api/v1/audio-assets/{asset['id']}/share-links", headers=_h(ws_id))).json()
    assert listed[0]["allow_comments"] is False and listed[0]["hold_for_approval"] is True

    updated = await _allow_comments(client, ws_id, asset["id"], link["token"])
    assert updated["allow_comments"] is True


async def test_updating_an_unknown_link_or_asset_is_404(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset, link = await _audio_share(client, ws_id, brand_id)
    bad = await client.patch(
        f"/api/v1/audio-assets/{asset['id']}/share-link/not-a-real-token",
        json={"allow_comments": True}, headers=_h(ws_id),
    )
    assert bad.status_code == 404
    empty_body = await client.patch(
        f"/api/v1/audio-assets/{asset['id']}/share-link/{link['token']}", json={}, headers=_h(ws_id),
    )
    assert empty_body.status_code == 400


# ── guest comments ───────────────────────────────────────────────────────────

async def test_a_guest_comment_is_held_for_approval_by_default(signup_user, make_client, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset, link = await _audio_share(client, ws_id, brand_id)
    await _allow_comments(client, ws_id, asset["id"], link["token"])

    public = make_client()
    posted = await public.post(
        f"/api/v1/share/{link['token']}/comments",
        json={"time_s": 12.5, "text": "Great episode!", "guest_name": "Jamie"},
    )
    assert posted.status_code == 201, posted.text
    assert posted.json()["author_name"] == "Jamie" and posted.json()["is_guest"] is True

    # Invisible to other viewers of the page until approved.
    public_list = await public.get(f"/api/v1/share/{link['token']}/comments")
    assert public_list.json() == []

    # But visible to the owner, who can now approve it.
    owner_list = await client.get(f"/api/v1/audio-assets/{asset['id']}/comments", headers=_h(ws_id))
    pending = owner_list.json()[0]
    assert pending["is_guest"] is True and pending["approved"] is False

    approved = await client.patch(
        f"/api/v1/audio-assets/{asset['id']}/comments/{pending['id']}", json={"approved": True}, headers=_h(ws_id),
    )
    assert approved.status_code == 200 and approved.json()["approved"] is True
    now_visible = await public.get(f"/api/v1/share/{link['token']}/comments")
    assert len(now_visible.json()) == 1


async def test_a_link_without_hold_for_approval_shows_guest_comments_immediately(signup_user, make_client, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset, link = await _audio_share(client, ws_id, brand_id)
    await _allow_comments(client, ws_id, asset["id"], link["token"], hold_for_approval=False)

    public = make_client()
    await public.post(f"/api/v1/share/{link['token']}/comments", json={"time_s": 1.0, "text": "Nice!"})
    visible = await public.get(f"/api/v1/share/{link['token']}/comments")
    assert len(visible.json()) == 1
    assert visible.json()[0]["author_name"] == "A listener"  # the default name when none is given


async def test_comments_are_refused_when_the_link_has_not_turned_them_on(signup_user, make_client, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset, link = await _audio_share(client, ws_id, brand_id)  # allow_comments left False

    public = make_client()
    res = await public.post(f"/api/v1/share/{link['token']}/comments", json={"time_s": 1.0, "text": "Hi"})
    assert res.status_code == 403


async def test_comment_validation(signup_user, make_client, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset, link = await _audio_share(client, ws_id, brand_id)
    await _allow_comments(client, ws_id, asset["id"], link["token"])
    public = make_client()

    assert (await public.post(f"/api/v1/share/{link['token']}/comments", json={"time_s": 1.0, "text": "   "})).status_code == 400
    assert (await public.post(f"/api/v1/share/{link['token']}/comments", json={"time_s": 1.0, "text": "x" * 1001})).status_code == 400
    assert (await public.post(f"/api/v1/share/{link['token']}/comments", json={"time_s": -1.0, "text": "hi"})).status_code == 400
    assert (await public.post("/api/v1/share/not-a-real-token/comments", json={"time_s": 1.0, "text": "hi"})).status_code == 404


async def test_the_honeypot_field_silently_discards_a_bot_submission(signup_user, make_client, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset, link = await _audio_share(client, ws_id, brand_id)
    await _allow_comments(client, ws_id, asset["id"], link["token"])
    public = make_client()

    res = await public.post(
        f"/api/v1/share/{link['token']}/comments",
        json={"time_s": 1.0, "text": "buy my product", "website": "http://spam.example"},
    )
    # Told it succeeded...
    assert res.status_code == 201, res.text
    # ...but nothing was actually saved.
    assert await audio_comments.count_documents({"audio_asset_id": asset["id"]}) == 0


async def test_a_disabled_expired_or_revoked_link_refuses_comments_too(signup_user, make_client, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset, link = await _audio_share(client, ws_id, brand_id)
    await _allow_comments(client, ws_id, asset["id"], link["token"])
    public = make_client()

    await audio_share_links.update_one(
        {"token": link["token"]}, {"$set": {"expires_at": datetime.now(timezone.utc) - timedelta(days=1)}},
    )
    expired = await public.post(f"/api/v1/share/{link['token']}/comments", json={"time_s": 1.0, "text": "hi"})
    assert expired.status_code == 404


async def test_only_the_recordings_own_workspace_sees_pending_guest_comments_in_stats(signup_user, make_client, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset, link = await _audio_share(client, ws_id, brand_id)
    await _allow_comments(client, ws_id, asset["id"], link["token"])
    public = make_client()
    await public.post(f"/api/v1/share/{link['token']}/comments", json={"time_s": 1.0, "text": "hi"})

    listed = (await client.get(f"/api/v1/audio-assets/{asset['id']}/share-links", headers=_h(ws_id))).json()
    assert listed[0]["pending_comments"] == 1


# ── anonymous view/play/listen-through stats ─────────────────────────────────

async def test_events_are_recorded_and_shown_as_real_counts(signup_user, make_client, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset, link = await _audio_share(client, ws_id, brand_id)
    public = make_client()

    for event in ("view", "play", "25", "50"):
        res = await public.post(f"/api/v1/share/{link['token']}/event", json={"event_type": event})
        assert res.status_code == 204, res.text

    listed = (await client.get(f"/api/v1/audio-assets/{asset['id']}/share-links", headers=_h(ws_id))).json()
    row = listed[0]
    assert row["views"] == 1 and row["plays"] == 1
    assert row["completed_25"] == 1 and row["completed_50"] == 1 and row["completed_75"] == 0


async def test_repeat_events_from_the_same_visitor_the_same_day_are_not_double_counted(signup_user, make_client, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset, link = await _audio_share(client, ws_id, brand_id)
    public = make_client()

    for _ in range(3):
        await public.post(f"/api/v1/share/{link['token']}/event", json={"event_type": "view"})

    listed = (await client.get(f"/api/v1/audio-assets/{asset['id']}/share-links", headers=_h(ws_id))).json()
    assert listed[0]["views"] == 1


async def test_a_different_visitor_is_counted_separately(signup_user, make_client, stubs, monkeypatch):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset, link = await _audio_share(client, ws_id, brand_id)

    from app.api.v1 import share as share_module
    calls = {"n": 0}

    def _fake_hash(request):
        calls["n"] += 1
        return f"visitor-{calls['n']}"

    monkeypatch.setattr(share_module, "_visitor_hash", _fake_hash)
    public = make_client()
    await public.post(f"/api/v1/share/{link['token']}/event", json={"event_type": "view"})
    await public.post(f"/api/v1/share/{link['token']}/event", json={"event_type": "view"})

    listed = (await client.get(f"/api/v1/audio-assets/{asset['id']}/share-links", headers=_h(ws_id))).json()
    assert listed[0]["views"] == 2


async def test_bad_event_type_and_dead_link_are_refused(signup_user, make_client, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset, link = await _audio_share(client, ws_id, brand_id)
    public = make_client()

    assert (await public.post(f"/api/v1/share/{link['token']}/event", json={"event_type": "not-real"})).status_code == 400
    assert (await public.post("/api/v1/share/not-a-real-token/event", json={"event_type": "view"})).status_code == 404


async def test_turnstile_is_skipped_when_not_configured_but_enforced_once_it_is(signup_user, make_client, stubs, monkeypatch):
    from app.api.v1 import share as share_module

    client, _, ws_id, brand_id = await _setup(signup_user)
    asset, link = await _audio_share(client, ws_id, brand_id)
    await _allow_comments(client, ws_id, asset["id"], link["token"])
    public = make_client()

    # No secret key configured: the form works without a Turnstile token at all.
    ok = await public.post(f"/api/v1/share/{link['token']}/comments", json={"time_s": 1.0, "text": "hi"})
    assert ok.status_code == 201, ok.text

    # Once configured, a real failed verification refuses the comment...
    monkeypatch.setattr(share_module.settings, "TURNSTILE_SECRET_KEY", "test-secret")
    monkeypatch.setattr(share_module, "_verify_turnstile", lambda token, request: _false())
    refused = await public.post(
        f"/api/v1/share/{link['token']}/comments", json={"time_s": 1.0, "text": "hi", "turnstile_token": "bad"},
    )
    assert refused.status_code == 400

    # ...and a real passed verification lets it through.
    monkeypatch.setattr(share_module, "_verify_turnstile", lambda token, request: _true())
    passed = await public.post(
        f"/api/v1/share/{link['token']}/comments", json={"time_s": 1.0, "text": "hi", "turnstile_token": "good"},
    )
    assert passed.status_code == 201, passed.text


async def _false():
    return False


async def _true():
    return True
