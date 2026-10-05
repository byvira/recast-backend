"""The Ops view of who is connected to a platform and the actions on a connection, the directory listings, the
Activity and Usage data, and what a member sees from the platform list."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.db.mongo import (
    activity_entries,
    content_pieces,
    platform_configs,
    platform_listings,
    platform_ops,
    publish_incidents,
    users,
    workspace_connections,
)
from app.pipelines.platform_ops.store import get_ops, save_ops
from app.pipelines.publish.platform_config_store import PLATFORM_WIDE, save_platform_config
from app.pipelines.publish.token_store import save_token
from app.platforms.base import get_platform, import_all
from app.shared.activity import record_system
from app.workers import token_refresh
from tests.conftest import create_workspace
from tests.test_platform_ops import _staff
from tests.test_publish_spine import H, _approve, _later, _seed

import_all()

B = "/api/v1/ops/platforms"
KEYS = ["linkedin", "instagram", "spotify", "slack", "discord"]


@pytest.fixture(autouse=True)
async def clean():
    async def wipe():
        await platform_ops.delete_many({"platform_key": {"$in": KEYS}})
        await platform_configs.delete_many({"platform": {"$in": ["spotify", "slack", "discord"]}})
        await platform_listings.delete_many({"platform_key": "spotify"})
    await wipe()
    yield
    await wipe()


async def _connected_workspace(signup_user, name: str, platform: str = "linkedin", days: int = 40, username: str = "Acct One"):
    client, profile = await signup_user(name="Member " + name)
    profile = {**profile, "email": (await users.find_one({"id": profile["id"]}))["email"]}
    ws_id = await create_workspace(client, name)
    await save_token(
        workspace_id=ws_id, platform=platform, access_token="secret-access-token", refresh_token=None,
        expires_at=datetime.now(timezone.utc) + timedelta(days=days), platform_user_id="acct-1", username=username,
        connected_by=profile["id"],
    )
    return client, profile, ws_id


async def _rows(staff, platform="linkedin", **params):
    res = await staff.get(f"{B}/{platform}/connections", params=params)
    assert res.status_code == 200, res.text
    return res.json()


async def test_staff_see_names_and_never_emails_or_tokens(make_client, signup_user):
    staff, _ = await _staff(make_client)
    client, profile, ws_id = await _connected_workspace(signup_user, "Conn WS Names")
    body = await _rows(staff, q="Conn WS Names")
    assert body["total"] == 1
    row = body["rows"][0]
    assert row["workspace_name"] == "Conn WS Names" and row["connected_by_name"] == "Member Conn WS Names"
    assert row["connected_by_role"] == "owner" and row["account"] == "Acct One" and row["via"] == "direct"
    assert row["status"] == "healthy" and row["days_left"] in (39, 40)
    text = json.dumps(body)
    assert profile["email"] not in text and "secret-access-token" not in text and "access_token" not in text


async def test_only_staff_can_open_the_connections_list(make_client, signup_user):
    client, _, _ = await _connected_workspace(signup_user, "Conn WS Private")
    assert (await client.get(f"{B}/linkedin/connections")).status_code == 403


async def test_status_filter_search_sort_and_paging(make_client, signup_user):
    staff, _ = await _staff(make_client)
    _, _, healthy_ws = await _connected_workspace(signup_user, "Zed Healthy WS")
    _, _, shaky_ws = await _connected_workspace(signup_user, "Zed Shaky WS")
    _, _, soon_ws = await _connected_workspace(signup_user, "Zed Soon WS", days=3)
    await workspace_connections.update_one({"workspace_id": shaky_ws, "platform": "linkedin"}, {"$set": {"health.state": "degraded"}})

    everything = await _rows(staff, q="Zed", limit=100)
    assert {r["workspace_name"]: r["status"] for r in everything["rows"]} == {
        "Zed Healthy WS": "healthy", "Zed Shaky WS": "degraded", "Zed Soon WS": "expiring",
    }
    assert (await _rows(staff, q="Zed", status="degraded"))["rows"][0]["workspace_name"] == "Zed Shaky WS"
    assert (await _rows(staff, q="Zed", status="expiring"))["total"] == 1
    assert [r["workspace_name"] for r in (await _rows(staff, q="Zed", sort="status"))["rows"]][:2] == ["Zed Shaky WS", "Zed Soon WS"]

    first = await _rows(staff, q="Zed", limit=2, page=1)
    second = await _rows(staff, q="Zed", limit=2, page=2)
    assert len(first["rows"]) == 2 and len(second["rows"]) == 1 and first["total"] == 3
    assert (await staff.get(f"{B}/linkedin/connections", params={"status": "nonsense"})).status_code == 422


async def test_connections_through_meta_and_webhook_say_how_they_connect(make_client, signup_user):
    staff, _ = await _staff(make_client)
    await _connected_workspace(signup_user, "Meta Via WS", platform="instagram")
    assert (await _rows(staff, "instagram", q="Meta Via WS"))["rows"][0]["via"] == "meta"

    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Hook Via WS")
    await save_platform_config(PLATFORM_WIDE, "slack", None, True, {}, {})
    await save_platform_config(ws_id, "slack", "Team channel", True, {}, {"webhook_url": "https://hooks.example.com/x"})
    row = (await _rows(staff, "slack", q="Hook Via WS"))["rows"][0]
    assert row["via"] == "webhook" and row["account"] == "Team channel" and row["kind"] == "config"


async def test_directories_and_content_shapes_have_no_connections_list(make_client):
    staff, _ = await _staff(make_client)
    for key in ("spotify", "blog"):
        res = await staff.get(f"{B}/{key}/connections")
        assert res.status_code == 400 and res.json()["detail"]["code"] == "not_applicable"


async def _connection_id(staff, ws_name, platform="linkedin"):
    return (await _rows(staff, platform, q=ws_name))["rows"][0]["id"]


async def test_the_audit_trail_shows_who_connected_and_what_went_wrong(make_client, signup_user):
    staff, _ = await _staff(make_client)
    client, profile, ws_id = await _connected_workspace(signup_user, "Audit WS")
    await record_system(
        workspace_id=ws_id, key=f"health:{ws_id}:1", actor_name="Connection monitor", category="account_connected",
        title="LinkedIn connection needs reconnecting", description="The platform refused the renewal.", status="failed", channel="linkedin",
    )
    await content_pieces.update_one(
        {"piece_id": await _seed(ws_id, profile["id"])}, {"$set": {"publish_target": "linkedin", "publish_status": "failed", "updated_at": datetime.now(timezone.utc)}},
    )
    res = await staff.get(f"{B}/linkedin/connections/{await _connection_id(staff, 'Audit WS')}/audit")
    assert res.status_code == 200, res.text
    body = res.json()
    texts = [e["text"] for e in body["events"]]
    assert any(t.startswith("Connected by Member Audit WS") for t in texts)
    failure = next(e for e in body["events"] if "needs reconnecting" in e["text"])
    assert failure["tone"] == "fail"
    assert body["facts"]["secrets"] == "Encrypted, never shown" and body["facts"]["failed_publishes_30d"] == 1
    assert profile["email"] not in json.dumps(body)


async def test_revealing_an_email_is_logged_every_time(make_client, signup_user):
    staff, staff_user = await _staff(make_client)
    client, profile, ws_id = await _connected_workspace(signup_user, "Reveal WS")
    connection_id = await _connection_id(staff, "Reveal WS")

    assert (await client.post(f"{B}/linkedin/connections/{connection_id}/reveal-email", json={})).status_code == 403
    res = await staff.post(f"{B}/linkedin/connections/{connection_id}/reveal-email", json={"reason": "Support ticket 12"})
    assert res.status_code == 200, res.text
    assert res.json() == {"email": profile["email"], "member": "Member Reveal WS"}
    await staff.post(f"{B}/linkedin/connections/{connection_id}/reveal-email", json={})

    rows = await activity_entries.find({"category": "platform_ops", "metadata.event": "connection.email_revealed", "metadata.subject_workspace_id": ws_id}).to_list(length=10)
    assert len(rows) == 2
    assert any(r["metadata"]["reason"] == "Support ticket 12" for r in rows)
    assert all(profile["email"] not in json.dumps(r.get("metadata")) for r in rows)


async def test_force_disconnect_is_owner_only_keeps_the_token_holds_posts_and_tells_the_owner(make_client, signup_user):
    staff, _ = await _staff(make_client)
    owner, _ = await _staff(make_client, master=True)
    client, profile, ws_id = await _connected_workspace(signup_user, "Force WS")
    piece_id = await _seed(ws_id, profile["id"])
    await _approve(client, ws_id, piece_id)
    sched = await client.patch(f"/api/v1/content/pieces/{piece_id}/schedule", json={"scheduled_at": _later().isoformat()}, headers=H(ws_id))
    assert sched.status_code == 200, sched.text
    connection_id = await _connection_id(staff, "Force WS")
    url = f"{B}/linkedin/connections/{connection_id}/force-disconnect"

    assert (await staff.post(url, json={"reason": "Suspicious"})).status_code == 403
    assert (await owner.post(url, json={"reason": "no"})).status_code == 422  # a reason is required

    with patch("app.api.v1.ops_platform_connections.send_templated_email", new=AsyncMock()) as mail:
        res = await owner.post(url, json={"reason": "Suspicious activity reported"})
    assert res.status_code == 200, res.text
    assert res.json() == {"disconnected": True, "held_posts": 1, "owner_notified": True}
    mail.assert_awaited_once()
    assert mail.await_args.args[0] == "platform-reconnect-needed" and mail.await_args.args[1] == profile["email"]

    conn = await workspace_connections.find_one({"workspace_id": ws_id, "platform": "linkedin"})
    assert conn["health"]["state"] == "broken" and conn["health"]["reason"] == "ops_disconnected"
    assert conn["access_token"] and conn["is_active"] is True  # the token is not deleted
    assert conn["disconnected_by_ops"]["reason"] == "Suspicious activity reported"
    held = await content_pieces.find_one({"piece_id": piece_id})
    assert held["hold"]["reason"] == "ops_disconnected" and held["publish_status"] == "queued"

    logged = await activity_entries.find_one({"category": "platform_ops", "metadata.event": "connection.force_disconnected", "metadata.subject_workspace_id": ws_id})
    assert logged and logged["metadata"]["reason"] == "Suspicious activity reported"
    assert (await _rows(staff, q="Force WS"))["rows"][0]["ops_disconnected"] is True


async def test_the_renewal_job_leaves_a_disconnected_connection_alone_until_the_member_reconnects(make_client, signup_user):
    owner, _ = await _staff(make_client, master=True)
    client, profile, ws_id = await _connected_workspace(signup_user, "Renew WS", days=1)
    piece_id = await _seed(ws_id, profile["id"])
    await _approve(client, ws_id, piece_id)
    await client.patch(f"/api/v1/content/pieces/{piece_id}/schedule", json={"scheduled_at": _later().isoformat()}, headers=H(ws_id))
    connection_id = (await _rows(owner, q="Renew WS"))["rows"][0]["id"]
    with patch("app.api.v1.ops_platform_connections.send_templated_email", new=AsyncMock()):
        await owner.post(f"{B}/linkedin/connections/{connection_id}/force-disconnect", json={"reason": "Testing the hold"})

    seen: list[str] = []

    async def fake_refresh(account):
        seen.append(account["workspace_id"])
        return True, ""

    with patch.object(token_refresh, "refresh_connection", fake_refresh):
        await token_refresh.refresh_expiring_tokens.__wrapped__()
        assert ws_id not in seen
        assert await token_refresh.recover_connection(ws_id, "linkedin") is False
    assert ws_id not in seen

    # the member reconnects: healthy again, the disconnect mark is gone, and the held post goes back in the queue
    await save_token(
        workspace_id=ws_id, platform="linkedin", access_token="fresh-token", refresh_token=None,
        expires_at=datetime.now(timezone.utc) + timedelta(days=60), platform_user_id="acct-1", username="Acct One",
        connected_by=profile["id"],
    )
    conn = await workspace_connections.find_one({"workspace_id": ws_id, "platform": "linkedin"})
    assert conn["health"]["state"] == "healthy" and "disconnected_by_ops" not in conn
    released = await content_pieces.find_one({"piece_id": piece_id})
    assert "hold" not in released and released["publish_status"] == "queued"


async def test_staff_can_ask_the_owner_to_reconnect(make_client, signup_user):
    staff, _ = await _staff(make_client)
    client, profile, ws_id = await _connected_workspace(signup_user, "Nudge WS")
    connection_id = await _connection_id(staff, "Nudge WS")
    with patch("app.api.v1.ops_platform_connections.send_templated_email", new=AsyncMock()) as mail:
        res = await staff.post(f"{B}/linkedin/connections/{connection_id}/request-reconnect")
    assert res.status_code == 200 and res.json() == {"sent": True}
    assert mail.await_args.args[1] == profile["email"]
    assert await activity_entries.find_one({"metadata.event": "connection.reconnect_requested", "metadata.subject_workspace_id": ws_id})


async def test_the_csv_has_names_and_no_emails(make_client, signup_user):
    staff, _ = await _staff(make_client)
    client, profile, ws_id = await _connected_workspace(signup_user, "Csv WS")
    res = await staff.get(f"{B}/linkedin/connections/export")
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/csv")
    lines = res.text.splitlines()
    assert lines[0].startswith("workspace,connected_by,role,account,via,status")
    assert any(l.startswith("Csv WS,Member Csv WS,owner,Acct One,direct,healthy") for l in lines)
    assert profile["email"] not in res.text and "secret-access-token" not in res.text
    assert await activity_entries.find_one({"metadata.event": "connections.exported", "metadata.platform_key": "linkedin"})


# ── directory listings ────────────────────────────────────────────────────────

async def test_a_member_records_their_listing_and_staff_see_it_without_any_checking(make_client, signup_user):
    staff, _ = await _staff(make_client)
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Listing WS")

    assert (await client.get("/api/v1/platforms/spotify/listing", headers=H(ws_id))).json()["listing_url"] is None
    bad = await client.put("/api/v1/platforms/spotify/listing", json={"listing_url": "http://x.example.com/show"}, headers=H(ws_id))
    assert bad.status_code == 400
    wrong = await client.put("/api/v1/platforms/linkedin/listing", json={"listing_url": "https://x.example.com/a"}, headers=H(ws_id))
    assert wrong.status_code == 400
    ok = await client.put("/api/v1/platforms/spotify/listing", json={"listing_url": "https://open.spotify.com/show/abc", "status": "live"}, headers=H(ws_id))
    assert ok.status_code == 200 and ok.json()["status"] == "live"
    mine = (await client.get("/api/v1/platforms/spotify/listing", headers=H(ws_id))).json()
    assert mine["listing_url"] == "https://open.spotify.com/show/abc" and mine["status"] == "live"

    listing = (await staff.get(f"{B}/spotify/listings")).json()
    row = next(r for r in listing["rows"] if r["workspace_id"] == ws_id)
    assert row["workspace_name"] == "Listing WS" and row["status"] == "live"
    assert listing["summary"]["live"] >= 1

    noted = await staff.put(f"{B}/spotify/listings/{ws_id}/note", json={"note": "Checked, looks right"})
    assert noted.status_code == 200
    assert (await platform_listings.find_one({"workspace_id": ws_id, "platform_key": "spotify"}))["note"] == "Checked, looks right"
    assert (await staff.put(f"{B}/spotify/listings/nope/note", json={"note": "x"})).status_code == 404


async def test_a_listing_can_be_changed_and_keeps_its_first_submission_date(make_client, signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Listing WS 2")
    await client.put("/api/v1/platforms/spotify/listing", json={"listing_url": "https://open.spotify.com/show/a"}, headers=H(ws_id))
    first = await platform_listings.find_one({"workspace_id": ws_id, "platform_key": "spotify"})
    await client.put("/api/v1/platforms/spotify/listing", json={"listing_url": "https://open.spotify.com/show/b", "status": "rejected"}, headers=H(ws_id))
    second = await platform_listings.find_one({"workspace_id": ws_id, "platform_key": "spotify"})
    assert second["listing_url"].endswith("/b") and second["status"] == "rejected"
    assert second["submitted_at"] == first["submitted_at"]


# ── activity and usage ────────────────────────────────────────────────────────

async def test_activity_reads_in_plain_sentences_and_can_be_filtered(make_client, signup_user):
    staff, _ = await _staff(make_client)
    owner, _ = await _staff(make_client, master=True)
    client, profile, ws_id = await _connected_workspace(signup_user, "Activity WS")
    await publish_incidents.insert_one({
        "platform": "linkedin", "workspace_id": ws_id, "error_type": "FATAL", "error_message": "Account suspended",
        "created_at": datetime.now(timezone.utc),
    })
    await record_system(
        workspace_id=ws_id, key=f"health:{ws_id}:2", actor_name="Connection monitor", category="account_connected",
        title="LinkedIn connection needs reconnecting", description="Two renewals failed.", status="failed", channel="linkedin",
    )
    await owner.post(f"{B}/linkedin/stage", json={"to": "paused", "reason": "Looking into failures", "version": 0})

    everything = (await staff.get(f"{B}/linkedin/activity")).json()
    kinds = {e["kind"] for e in everything["events"]}
    assert {"failure", "connection", "change"} <= kinds
    failure = next(e for e in everything["events"] if e["kind"] == "failure" and e["workspace_id"] == ws_id)
    assert failure["tag"] == "Needs Ops" and failure["body"] == "Account suspended" and failure["workspace_name"] == "Activity WS"
    connection = next(e for e in everything["events"] if e["kind"] == "connection" and e["workspace_id"] == ws_id)
    assert connection["tag"] == "Member action" and connection["tone"] == "red"
    assert all(e["tag"] in ("Needs Ops", "Member action", "FYI") for e in everything["events"])
    assert everything["summary"].startswith("LinkedIn |")

    only_changes = (await staff.get(f"{B}/linkedin/activity", params={"kind": "changes"})).json()["events"]
    assert only_changes and all(e["kind"] == "change" for e in only_changes)
    assert (await staff.get(f"{B}/linkedin/activity", params={"kind": "nonsense"})).status_code == 422
    assert (await client.get(f"{B}/linkedin/activity")).status_code == 403


async def test_usage_counts_publishes_and_failures(make_client, signup_user):
    staff, _ = await _staff(make_client)
    client, profile, ws_id = await _connected_workspace(signup_user, "Usage WS")
    now = datetime.now(timezone.utc)
    ok_piece = await _seed(ws_id, profile["id"])
    failed_piece = await _seed(ws_id, profile["id"])
    await content_pieces.update_one({"piece_id": ok_piece}, {"$set": {"publish_target": "linkedin", "publish_status": "published", "published_at": now}})
    await content_pieces.update_one({"piece_id": failed_piece}, {"$set": {"publish_target": "linkedin", "publish_status": "failed", "updated_at": now}})
    await publish_incidents.insert_one({"platform": "linkedin", "workspace_id": ws_id, "error_type": "AUTH", "error_message": "x", "created_at": now})

    body = (await staff.get(f"{B}/linkedin/usage", params={"days": 14})).json()
    assert len(body["published_per_day"]) == 14 and body["published_per_day"][-1]["date"] == now.strftime("%Y-%m-%d")
    assert body["published_30d"] >= 1 and body["failed_30d"] >= 1 and 0 < body["success_rate"] < 1
    assert {"reason": "AUTH", "count": 1} in [r for r in body["failures_by_reason"] if r["reason"] == "AUTH"]
    assert body["analytics_status"] in ("ok", "blocked", "none") and len(body["connected_over_time"]) == 8
    assert (await staff.get(f"{B}/linkedin/usage", params={"days": 3})).status_code == 422


# ── what a member sees ────────────────────────────────────────────────────────

async def test_the_member_platform_list_says_what_this_workspace_may_do(make_client, signup_user):
    staff, _ = await _staff(make_client)
    owner, _ = await _staff(make_client, master=True)
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Member View WS")

    def by_key(res):
        return {p["key"]: p for p in res.json()["platforms"]}

    res = await client.get("/api/v1/platforms", headers=H(ws_id))
    assert res.status_code == 200, res.text
    platforms = by_key(res)
    assert platforms["linkedin"]["availability"]["value"] == "connectable"
    assert platforms["twitter"]["availability"]["value"] == "manual"
    assert platforms["slack"]["availability"]["value"] == "hidden"
    assert platforms["mastodon"]["availability"]["reason"] == "no_code"
    assert "ops_stage" not in platforms["linkedin"] and "rollout" not in platforms["linkedin"]
    # The most characters a post may have is sent to members, so the screens need no copy of it.
    assert platforms["linkedin"]["max_chars"] == 3000
    assert platforms["blog"]["max_chars"] is None
    assert platforms["instagram"]["requires_media"] is True and platforms["linkedin"]["requires_media"] is False

    # LinkedIn is live for everyone, so once it is paused every workspace sees it as paused.
    await owner.post(f"{B}/linkedin/stage", json={"to": "paused", "reason": "Checking", "version": 0})
    assert by_key(await client.get("/api/v1/platforms", headers=H(ws_id)))["linkedin"]["availability"]["value"] == "paused"
    detail = await client.get("/api/v1/platforms/linkedin", headers=H(ws_id))
    assert detail.json()["availability"]["value"] == "paused"

    stranger, _ = await signup_user(name="Somebody Else")
    other = await stranger.get("/api/v1/platforms", headers=H(ws_id))
    assert other.status_code == 200 and all(p["availability"] is None for p in other.json()["platforms"])


async def test_a_retired_platform_is_no_longer_read_for_results_and_a_paused_one_still_is(make_client):
    from app.pipelines.analytics.aggregator import _default_analytics_platforms

    owner, _ = await _staff(make_client, master=True)
    assert "linkedin" in await _default_analytics_platforms()
    paused = await owner.post(f"{B}/linkedin/stage", json={"to": "paused", "reason": "Checking", "version": 0})
    assert "linkedin" in await _default_analytics_platforms()
    await owner.post(f"{B}/linkedin/stage", json={"to": "retired", "reason": "Gone", "confirm_name": "LinkedIn", "version": paused.json()["ops"]["version"]})
    assert "linkedin" not in await _default_analytics_platforms()


async def test_the_member_list_says_when_a_manual_platform_has_a_link_and_gives_directory_steps(make_client, signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Extras WS")

    def by_key(res):
        return {p["key"]: p for p in res.json()["platforms"]}

    before = by_key(await client.get("/api/v1/platforms", headers=H(ws_id)))
    assert "manual_handoff_ready" not in before["twitter"] and "directory" not in before["spotify"]

    await save_platform_config(PLATFORM_WIDE, "twitter", None, True, {"compose_url_template": "http://bad.example.com/c?text={text}"}, {})
    await save_platform_config(PLATFORM_WIDE, "spotify", None, True, {"submission_url": "https://podcasters.spotify.com", "member_steps": "1. Open it."}, {})
    bad = by_key(await client.get("/api/v1/platforms", headers=H(ws_id)))
    assert bad["twitter"]["manual_handoff_ready"] is False  # an http link is not usable
    assert bad["spotify"]["directory"] == {"submission_url": "https://podcasters.spotify.com", "member_steps": "1. Open it.", "require_listing_url": True}

    await save_platform_config(PLATFORM_WIDE, "twitter", None, True, {"compose_url_template": "https://x.example.com/c?text={text}"}, {})
    good = by_key(await client.get("/api/v1/platforms", headers=H(ws_id)))
    assert good["twitter"]["manual_handoff_ready"] is True
    detail = (await client.get("/api/v1/platforms/twitter", headers=H(ws_id))).json()
    assert detail["manual_handoff_ready"] is True
    await platform_configs.delete_many({"platform": {"$in": ["twitter", "spotify"]}})


# ── scopes, rollout names, purging credentials, a workspace's own webhook address ──

async def test_the_audit_shows_the_scopes_the_provider_reported(make_client, signup_user):
    staff, _ = await _staff(make_client)
    client, profile = await signup_user(name="Scopes Member")
    ws_id = await create_workspace(client, "Scopes WS")
    await save_token(
        workspace_id=ws_id, platform="linkedin", access_token="t", refresh_token=None,
        expires_at=datetime.now(timezone.utc) + timedelta(days=40), platform_user_id="a", username="Acct",
        connected_by=profile["id"], scopes=["openid", "w_member_social"],
    )
    connection_id = await _connection_id(staff, "Scopes WS")
    facts = (await staff.get(f"{B}/linkedin/connections/{connection_id}/audit")).json()["facts"]
    assert facts["scopes"] == ["openid", "w_member_social"]
    # a renewal sends no scopes, so what was recorded stays
    await save_token(
        workspace_id=ws_id, platform="linkedin", access_token="t2", refresh_token=None,
        expires_at=datetime.now(timezone.utc) + timedelta(days=60), platform_user_id="a", username="Acct", connected_by=profile["id"],
    )
    again = (await staff.get(f"{B}/linkedin/connections/{connection_id}/audit")).json()["facts"]
    assert again["scopes"] == ["openid", "w_member_social"]


async def test_a_selected_rollout_comes_back_with_workspace_names(make_client, signup_user):
    owner, _ = await _staff(make_client, master=True)
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Named Rollout WS")
    await owner.put(f"{B}/slack/rollout", json={"scope": "selected", "workspace_ids": [ws_id], "version": 0})
    detail = (await owner.get(f"{B}/slack")).json()
    assert detail["rollout_workspaces"] == [{"id": ws_id, "name": "Named Rollout WS"}]
    assert (await owner.get(f"{B}/discord")).json()["rollout_workspaces"] == []


async def test_purging_credentials_is_owner_only_needs_a_retired_platform_and_the_name(make_client, signup_user):
    staff, _ = await _staff(make_client)
    owner, _ = await _staff(make_client, master=True)
    client, profile, ws_id = await _connected_workspace(signup_user, "Purge WS")
    url = f"{B}/linkedin/purge-credentials"

    assert (await staff.post(url, json={"confirm_name": "LinkedIn"})).status_code == 403
    live = await owner.post(url, json={"confirm_name": "LinkedIn"})
    assert live.status_code == 400 and live.json()["detail"]["code"] == "not_retired"

    await owner.post(f"{B}/linkedin/stage", json={"to": "retired", "reason": "Gone", "confirm_name": "LinkedIn", "version": 0})
    wrong = await owner.post(url, json={"confirm_name": "Linked"})
    assert wrong.status_code == 400 and wrong.json()["detail"]["code"] == "confirm_name"
    assert (await workspace_connections.find_one({"workspace_id": ws_id, "platform": "linkedin"}))["access_token"]

    ok = await owner.post(url, json={"confirm_name": "linkedin", "reason": "Platform closed"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["connections"] >= 1
    conn = await workspace_connections.find_one({"workspace_id": ws_id, "platform": "linkedin"})
    assert "access_token" not in conn and "refresh_token" not in conn and conn["is_active"] is False
    logged = await activity_entries.find_one({"metadata.event": "platform.credentials_purged", "metadata.platform_key": "linkedin"})
    assert logged and logged["metadata"]["reason"] == "Platform closed"


async def test_a_workspace_saves_its_own_webhook_address_and_it_is_never_shown_again(make_client, signup_user, monkeypatch):
    owner, _ = await _staff(make_client, master=True)
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Own Hook WS")
    url = "/api/v1/platforms/slack/connection"

    unopened = await client.put(url, json={"webhook_url": "https://8.8.8.8/hook"}, headers=H(ws_id))
    assert unopened.status_code == 409 and unopened.json()["detail"]["code"] == "PLATFORM_NOT_AVAILABLE"

    await save_platform_config(PLATFORM_WIDE, "slack", None, True, {}, {})
    definition = get_platform("slack")
    await save_ops(definition, {"ops_stage": "live", "rollout": {"scope": "everyone", "workspace_ids": []}}, expected_version=0, actor_id="test")

    assert (await client.put(url, json={"webhook_url": "http://8.8.8.8/hook"}, headers=H(ws_id))).status_code == 400
    assert (await client.put(url, json={"webhook_url": "https://10.0.0.5/hook"}, headers=H(ws_id))).status_code == 400
    assert (await client.get(url, headers=H(ws_id))).json() == {"webhook_set": False, "label": None}

    def by_key(res):
        return {p["key"]: p for p in res.json()["platforms"]}

    before = by_key(await client.get("/api/v1/platforms", headers=H(ws_id)))["slack"]
    assert before["webhook_ready"] is False and before["webhook_own"] is False

    ok = await client.put(url, json={"webhook_url": "https://8.8.8.8/hook", "label": "Team channel"}, headers=H(ws_id))
    assert ok.status_code == 200, ok.text
    assert "8.8.8.8" not in ok.text
    mine = await client.get(url, headers=H(ws_id))
    assert mine.json() == {"webhook_set": True, "label": "Team channel"} and "8.8.8.8" not in mine.text
    after = by_key(await client.get("/api/v1/platforms", headers=H(ws_id)))["slack"]
    assert after["webhook_ready"] is True and after["webhook_own"] is True
    assert (await platform_configs.find_one({"workspace_id": ws_id, "platform": "slack"}))["secrets"]["webhook_url"] != "https://8.8.8.8/hook"

    gone = await client.delete(url, headers=H(ws_id))
    assert gone.status_code == 200
    assert (await client.get(url, headers=H(ws_id))).json()["webhook_set"] is False

    assert (await client.put("/api/v1/platforms/linkedin/connection", json={"webhook_url": "https://8.8.8.8/x"}, headers=H(ws_id))).status_code == 400
    assert await activity_entries.find_one({"metadata.event": "connection.webhook_saved", "metadata.subject_workspace_id": ws_id})
