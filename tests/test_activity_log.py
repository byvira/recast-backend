"""Integration tests for the Activity Log (app.shared.activity + /api/v1/activity).

Covers the two lanes, per-row visibility (workspace / admins / member-private),
decisions flowing back to Remy's and Odette's own collections, idempotent
re-projection, filters and cursor pagination.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.agents.personal.signals import emit_signal
from app.db.mongo import activity_entries, personal_signals, workspace_flags, workspace_insights
from app.shared.activity import project_odette_flag, project_odette_insight, record_system
from app.shared.events import emit_event
from tests.conftest import create_workspace, invite_and_accept, signup_new_user


def _ws_headers(ws_id: str) -> dict:
    return {"X-Workspace-Id": ws_id}


async def _list(client, ws_id, **params):
    res = await client.get("/api/v1/activity", params=params, headers=_ws_headers(ws_id))
    assert res.status_code == 200, res.text
    return res.json()


async def _remy_signal(ws_id: str, user_id: str) -> str:
    return await emit_signal(
        workspace_id=ws_id,
        user_id=user_id,
        pipeline_type="text",
        signal_type="voice_drift",
        severity="medium",
        metric={"name": "voice_distance", "value": 0.42, "baseline": 0.2, "threshold": 0.35},
        window={"kind": "rolling", "n": 10},
        evidence_refs=[{"collection": "content_pieces", "id": "piece-1"}],
        member_message="hey - this one reads a little off from how you usually sound.",
        supervisor_note="voice drift 0.42 > 0.35",
    )


async def _odette_insight(ws_id: str) -> str:
    iid = str(uuid4())
    now = datetime.now(timezone.utc)
    await workspace_insights.insert_one({
        "_id": iid, "workspace_id": ws_id, "kind": "recommendation",
        "title": "LinkedIn is carrying your reach", "body_persona": "Lean into it this week.",
        "rationale": "70% of reach", "priority": "high", "status": "new",
        "created_at": now, "updated_at": now,
    })
    await project_odette_insight(await workspace_insights.find_one({"_id": iid}))
    return iid


async def _odette_flag(ws_id: str) -> str:
    fid = str(uuid4())
    await workspace_flags.insert_one({
        "_id": fid, "workspace_id": ws_id, "flag_type": "daily_publish_cap",
        "detection": "rule", "severity": "critical",
        "summary_persona": "Publishing hit 12 in the last 24h against a 10/day cap.",
        "detail": {}, "metric": {"name": "published_24h", "value": 12, "limit": 10},
        "status": "open", "created_at": datetime.now(timezone.utc), "resolved_at": None,
    })
    await project_odette_flag(await workspace_flags.find_one({"_id": fid}))
    return fid


# ─────────────────────────────────────────────────────────────────────────────
# Lanes + visibility
# ─────────────────────────────────────────────────────────────────────────────

async def test_remy_feedback_is_active_and_private_to_its_member(api_client, make_client):
    owner = await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Remy Privacy", tier="large")
    editor_client, editor = await invite_and_accept(api_client, make_client, ws_id, "editor")

    await _remy_signal(ws_id, editor["id"])

    mine = await _list(editor_client, ws_id, lane="active")
    assert [i["source"] for i in mine["items"]] == ["remy_signal"]
    item = mine["items"][0]
    assert item["actor"]["name"] == "Remy"
    assert item["category"] == "recommendation"
    assert item["status"] == "warning"

    # Not even the workspace owner sees another member's Remy feedback.
    owners_view = await _list(api_client, ws_id, lane="active")
    assert owners_view["items"] == []
    assert owner["id"] != editor["id"]


async def test_odette_items_are_admin_only(api_client, make_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Odette Gate", tier="large")
    editor_client, _ = await invite_and_accept(api_client, make_client, ws_id, "editor")

    await _odette_insight(ws_id)
    await _odette_flag(ws_id)

    admin_view = await _list(api_client, ws_id, lane="active")
    assert {i["source"] for i in admin_view["items"]} == {"odette_insight", "odette_flag"}
    flag = next(i for i in admin_view["items"] if i["source"] == "odette_flag")
    assert flag["status"] == "failed"          # critical severity
    assert flag["category"] == "workspace_alert"

    editor_view = await _list(editor_client, ws_id, lane="active")
    assert editor_view["items"] == []


async def test_workspace_events_land_in_passive_for_every_member(api_client, make_client):
    owner = await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Passive Feed", tier="large")
    editor_client, _ = await invite_and_accept(api_client, make_client, ws_id, "editor")

    await emit_event(
        event_type="pipeline.run_completed", pipeline_type="text",
        workspace_id=ws_id, actor_user_id=owner["id"], actor_role="owner",
        payload={"session_id": "sess-1", "pieces": 3, "failed": 1, "duration_ms": 42_000,
                 "platforms": ["LinkedIn", "Twitter/X", "Instagram"], "title": "Async teams"},
        idempotency_key=f"test-run:{uuid4()}",
    )

    for client in (api_client, editor_client):
        feed = await _list(client, ws_id, lane="passive")
        runs = [i for i in feed["items"] if i["category"] == "content_generated"]
        assert len(runs) == 1
        run = runs[0]
        assert run["status"] == "warning"       # one output failed
        assert run["metadata"]["branchesCount"] == 3
        assert run["metadata"]["durationSeconds"] == 42
        assert run["actor"]["type"] == "team_member"   # owner → "Admins & Owners"


async def test_content_created_events_are_not_projected(api_client):
    owner = await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "No Per-Piece Rows", tier="large")
    await emit_event(
        event_type="content.created", pipeline_type="text",
        workspace_id=ws_id, actor_user_id=owner["id"], actor_role="owner",
        payload={"content_id": "p1", "content_ref": {"collection": "content_pieces", "id": "p1"}},
        idempotency_key=f"test-created:{uuid4()}",
    )
    feed = await _list(api_client, ws_id, lane="passive")
    assert [i for i in feed["items"] if (i.get("targetId") == "p1")] == []


# ─────────────────────────────────────────────────────────────────────────────
# Decisions
# ─────────────────────────────────────────────────────────────────────────────

async def test_accepting_remy_feedback_updates_the_signal_and_moves_lanes(api_client):
    me = await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Decide Remy", tier="large")
    signal_id = await _remy_signal(ws_id, me["id"])

    entry_id = f"remy_signal:{signal_id}"
    res = await api_client.post(
        f"/api/v1/activity/{entry_id}/decision", json={"decision": "accept"},
        headers=_ws_headers(ws_id),
    )
    assert res.status_code == 200, res.text
    assert res.json()["lane"] == "passive"
    assert res.json()["decision"] == "accepted"

    signal = await personal_signals.find_one({"_id": signal_id})
    assert signal["status"] == "acknowledged"

    assert (await _list(api_client, ws_id, lane="active"))["items"] == []
    history = await _list(api_client, ws_id, lane="passive")
    assert any(i["id"] == entry_id for i in history["items"])

    # A second decision on the same item is a conflict, not a silent overwrite.
    again = await api_client.post(
        f"/api/v1/activity/{entry_id}/decision", json={"decision": "dismiss"},
        headers=_ws_headers(ws_id),
    )
    assert again.status_code == 409


async def test_dismissing_odette_flag_mutes_it(api_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Decide Flag", tier="large")
    fid = await _odette_flag(ws_id)

    res = await api_client.post(
        f"/api/v1/activity/odette_flag:{fid}/decision", json={"decision": "dismiss"},
        headers=_ws_headers(ws_id),
    )
    assert res.status_code == 200, res.text
    assert (await workspace_flags.find_one({"_id": fid}))["status"] == "muted"


async def test_odette_page_decisions_sync_to_activity(api_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Sync From Odette", tier="large")
    iid = await _odette_insight(ws_id)

    res = await api_client.post(
        f"/api/v1/supervisor/insights/{iid}/status", json={"status": "actioned"},
        headers=_ws_headers(ws_id),
    )
    assert res.status_code == 200, res.text

    row = await activity_entries.find_one({"_id": f"odette_insight:{iid}"})
    assert row["lane"] == "passive"
    assert row["decision"] == {"outcome": "accepted"}
    assert row["expires_at"] is not None


async def test_snooze_hides_until_it_lapses(api_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Snooze", tier="large")
    iid = await _odette_insight(ws_id)
    entry_id = f"odette_insight:{iid}"

    res = await api_client.post(
        f"/api/v1/activity/{entry_id}/decision", json={"decision": "snooze", "snooze_hours": 1},
        headers=_ws_headers(ws_id),
    )
    assert res.status_code == 200, res.text
    assert (await _list(api_client, ws_id, lane="active"))["items"] == []

    # Lapse the snooze — the item is back, still undecided.
    await workspace_insights.update_one(
        {"_id": iid}, {"$set": {"snoozed_until": datetime.now(timezone.utc) - timedelta(minutes=1)}}
    )
    await project_odette_insight(await workspace_insights.find_one({"_id": iid}))
    active = await _list(api_client, ws_id, lane="active")
    assert [i["id"] for i in active["items"]] == [entry_id]


async def test_member_cannot_decide_on_odette_items(api_client, make_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "No Member Decisions", tier="large")
    editor_client, _ = await invite_and_accept(api_client, make_client, ws_id, "editor")
    fid = await _odette_flag(ws_id)

    res = await editor_client.post(
        f"/api/v1/activity/odette_flag:{fid}/decision", json={"decision": "accept"},
        headers=_ws_headers(ws_id),
    )
    assert res.status_code == 404      # not visible to them at all
    assert (await workspace_flags.find_one({"_id": fid}))["status"] == "open"


# ─────────────────────────────────────────────────────────────────────────────
# Projection mechanics, filters, pagination
# ─────────────────────────────────────────────────────────────────────────────

async def test_system_rows_are_idempotent_per_key(api_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Retry Chain", tier="large")
    for status, title in (("warning", "Retrying"), ("success", "Recovered")):
        await record_system(
            workspace_id=ws_id, key="publish:piece-9", actor_name="Publishing scheduler",
            category="post_published", title=title, description="", status=status,
        )
    rows = await activity_entries.find({"workspace_id": ws_id, "_id": "system:publish:piece-9"}).to_list(5)
    assert len(rows) == 1
    assert rows[0]["title"] == "Recovered"


async def test_filters_and_cursor_pagination(api_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Paging", tier="large")
    base = datetime.now(timezone.utc)
    for n in range(5):
        await record_system(
            workspace_id=ws_id, key=f"page:{n}", actor_name="Connection monitor",
            category="account_connected", title=f"Renewed {n}", description="",
            status="failed" if n == 0 else "success",
            occurred_at=base - timedelta(minutes=n),
        )

    first = await _list(api_client, ws_id, lane="passive", limit=2)
    assert first["total"] == 5
    assert [i["title"] for i in first["items"]] == ["Renewed 0", "Renewed 1"]
    second = await _list(api_client, ws_id, lane="passive", limit=2, cursor=first["next_cursor"])
    assert [i["title"] for i in second["items"]] == ["Renewed 2", "Renewed 3"]

    failed = await _list(api_client, ws_id, lane="passive", status="failed")
    assert [i["title"] for i in failed["items"]] == ["Renewed 0"]
    searched = await _list(api_client, ws_id, lane="passive", q="renewed 3")
    assert [i["title"] for i in searched["items"]] == ["Renewed 3"]
    by_actor = await _list(api_client, ws_id, lane="passive", actor_type="system_cron")
    assert by_actor["total"] == 5
    by_person = await _list(api_client, ws_id, lane="passive", actor_type="user")
    assert by_person["total"] == 0


async def test_activity_requires_membership(api_client, make_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Private WS", tier="large")
    outsider = make_client()
    await signup_new_user(outsider)
    res = await outsider.get("/api/v1/activity", headers=_ws_headers(ws_id))
    assert res.status_code == 403


# ─────────────────────────────────────────────────────────────────────────────
# Control Tower
# ─────────────────────────────────────────────────────────────────────────────

async def test_control_tower_shows_live_runs_with_real_progress(api_client):
    from app.shared.activity.runs import end_run, start_run, tracked_run, update_run

    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Tower Live", tier="large")

    await start_run(workspace_id=ws_id, run_id="run-a", kind="campaign",
                    title="Launch week", project="Acme", steps_total=4)
    await update_run(ws_id, "run-a", stage="Writing hooks", steps_done=1)

    res = await api_client.get("/api/v1/activity/control-tower", headers=_ws_headers(ws_id))
    assert res.status_code == 200, res.text
    live = res.json()["live"]
    assert [r["id"] for r in live] == ["run-a"]
    assert live[0]["progress"] == 25
    assert live[0]["stage"] == "Writing hooks"
    assert live[0]["eta"] != ""            # measured from its own pace (1 of 4 done)

    await end_run(ws_id, "run-a")
    async with tracked_run(workspace_id=ws_id, run_id="run-b", kind="text", title="x"):
        mid = (await api_client.get("/api/v1/activity/control-tower", headers=_ws_headers(ws_id))).json()
        assert [r["id"] for r in mid["live"]] == ["run-b"]
        # No step info and no run history yet → no invented ETA.
        assert mid["live"][0]["eta"] == ""
    after = (await api_client.get("/api/v1/activity/control-tower", headers=_ws_headers(ws_id))).json()
    assert after["live"] == []


async def test_control_tower_completed_lists_successful_work_only(api_client):
    owner = await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Tower Done", tier="large")

    await emit_event(
        event_type="pipeline.run_completed", pipeline_type="text",
        workspace_id=ws_id, actor_user_id=owner["id"], actor_role="owner",
        payload={"session_id": "s-ok", "pieces": 2, "failed": 0, "duration_ms": 5000,
                 "platforms": ["LinkedIn", "Instagram"], "title": "Hiring update"},
        idempotency_key=f"tower-ok:{uuid4()}",
    )
    await record_system(
        workspace_id=ws_id, key="publish:bad", actor_name="Publishing scheduler",
        category="post_published", title="Scheduled post to linkedin failed",
        description="boom", status="failed",
    )

    res = await api_client.get("/api/v1/activity/control-tower", headers=_ws_headers(ws_id))
    completed = res.json()["completed"]
    assert len(completed) == 1
    card = completed[0]
    assert card["kind"] == "generated"
    assert card["title"] == "Hiring update"
    assert card["project"] == "LinkedIn, Instagram"
    assert card["subtitle"] == "Generated 2 outputs"


# ─────────────────────────────────────────────────────────────────────────────
# Inbox
# ─────────────────────────────────────────────────────────────────────────────

async def _inbox(client, ws_id):
    res = await client.get("/api/v1/activity/inbox", headers=_ws_headers(ws_id))
    assert res.status_code == 200, res.text
    return res.json()


async def test_inbox_shows_what_needs_noticing_not_your_own_clicks(api_client, make_client):
    owner = await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Inbox", tier="large")
    editor_client, editor = await invite_and_accept(api_client, make_client, ws_id, "editor")

    await _odette_flag(ws_id)                                            # admin decision → owner only
    await _remy_signal(ws_id, editor["id"])                             # editor's own feedback
    await record_system(workspace_id=ws_id, key="pub:ok", actor_name="Publishing scheduler",
                        category="post_published", title="Scheduled post went live", description="")
    await record_system(workspace_id=ws_id, key="edit:mine", actor_name="", actor_user_id=owner["id"],
                        category="content_edited", title="Edited LinkedIn draft", description="")

    owner_titles = {i["title"] for i in (await _inbox(api_client, ws_id))["items"]}
    assert "Daily publishing cap reached" in owner_titles
    assert "Scheduled post went live" in owner_titles
    assert "Edited LinkedIn draft" not in owner_titles                   # their own click
    assert "This piece drifts from your usual voice" not in owner_titles  # editor's private Remy item

    editor_titles = {i["title"] for i in (await _inbox(editor_client, ws_id))["items"]}
    assert "This piece drifts from your usual voice" in editor_titles
    assert "Daily publishing cap reached" not in editor_titles


async def test_mark_read_keeps_pending_decisions_unread(api_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Inbox Read", tier="large")
    await _odette_flag(ws_id)
    await record_system(workspace_id=ws_id, key="pub:ok2", actor_name="Publishing scheduler",
                        category="post_published", title="Went live", description="")

    before = await _inbox(api_client, ws_id)
    assert before["unread"] == 2

    res = await api_client.post("/api/v1/activity/inbox/read", json={}, headers=_ws_headers(ws_id))
    assert res.status_code == 200
    after = await _inbox(api_client, ws_id)
    assert after["unread"] == 1                                          # the undecided flag
    assert [i["type"] for i in after["items"] if i["unread"]] == ["failed"]

    flag_item = next(i for i in after["items"] if i["unread"])
    await api_client.post("/api/v1/activity/inbox/read", json={"ids": [flag_item["id"]]},
                          headers=_ws_headers(ws_id))
    assert (await _inbox(api_client, ws_id))["unread"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# Read / unread / delete + sidebar count + Control Tower "up next"
# ─────────────────────────────────────────────────────────────────────────────

async def test_read_unread_and_sidebar_count(api_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Read State", tier="large")
    h = _ws_headers(ws_id)
    await record_system(workspace_id=ws_id, key="pub:a", actor_name="Publishing scheduler",
                        category="post_published", title="Went live A", description="")
    row_id = "system:pub:a"

    count = (await api_client.get("/api/v1/activity/unread-count", headers=h)).json()["unread"]
    assert count == 1
    [row] = [i for i in (await _list(api_client, ws_id, lane="passive"))["items"] if i["id"] == row_id]
    assert row["unread"] is True

    await api_client.post("/api/v1/activity/read", json={"ids": [row_id]}, headers=h)
    [row] = [i for i in (await _list(api_client, ws_id, lane="passive"))["items"] if i["id"] == row_id]
    assert row["unread"] is False
    assert (await api_client.get("/api/v1/activity/unread-count", headers=h)).json()["unread"] == 0

    res = await api_client.post("/api/v1/activity/read", json={"ids": [row_id], "unread": True}, headers=h)
    assert res.json()["unread"] == 1

    # Mark all read clears explicit "unread" flags too.
    await api_client.post("/api/v1/activity/read", json={}, headers=h)
    assert (await api_client.get("/api/v1/activity/unread-count", headers=h)).json()["unread"] == 0


async def test_own_actions_are_never_unread(api_client):
    me = await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Own", tier="large")
    await record_system(workspace_id=ws_id, key="edit:own", actor_name="", actor_user_id=me["id"],
                        category="content_edited", title="Edited", description="")
    [row] = [i for i in (await _list(api_client, ws_id, lane="passive"))["items"] if i["id"] == "system:edit:own"]
    assert row["unread"] is False


async def test_delete_hides_for_me_only_and_never_pending_decisions(api_client, make_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Delete", tier="large")
    editor_client, _ = await invite_and_accept(api_client, make_client, ws_id, "editor")
    h = _ws_headers(ws_id)
    await record_system(workspace_id=ws_id, key="pub:del", actor_name="Publishing scheduler",
                        category="post_published", title="Went live", description="")
    flag_id = await _odette_flag(ws_id)

    res = await api_client.post("/api/v1/activity/hide",
                                json={"ids": ["system:pub:del", f"odette_flag:{flag_id}"]}, headers=h)
    assert res.json() == {"removed": 1, "skipped": 1}

    mine = {i["id"] for i in (await _list(api_client, ws_id, lane="passive"))["items"]}
    assert "system:pub:del" not in mine
    theirs = {i["id"] for i in (await _list(editor_client, ws_id, lane="passive"))["items"]}
    assert "system:pub:del" in theirs                                  # audit trail intact for others
    active = {i["id"] for i in (await _list(api_client, ws_id, lane="active"))["items"]}
    assert f"odette_flag:{flag_id}" in active                          # pending decision stays


async def test_control_tower_lists_what_runs_next(api_client):
    from app.db.mongo import content_pieces, get_campaigns_collection

    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Up Next", tier="large")
    now = datetime.now(timezone.utc)
    await content_pieces.insert_one({
        "piece_id": str(uuid4()), "workspace_id": ws_id, "publish_status": "queued",
        "publish_scheduled_at": (now + timedelta(hours=3)).isoformat(), "publish_target": "linkedin",
        "content": "Hiring update\nbody", "deleted": False,
    })
    await content_pieces.insert_one({
        "piece_id": str(uuid4()), "workspace_id": ws_id, "publish_status": "queued",
        "publish_scheduled_at": (now + timedelta(days=3)).isoformat(), "content": "Too far out",
        "deleted": False,
    })
    await get_campaigns_collection().insert_one({
        "id": str(uuid4()), "workspace_id": ws_id, "name": "Launch week", "status": "active",
        "cadence": {"frequency": "daily", "next_run_at": now + timedelta(hours=1)}, "deleted": False,
    })

    upcoming = (await api_client.get("/api/v1/activity/control-tower", headers=_ws_headers(ws_id))).json()["upcoming"]
    assert [(u["kind"], u["title"]) for u in upcoming] == [
        ("campaign_run", "Launch week"), ("scheduled_post", "Hiring update"),
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Backfill (scripts/backfill_activity.py)
# ─────────────────────────────────────────────────────────────────────────────

async def test_backfill_projects_history_idempotently_without_suggestions(api_client):
    from app.db.mongo import workspace_events
    from scripts.backfill_activity import backfill

    owner = await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Backfill", tier="large")
    event_id = str(uuid4())
    # Written straight to the store, as pre-Activity-Log history would be.
    await workspace_events.insert_one({
        "_id": event_id, "event_id": event_id, "event_type": "pipeline.run_completed",
        "pipeline_type": "text", "workspace_id": ws_id, "actor_user_id": owner["id"],
        "actor_role": "owner", "occurred_at": datetime.now(timezone.utc).isoformat(),
        "idempotency_key": f"bf:{event_id}",
        "payload": {"session_id": "old-run", "pieces": 1, "platforms": ["LinkedIn"],
                    "title": "Old run", "trigger": "manual"},
    })

    dry = await backfill(execute=False, workspace_id=ws_id)
    assert dry["events"] == 1
    assert await activity_entries.count_documents({"workspace_id": ws_id}) == 0   # dry run wrote nothing

    await backfill(execute=True, workspace_id=ws_id)
    await backfill(execute=True, workspace_id=ws_id)                              # re-run is safe
    rows = await activity_entries.find({"workspace_id": ws_id}).to_list(10)
    assert [r["_id"] for r in rows] == [f"event:{event_id}"]                      # no next-step row
