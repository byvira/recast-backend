"""What Ops allows for a platform: the stored record and its defaults, availability for a workspace, the go-live
checks, and the Ops routes for stage, rollout, live test, registry facts and test sends."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.core.config import settings
from app.db.mongo import activity_entries, content_pieces, platform_configs, platform_ops, users
from app.pipelines.platform_ops.availability import platform_availability
from app.pipelines.platform_ops.readiness import build_readiness
from app.pipelines.platform_ops.store import VersionConflict, derived_default, get_ops, save_ops
from app.pipelines.publish.platform_config_store import PLATFORM_WIDE, save_platform_config
from app.platforms.base import get_platform, import_all
from app.workers import scheduled_posts as worker
from tests.conftest import create_workspace, signup_new_user
from tests.test_publish_spine import H, _approve, _connect, _later, _ok_publisher, _seed

import_all()

B = "/api/v1/ops/platforms"
KEYS = ["slack", "discord", "twitter", "spotify", "mastodon", "medium", "linkedin"]


@pytest.fixture(autouse=True)
async def clean():
    async def wipe():
        await platform_ops.delete_many({"platform_key": {"$in": KEYS}})
        await platform_configs.delete_many({"platform": {"$in": KEYS}})
    await wipe()
    yield
    await wipe()


async def _staff(make_client, master: bool = False):
    client = make_client()
    user = await signup_new_user(client, name="Ops Person")
    fields = {"is_platform_staff": True}
    if master:
        fields["is_master_admin"] = True
    await users.update_one({"id": user["id"]}, {"$set": fields})
    return client, user


def _def(key):
    return get_platform(key)


# ── the record and its defaults ───────────────────────────────────────────────

async def test_untouched_platforms_behave_from_derived_defaults():
    linkedin = await get_ops(_def("linkedin"))
    assert (linkedin["ops_stage"], linkedin["rollout"]["scope"], linkedin["version"], linkedin["derived"]) == ("live", "everyone", 0, True)
    x = await get_ops(_def("twitter"))
    assert x["ops_stage"] == "live" and x["rollout"]["scope"] == "everyone"
    mastodon = await get_ops(_def("mastodon"))
    assert mastodon["ops_stage"] == "not_started"
    assert derived_default(_def("slack"))["rollout"]["scope"] == "ops_only"


async def test_saving_checks_the_version_it_was_based_on():
    definition = _def("slack")
    first = await save_ops(definition, {"ops_stage": "in_setup"}, expected_version=0, actor_id="u1",
                           history_entry={"from": "not_started", "to": "in_setup", "reason": ""})
    assert first["version"] == 1 and first["derived"] is False and first["stage_history"][0]["to"] == "in_setup"
    with pytest.raises(VersionConflict):
        await save_ops(definition, {"ops_stage": "live"}, expected_version=0, actor_id="u2")
    second = await save_ops(definition, {"ops_stage": "live"}, expected_version=1, actor_id="u2")
    assert second["version"] == 2 and (await get_ops(definition))["ops_stage"] == "live"


async def test_history_keeps_only_the_latest_fifty_changes():
    definition = _def("slack")
    version = 0
    for i in range(55):
        saved = await save_ops(definition, {}, expected_version=version, actor_id="u", history_entry={"from": "a", "to": str(i), "reason": ""})
        version = saved["version"]
    history = (await get_ops(definition))["stage_history"]
    assert len(history) == 50 and history[-1]["to"] == "54"


# ── availability ──────────────────────────────────────────────────────────────

async def _stage(key, stage, scope="ops_only", ids=None):
    definition = _def(key)
    current = await get_ops(definition)
    await save_ops(
        definition, {"ops_stage": stage, "rollout": {"scope": scope, "workspace_ids": ids or []}},
        expected_version=current["version"], actor_id="test",
    )


async def test_a_platform_with_no_code_is_never_offered(monkeypatch):
    monkeypatch.setattr(settings, "OPS_WORKSPACE_IDS", "ws-ops")
    await _stage("mastodon", "live", "everyone")
    result = await platform_availability("mastodon", "ws-ops")
    assert (result.value, result.reason) == ("hidden", "no_code")
    assert (await platform_availability("not-a-platform", "ws-ops")).value == "hidden"


async def test_untouched_active_platforms_are_connectable_by_everyone(monkeypatch):
    monkeypatch.setattr(settings, "OPS_WORKSPACE_IDS", "ws-ops")
    assert (await platform_availability("linkedin", "any-workspace")).value == "connectable"
    assert (await platform_availability("twitter", "any-workspace")).value == "manual"


async def test_a_webhook_platform_needs_settings_and_then_follows_its_stage_and_rollout(monkeypatch):
    monkeypatch.setattr(settings, "OPS_WORKSPACE_IDS", "ws-ops")
    assert (await platform_availability("slack", "ws-ops")).reason == "no_code"  # no settings saved yet
    await save_platform_config(PLATFORM_WIDE, "slack", None, True, {}, {})

    assert (await platform_availability("slack", "ws-ops")).reason == "not_started"
    await _stage("slack", "in_setup")
    assert (await platform_availability("slack", "ws-ops")).value == "connectable"
    assert (await platform_availability("slack", "ws-other")).value == "hidden"

    await _stage("slack", "live", "ops_only")
    assert (await platform_availability("slack", "ws-ops")).value == "connectable"
    assert (await platform_availability("slack", "ws-other")) == (await platform_availability("slack", "ws-third"))
    assert (await platform_availability("slack", "ws-other")).reason == "outside_rollout"

    await _stage("slack", "live", "selected", ["ws-pick"])
    assert (await platform_availability("slack", "ws-pick")).value == "connectable"
    assert (await platform_availability("slack", "ws-other")).value == "hidden"

    await _stage("slack", "live", "everyone")
    assert (await platform_availability("slack", "ws-other")).value == "connectable"


async def test_a_workspace_that_already_connected_keeps_access_when_the_rollout_is_narrowed(monkeypatch):
    monkeypatch.setattr(settings, "OPS_WORKSPACE_IDS", "ws-ops")
    await save_platform_config(PLATFORM_WIDE, "slack", None, True, {}, {})
    await save_platform_config("ws-already", "slack", None, True, {}, {"webhook_url": "https://hooks.example.com/x"})
    await _stage("slack", "live", "ops_only")
    assert (await platform_availability("slack", "ws-already")).value == "connectable"
    assert (await platform_availability("slack", "ws-new")).value == "hidden"


async def test_paused_and_retired_reach_those_who_were_in_but_hide_from_everyone_else(monkeypatch):
    monkeypatch.setattr(settings, "OPS_WORKSPACE_IDS", "ws-ops")
    await save_platform_config(PLATFORM_WIDE, "slack", None, True, {}, {})
    await save_platform_config("ws-already", "slack", None, True, {}, {"webhook_url": "https://hooks.example.com/x"})
    await _stage("slack", "paused", "ops_only")
    assert (await platform_availability("slack", "ws-ops")).value == "paused"
    assert (await platform_availability("slack", "ws-already")).value == "paused"
    assert (await platform_availability("slack", "ws-new")).value == "hidden"
    await _stage("slack", "retired", "ops_only")
    assert (await platform_availability("slack", "ws-already")).value == "retired"
    assert (await platform_availability("slack", "ws-new")).value == "hidden"


async def test_an_rss_directory_is_a_manual_listing_and_obeys_the_stage(monkeypatch):
    monkeypatch.setattr(settings, "OPS_WORKSPACE_IDS", "ws-ops")
    assert (await platform_availability("spotify", "ws-ops")).value == "hidden"
    await _stage("spotify", "in_setup")
    assert (await platform_availability("spotify", "ws-ops")).value == "manual"


async def test_the_ops_workspace_falls_back_to_the_default_workspace_of_master_admins(make_client, monkeypatch):
    monkeypatch.setattr(settings, "OPS_WORKSPACE_IDS", "")
    from app.pipelines.platform_ops.availability import ops_workspace_ids

    client, user = await _staff(make_client, master=True)
    default_ws = (await users.find_one({"id": user["id"]})).get("default_workspace_id")
    assert default_ws and default_ws in await ops_workspace_ids()


# ── the go-live checks ────────────────────────────────────────────────────────

async def test_a_webhook_platform_is_ready_only_when_settings_a_live_test_and_facts_are_in():
    definition = _def("slack")
    ops = derived_default(definition)
    report = build_readiness(definition, ops, None)
    assert not report["can_go_live"] and set(report["blockers"]) == {"config", "live_test", "facts"}

    config = {"enabled": True, "fields": {}}
    ops = {**ops, "auth_test": {"tested_by": "Asha"}, "facts_verified": {"source_url": "https://api.slack.com/x"}}
    report = build_readiness(definition, ops, config)
    assert report["can_go_live"] and report["blockers"] == []


async def test_switched_off_settings_do_not_count():
    definition = _def("slack")
    report = build_readiness(definition, derived_default(definition), {"enabled": False, "fields": {}})
    config_check = next(c for c in report["checks"] if c["key"] == "config")
    assert config_check["state"] == "fail" and "switched off" in config_check["detail"]


async def test_a_manual_platform_needs_a_working_compose_link():
    definition = _def("medium")
    ops = {**derived_default(definition), "auth_test": {"tested_by": "A"}, "facts_verified": {"source_url": "https://x.example.com"}}
    bad = build_readiness(definition, ops, {"enabled": True, "fields": {"compose_url_template": "http://x.example.com/c?text={text}"}})
    assert "config" in bad["blockers"]
    good = build_readiness(definition, ops, {"enabled": True, "fields": {"compose_url_template": "https://x.example.com/c?text={text}"}})
    assert good["can_go_live"]


async def test_a_platform_with_no_publisher_written_is_waiting_for_a_developer():
    report = build_readiness(_def("mastodon"), derived_default(_def("mastodon")), None)
    assert report["waiting_for_developer"] and "publisher" in report["blockers"] and not report["can_go_live"]


async def test_an_rss_directory_needs_its_submission_address_but_no_live_test():
    definition = _def("spotify")
    ops = {**derived_default(definition), "facts_verified": {"source_url": "https://x.example.com"}}
    missing = build_readiness(definition, ops, {"enabled": True, "fields": {}})
    assert missing["blockers"] == ["config"]
    ok = build_readiness(definition, ops, {"enabled": True, "fields": {"submission_url": "https://podcasters.spotify.com"}})
    assert ok["can_go_live"]
    assert next(c for c in ok["checks"] if c["key"] == "live_test")["state"] == "na"


async def test_a_content_shape_has_nothing_to_take_live():
    report = build_readiness(_def("blog"), derived_default(_def("blog")), None)
    assert report["can_go_live"] is False and report["blockers"] == []


async def test_an_already_verified_platform_needs_no_fact_check():
    report = build_readiness(_def("linkedin"), derived_default(_def("linkedin")), None)
    assert next(c for c in report["checks"] if c["key"] == "facts")["state"] == "pass"


# ── the routes ────────────────────────────────────────────────────────────────

async def test_only_platform_staff_can_see_the_catalog(make_client):
    ordinary = make_client()
    await signup_new_user(ordinary)
    assert (await ordinary.get(B)).status_code == 403
    staff, _ = await _staff(make_client)
    res = await staff.get(B)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["total"] == 74
    row = next(p for p in body["platforms"] if p["key"] == "linkedin")
    assert row["ops_stage"] == "live" and row["derived"] is True and row["version"] == 0
    assert row["external_gate"] is None
    assert next(p for p in body["platforms"] if p["key"] == "instagram")["external_gate"] == "Meta app in tester mode"
    assert next(p for p in body["platforms"] if p["key"] == "mastodon")["waiting_for_developer"] is True


async def test_detail_carries_the_checks_and_the_record(make_client):
    staff, _ = await _staff(make_client)
    res = await staff.get(f"{B}/slack")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ops"]["ops_stage"] == "not_started" and body["ops"]["version"] == 0
    assert body["pictures_per_post"] == 1 and body["max_chars"] is None
    assert (await staff.get(f"{B}/bluesky")).json()["pictures_per_post"] == 4
    assert {c["key"] for c in body["readiness"]["checks"]} >= {"publisher", "validator", "config", "live_test", "facts"}
    assert (await staff.get(f"{B}/not-a-platform")).status_code == 404


async def test_the_old_settings_list_still_answers_at_its_new_path(make_client):
    owner, _ = await _staff(make_client, master=True)
    res = await owner.get(f"{B}/configs")
    assert res.status_code == 200, res.text
    assert all(p["platform"] for p in res.json()["platforms"])


async def test_only_the_owner_can_start_setup_and_stale_writes_are_refused(make_client):
    staff, _ = await _staff(make_client)
    owner, _ = await _staff(make_client, master=True)

    denied = await staff.post(f"{B}/slack/stage", json={"to": "in_setup", "version": 0})
    assert denied.status_code == 403 and denied.json()["detail"]["code"] == "owner_only"

    ok = await owner.post(f"{B}/slack/stage", json={"to": "in_setup", "version": 0})
    assert ok.status_code == 200, ok.text
    assert ok.json()["ops"]["ops_stage"] == "in_setup" and ok.json()["ops"]["version"] == 1

    stale = await owner.post(f"{B}/slack/stage", json={"to": "not_started", "version": 0})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "version_conflict"


async def test_going_live_lists_what_is_missing_then_passes_once_everything_is_in(make_client):
    staff, _ = await _staff(make_client)
    owner, _ = await _staff(make_client, master=True)
    await owner.post(f"{B}/slack/stage", json={"to": "in_setup", "version": 0})

    blocked = await owner.post(f"{B}/slack/stage", json={"to": "live", "version": 1})
    assert blocked.status_code == 400
    assert blocked.json()["detail"]["code"] == "not_ready"
    assert set(blocked.json()["detail"]["blockers"]) == {"config", "live_test", "facts"}

    await save_platform_config(PLATFORM_WIDE, "slack", None, True, {}, {})
    tested = await staff.post(f"{B}/slack/auth-test", json={"note": "Sent a message to #tests", "version": 1})
    assert tested.status_code == 200, tested.text
    assert tested.json()["ops"]["auth_test"]["note"] == "Sent a message to #tests"
    verified = await staff.post(f"{B}/slack/facts-verified", json={"source_url": "https://api.slack.com/messaging/webhooks", "version": 2})
    assert verified.status_code == 200, verified.text

    live = await owner.post(f"{B}/slack/stage", json={"to": "live", "version": 3, "reason": "Tested in #tests"})
    assert live.status_code == 200, live.text
    ops = live.json()["ops"]
    assert ops["ops_stage"] == "live" and ops["rollout"] == {"scope": "ops_only", "workspace_ids": []}
    assert [h["to"] for h in ops["stage_history"]][-2:] == ["in_setup", "live"]


async def test_nonsense_changes_are_refused(make_client):
    owner, _ = await _staff(make_client, master=True)
    res = await owner.post(f"{B}/mastodon/stage", json={"to": "live", "version": 0})
    assert res.status_code == 400 and res.json()["detail"]["code"] == "invalid_transition"
    res = await owner.post(f"{B}/blog/stage", json={"to": "in_setup", "version": 0})
    assert res.status_code == 400 and res.json()["detail"]["code"] == "not_applicable"
    res = await owner.post(f"{B}/linkedin/stage", json={"to": "retired", "reason": "gone", "version": 0})
    assert res.status_code == 400 and res.json()["detail"]["code"] == "confirm_name"


async def test_a_platform_with_workspaces_connected_cannot_go_back_to_not_started(make_client):
    from app.pipelines.publish.token_store import save_token

    owner, _ = await _staff(make_client, master=True)
    await owner.post(f"{B}/discord/stage", json={"to": "in_setup", "version": 0})
    await save_token(
        workspace_id="ws-conn", platform="discord", access_token="x", refresh_token=None, expires_at=None,
        platform_user_id="1", username="u", connected_by="",
    )
    res = await owner.post(f"{B}/discord/stage", json={"to": "not_started", "version": 1})
    assert res.status_code == 400 and res.json()["detail"]["code"] == "has_connections"
    from app.db.mongo import workspace_connections
    await workspace_connections.delete_many({"workspace_id": "ws-conn"})


async def test_rollout_is_owner_only_and_selected_workspaces_must_exist(make_client):
    staff, _ = await _staff(make_client)
    owner, owner_user = await _staff(make_client, master=True)
    assert (await staff.put(f"{B}/slack/rollout", json={"scope": "everyone", "version": 0})).status_code == 403

    empty = await owner.put(f"{B}/slack/rollout", json={"scope": "selected", "workspace_ids": [], "version": 0})
    assert empty.status_code == 400 and empty.json()["detail"]["code"] == "no_workspaces"
    ghost = await owner.put(f"{B}/slack/rollout", json={"scope": "selected", "workspace_ids": ["nope"], "version": 0})
    assert ghost.status_code == 400 and ghost.json()["detail"]["code"] == "unknown_workspace"

    ws_id = await create_workspace(owner, "Rollout WS")
    ok = await owner.put(f"{B}/slack/rollout", json={"scope": "selected", "workspace_ids": [ws_id, ws_id], "version": 0})
    assert ok.status_code == 200, ok.text
    assert ok.json()["ops"]["rollout"] == {"scope": "selected", "workspace_ids": [ws_id]}
    stale = await owner.put(f"{B}/slack/rollout", json={"scope": "everyone", "version": 0})
    assert stale.status_code == 409


async def test_a_live_test_and_a_fact_check_need_something_to_apply_to(make_client):
    staff, _ = await _staff(make_client)
    res = await staff.post(f"{B}/spotify/auth-test", json={"note": "", "version": 0})
    assert res.status_code == 400 and res.json()["detail"]["code"] == "not_applicable"
    res = await staff.post(f"{B}/slack/facts-verified", json={"source_url": "not a link at all", "version": 0})
    assert res.status_code == 400 and res.json()["detail"]["code"] == "bad_source"


async def test_staff_actions_are_written_to_the_activity_log(make_client):
    owner, owner_user = await _staff(make_client, master=True)
    await activity_entries.delete_many({"category": "platform_ops", "metadata.platform_key": "slack"})
    await owner.post(f"{B}/slack/stage", json={"to": "in_setup", "version": 0, "reason": "Starting"})
    row = await activity_entries.find_one({"category": "platform_ops", "metadata.platform_key": "slack"})
    assert row, "the stage change should be logged"
    assert row["metadata"]["event"] == "platform.stage_changed"
    assert row["metadata"]["before"] == "not_started" and row["metadata"]["after"] == "in_setup"
    assert row["metadata"]["reason"] == "Starting" and row["visibility"] == "admins"
    await activity_entries.delete_many({"category": "platform_ops", "metadata.platform_key": "slack"})


async def test_settings_with_an_unsafe_address_or_link_are_refused_before_they_are_stored(make_client):
    owner, _ = await _staff(make_client, master=True)
    bad_hook = await owner.put(f"{B}/slack/config", json={"enabled": True, "fields": {}, "secrets": {"webhook_url": "https://10.0.0.1/x"}})
    assert bad_hook.status_code == 400 and "private network" in bad_hook.json()["detail"]
    bad_link = await owner.put(f"{B}/medium/config", json={"enabled": True, "fields": {"compose_url_template": "http://x.example.com/c?text={text}"}, "secrets": {}})
    assert bad_link.status_code == 400 and "https" in bad_link.json()["detail"]
    assert await platform_configs.find_one({"platform": {"$in": ["slack", "medium"]}}) is None

    good = await owner.put(f"{B}/slack/config", json={"enabled": True, "fields": {}, "secrets": {"webhook_url": "https://8.8.8.8/hook"}})
    assert good.status_code == 200, good.text
    saved = await platform_configs.find_one({"platform": "slack"})
    assert saved["workspace_id"] == PLATFORM_WIDE
    assert "8.8.8.8" not in str(good.json())  # the address is a secret and is never sent back


async def test_a_test_send_builds_the_link_and_does_not_record_the_live_test(make_client):
    owner, owner_user = await _staff(make_client, master=True)
    none_yet = await owner.post(f"{B}/medium/test", json={})
    assert none_yet.status_code == 400 and none_yet.json()["detail"]["code"] == "no_settings"
    await save_platform_config(PLATFORM_WIDE, "medium", None, True, {"compose_url_template": "https://x.example.com/c?text={text}"}, {})
    res = await owner.post(f"{B}/medium/test", json={"text": "Hello there"})
    assert res.status_code == 200, res.text
    assert res.json()["kind"] == "link" and res.json()["manual_action_url"] == "https://x.example.com/c?text=Hello%20there"
    assert (await get_ops(_def("medium")))["auth_test"] is None

    not_testable = await owner.post(f"{B}/linkedin/test", json={})
    assert not_testable.status_code == 400 and not_testable.json()["detail"]["code"] == "not_testable"


# ── pause, resume, retire ─────────────────────────────────────────────────────

async def _scheduled_post(client_and_profile, ws_name: str, platform: str = "LinkedIn", slug: str = "linkedin"):
    """A workspace with a connected account and one approved, queued post for that platform."""
    client, profile = client_and_profile
    ws_id = await create_workspace(client, ws_name)
    await _connect(ws_id, slug)
    piece_id = await _seed(ws_id, profile["id"], platform=platform)
    await _approve(client, ws_id, piece_id)
    res = await client.patch(f"/api/v1/content/pieces/{piece_id}/schedule", json={"scheduled_at": _later().isoformat()}, headers=H(ws_id))
    assert res.status_code == 200, res.text
    return client, ws_id, piece_id


async def test_pausing_needs_a_reason_and_holds_scheduled_posts_without_cancelling_them(make_client, signup_user):
    staff, _ = await _staff(make_client)
    client, ws_id, piece_id = await _scheduled_post(await signup_user(), "Pause WS 1")
    before = await content_pieces.find_one({"piece_id": piece_id})

    no_reason = await staff.post(f"{B}/linkedin/stage", json={"to": "paused", "version": 0})
    assert no_reason.status_code == 400 and no_reason.json()["detail"]["code"] == "reason_required"

    res = await staff.post(f"{B}/linkedin/stage", json={"to": "paused", "reason": "Token problem", "version": 0})
    assert res.status_code == 200, res.text
    assert res.json()["ops"]["ops_stage"] == "paused" and res.json()["ops"]["paused"]["reason"] == "Token problem"

    held = await content_pieces.find_one({"piece_id": piece_id})
    assert held["publish_status"] == "queued" and held["hold"]["reason"] == "platform_paused"
    assert held["publish_scheduled_at"] == before["publish_scheduled_at"]
    assert "paused" in held["schedule_note"].lower()

    notice = await activity_entries.find_one({"workspace_id": ws_id, "channel": "linkedin", "title": "LinkedIn is paused"})
    assert notice and "1 scheduled post is on hold" in notice["description"]
    assert "Nothing was cancelled" in notice["description"]


async def test_a_paused_platform_refuses_new_scheduling_and_publish_now(make_client, signup_user):
    staff, _ = await _staff(make_client)
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Pause WS 2")
    await _connect(ws_id)
    piece_id = await _seed(ws_id, profile["id"])
    await _approve(client, ws_id, piece_id)
    await staff.post(f"{B}/linkedin/stage", json={"to": "paused", "reason": "Checking", "version": 0})

    sched = await client.patch(f"/api/v1/content/pieces/{piece_id}/schedule", json={"scheduled_at": _later().isoformat()}, headers=H(ws_id))
    assert sched.status_code == 409 and sched.json()["detail"]["code"] == "PLATFORM_PAUSED"
    now = await client.post("/api/v1/publish/now", json={"piece_id": piece_id}, headers=H(ws_id))
    assert now.status_code == 409 and now.json()["detail"]["code"] == "PLATFORM_PAUSED"
    assert (await content_pieces.find_one({"piece_id": piece_id}))["publish_status"] in (None, "pending")


async def test_the_worker_never_sends_a_held_post(make_client, signup_user):
    staff, _ = await _staff(make_client)
    client, ws_id, piece_id = await _scheduled_post(await signup_user(), "Pause WS 3")
    await staff.post(f"{B}/linkedin/stage", json={"to": "paused", "reason": "Checking", "version": 0})
    await content_pieces.update_one({"piece_id": piece_id}, {"$set": {"publish_scheduled_at": datetime.now(timezone.utc) - timedelta(minutes=1)}})

    fake = _ok_publisher(piece_id)
    with patch("app.workers.scheduled_posts.get_publisher", return_value=fake):
        await worker.process_scheduled_posts.__wrapped__()
    fake.publish.assert_not_awaited()
    doc = await content_pieces.find_one({"piece_id": piece_id})
    assert doc["publish_status"] == "queued" and doc["hold"]["reason"] == "platform_paused"


async def test_a_post_claimed_just_before_the_pause_is_put_back_not_sent(make_client, signup_user):
    client, ws_id, piece_id = await _scheduled_post(await signup_user(), "Pause WS 4")
    # The platform is paused but this post was never marked held, as if the pause landed after the post was claimed.
    definition = get_platform("linkedin")
    await save_ops(definition, {"ops_stage": "paused", "paused": {"reason": "x"}}, expected_version=0, actor_id="test")
    await content_pieces.update_one({"piece_id": piece_id}, {"$set": {"publish_scheduled_at": datetime.now(timezone.utc) - timedelta(minutes=1)}})

    fake = _ok_publisher(piece_id)
    with patch("app.workers.scheduled_posts.get_publisher", return_value=fake):
        await worker.process_scheduled_posts.__wrapped__()
    fake.publish.assert_not_awaited()
    doc = await content_pieces.find_one({"piece_id": piece_id})
    assert doc["publish_status"] == "queued" and doc["hold"]["reason"] == "platform_paused"


async def test_only_the_paused_platforms_posts_are_held(make_client, signup_user):
    staff, _ = await _staff(make_client)
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Pause WS 5")
    await _connect(ws_id, "linkedin")
    await _connect(ws_id, "facebook")
    ids = {}
    for platform, slug in (("LinkedIn", "linkedin"), ("Facebook", "facebook")):
        piece_id = await _seed(ws_id, profile["id"], platform=platform)
        await _approve(client, ws_id, piece_id)
        res = await client.patch(f"/api/v1/content/pieces/{piece_id}/schedule", json={"scheduled_at": _later().isoformat()}, headers=H(ws_id))
        assert res.status_code == 200, res.text
        ids[slug] = piece_id
    await staff.post(f"{B}/linkedin/stage", json={"to": "paused", "reason": "Checking", "version": 0})
    assert "hold" in await content_pieces.find_one({"piece_id": ids["linkedin"]})
    assert "hold" not in await content_pieces.find_one({"piece_id": ids["facebook"]})


async def test_only_the_owner_can_resume_and_approved_posts_go_back_to_the_queue(make_client, signup_user):
    staff, _ = await _staff(make_client)
    owner, _ = await _staff(make_client, master=True)
    client, ws_id, piece_id = await _scheduled_post(await signup_user(), "Resume WS 1")
    paused = await staff.post(f"{B}/linkedin/stage", json={"to": "paused", "reason": "Checking", "version": 0})
    version = paused.json()["ops"]["version"]

    denied = await staff.post(f"{B}/linkedin/stage", json={"to": "live", "version": version})
    assert denied.status_code == 403 and denied.json()["detail"]["code"] == "owner_only"

    resumed = await owner.post(f"{B}/linkedin/stage", json={"to": "live", "version": version})
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["ops"]["ops_stage"] == "live" and resumed.json()["ops"]["paused"] is None
    doc = await content_pieces.find_one({"piece_id": piece_id})
    assert doc["publish_status"] == "queued" and "hold" not in doc and "schedule_note" not in doc
    back = await activity_entries.find_one({"workspace_id": ws_id, "title": "LinkedIn is back"})
    assert back and "go out as planned" in back["description"]


async def test_a_post_whose_time_passed_is_not_sent_on_resume_it_waits_for_a_new_time(make_client, signup_user):
    staff, _ = await _staff(make_client)
    owner, _ = await _staff(make_client, master=True)
    client, ws_id, piece_id = await _scheduled_post(await signup_user(), "Resume WS 2")
    paused = await staff.post(f"{B}/linkedin/stage", json={"to": "paused", "reason": "Checking", "version": 0})
    await content_pieces.update_one({"piece_id": piece_id}, {"$set": {"publish_scheduled_at": datetime.now(timezone.utc) - timedelta(hours=2)}})
    await owner.post(f"{B}/linkedin/stage", json={"to": "live", "version": paused.json()["ops"]["version"]})
    doc = await content_pieces.find_one({"piece_id": piece_id})
    assert doc["publish_status"] == "pending" and "hold" not in doc
    assert "Pick a new time" in doc["schedule_note"]


async def test_a_held_post_that_is_no_longer_approved_stays_held_on_resume(make_client, signup_user):
    staff, _ = await _staff(make_client)
    owner, _ = await _staff(make_client, master=True)
    client, ws_id, piece_id = await _scheduled_post(await signup_user(), "Resume WS 3")
    paused = await staff.post(f"{B}/linkedin/stage", json={"to": "paused", "reason": "Checking", "version": 0})
    await content_pieces.update_one({"piece_id": piece_id}, {"$set": {"approval_status": "rejected"}})
    await owner.post(f"{B}/linkedin/stage", json={"to": "live", "version": paused.json()["ops"]["version"]})
    doc = await content_pieces.find_one({"piece_id": piece_id})
    assert doc["hold"]["reason"] == "platform_paused" and "Still on hold" in doc["schedule_note"]


async def test_retiring_asks_for_a_reason_and_the_name_then_holds_posts_for_good(make_client, signup_user):
    owner, _ = await _staff(make_client, master=True)
    client, ws_id, piece_id = await _scheduled_post(await signup_user(), "Retire WS 1")

    impact = await owner.get(f"{B}/linkedin/impact")
    assert impact.status_code == 200 and impact.json()["scheduled_posts"] >= 1

    no_name = await owner.post(f"{B}/linkedin/stage", json={"to": "retired", "reason": "Closing it", "version": 0})
    assert no_name.status_code == 400 and no_name.json()["detail"]["code"] == "confirm_name"
    res = await owner.post(f"{B}/linkedin/stage", json={"to": "retired", "reason": "Closing it", "confirm_name": "linkedin", "version": 0})
    assert res.status_code == 200, res.text
    assert res.json()["ops"]["retired"]["reason"] == "Closing it"

    doc = await content_pieces.find_one({"piece_id": piece_id})
    assert doc["hold"]["reason"] == "platform_retired" and doc["publish_status"] == "queued"
    assert "copy" in doc["schedule_note"].lower()
    assert (await platform_availability("linkedin", ws_id)).value == "retired"

    sched = await client.patch(f"/api/v1/content/pieces/{piece_id}/schedule", json={"scheduled_at": _later().isoformat()}, headers=H(ws_id))
    assert sched.status_code == 409 and sched.json()["detail"]["code"] == "PLATFORM_RETIRED"


async def test_a_paused_platform_that_is_then_retired_re_marks_its_held_posts(make_client, signup_user):
    staff, _ = await _staff(make_client)
    owner, _ = await _staff(make_client, master=True)
    client, ws_id, piece_id = await _scheduled_post(await signup_user(), "Retire WS 2")
    paused = await staff.post(f"{B}/linkedin/stage", json={"to": "paused", "reason": "Checking", "version": 0})
    retired = await owner.post(f"{B}/linkedin/stage", json={"to": "retired", "reason": "Gone", "confirm_name": "LinkedIn", "version": paused.json()["ops"]["version"]})
    assert retired.status_code == 200, retired.text
    assert (await content_pieces.find_one({"piece_id": piece_id}))["hold"]["reason"] == "platform_retired"


async def test_a_retired_platform_can_be_reinstated_by_the_owner_only(make_client):
    staff, _ = await _staff(make_client)
    owner, _ = await _staff(make_client, master=True)
    retired = await owner.post(f"{B}/linkedin/stage", json={"to": "retired", "reason": "Gone", "confirm_name": "LinkedIn", "version": 0})
    version = retired.json()["ops"]["version"]
    assert (await staff.post(f"{B}/linkedin/stage", json={"to": "in_setup", "version": version})).status_code == 403
    back = await owner.post(f"{B}/linkedin/stage", json={"to": "in_setup", "version": version})
    assert back.status_code == 200 and back.json()["ops"]["ops_stage"] == "in_setup" and back.json()["ops"]["retired"] is None


async def test_staff_can_search_workspaces_by_name_for_a_rollout(make_client, signup_user):
    staff, _ = await _staff(make_client)
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Zebra Rollout Search WS")
    res = await staff.get(f"{B}/workspaces/search", params={"q": "zebra rollout"})
    assert res.status_code == 200, res.text
    assert {"id": ws_id, "name": "Zebra Rollout Search WS"} in res.json()["workspaces"]
    by_id = await staff.get(f"{B}/workspaces/search", params={"q": ws_id})
    assert [w["id"] for w in by_id.json()["workspaces"]] == [ws_id]
    assert (await staff.get(f"{B}/workspaces/search", params={"q": "  "})).json() == {"workspaces": []}
    assert (await client.get(f"{B}/workspaces/search", params={"q": "zebra"})).status_code == 403


async def test_the_calendar_marks_a_held_post(make_client, signup_user):
    staff, _ = await _staff(make_client)
    client, ws_id, piece_id = await _scheduled_post(await signup_user(), "Calendar Hold WS")
    when = datetime.now(timezone.utc)

    def day_pieces(res):
        return [p for pieces in res.json()["days"].values() for p in pieces if p["id"] == piece_id]

    url = f"/api/v1/analytics/calendar?year={when.year}&month={when.month}"
    await content_pieces.update_one({"piece_id": piece_id}, {"$set": {"publish_scheduled_at": when + timedelta(hours=1)}})
    before = day_pieces(await client.get(url, headers=H(ws_id)))
    assert before and before[0]["held"] is False

    await staff.post(f"{B}/linkedin/stage", json={"to": "paused", "reason": "Checking", "version": 0})
    after = day_pieces(await client.get(url, headers=H(ws_id)))
    assert after and after[0]["held"] is True


async def test_platform_settings_are_for_staff_not_for_the_owner_of_one_workspace(make_client, signup_user):
    """Settings apply to every workspace, so a workspace owner must not be able to read or change them."""
    workspace_owner, profile = await signup_user()
    ws_id = await create_workspace(workspace_owner, "Ordinary Owner WS")
    staff, _ = await _staff(make_client)
    owner, _ = await _staff(make_client, master=True)
    body = {"enabled": True, "fields": {}, "secrets": {"webhook_url": "https://8.8.8.8/hook"}}

    for call in (
        workspace_owner.get(f"{B}/configs", headers=H(ws_id)),
        workspace_owner.get(f"{B}/slack/config", headers=H(ws_id)),
        workspace_owner.put(f"{B}/slack/config", json=body, headers=H(ws_id)),
        workspace_owner.delete(f"{B}/slack/config", headers=H(ws_id)),
    ):
        assert (await call).status_code == 403

    assert (await staff.get(f"{B}/configs")).status_code == 200
    assert (await staff.put(f"{B}/slack/config", json=body)).status_code == 403
    assert (await staff.delete(f"{B}/slack/config")).status_code == 403
    assert (await owner.put(f"{B}/slack/config", json=body)).status_code == 200
    assert (await owner.delete(f"{B}/slack/config")).status_code == 200
