"""Ekavo Health: the error log, the status collector and incidents, the public answer and the Ops routes. Real Mongo, no
provider calls."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.db.mongo import get_db, users
from app.health import errors, signals, status


async def _settle():
    await asyncio.gather(*list(errors._tasks), return_exceptions=True)


@pytest.fixture(autouse=True)
def _quiet_alerts(monkeypatch):
    sent: list = []

    async def capture(alert, rules):
        sent.append(alert)
        return {}

    from app.shared.llm_health import alerts

    monkeypatch.setattr(alerts, "deliver", capture)
    status.forget()
    signals.reset()
    yield sent


# ── the error log ────────────────────────────────────────────────────────

def test_the_same_failure_in_two_places_with_different_ids_is_one_issue():
    a = errors.fingerprint("server", "KeyError", "GET /api/v1/posts/3f2c9a7e-1111-4222-8333-444455556666", 'File "app/api/v1/x.py", line 10')
    b = errors.fingerprint("server", "KeyError", "GET /api/v1/posts/9d8e7f6a-1111-4222-8333-444455556666", 'File "app/api/v1/x.py", line 99')
    c = errors.fingerprint("server", "ValueError", "GET /api/v1/posts/9d8e", 'File "app/api/v1/x.py", line 99')
    assert a == b and a != c


async def test_errors_are_grouped_counted_and_scrubbed(_quiet_alerts):
    for _ in range(3):
        errors.capture(source="server", kind="RuntimeError", message="boom for me@example.com key gsk_abcdefghijklmnopqrstuv",
                       where="POST /api/v1/text/generate", stack='File "app/api/v1/text.py", line 5', workspace_id="ws-1")
        await _settle()
    issue = await errors.issues_col().find_one({"where": "POST /api/v1/text/generate"})
    assert issue["count"] == 3 and issue["status"] == "open" and issue["workspaces"] == ["ws-1"]
    assert "example.com" not in issue["last_message"] and "gsk_" not in issue["last_message"]
    assert await errors.events_col().count_documents({"issue_id": issue["_id"]}) == 3
    assert [a.kind for a in _quiet_alerts] == ["new_issue"]


async def test_a_resolved_issue_reopens_by_itself_and_an_ignored_one_stays_quiet(_quiet_alerts):
    errors.capture(source="job", kind="TimeoutError", message="slow", where="run audio")
    await _settle()
    issue = await errors.issues_col().find_one({"where": "run audio"})
    await errors.issues_col().update_one({"_id": issue["_id"]}, {"$set": {"status": "resolved"}})
    errors.capture(source="job", kind="TimeoutError", message="slow again", where="run audio")
    await _settle()
    back = await errors.issues_col().find_one({"_id": issue["_id"]})
    assert back["status"] == "open" and back.get("reopened_at")
    assert "reopened" in [a.kind for a in _quiet_alerts]

    await errors.issues_col().update_one({"_id": issue["_id"]}, {"$set": {"status": "ignored"}})
    _quiet_alerts.clear()
    errors.capture(source="job", kind="TimeoutError", message="still slow", where="run audio")
    await _settle()
    assert (await errors.issues_col().find_one({"_id": issue["_id"]}))["status"] == "ignored" and _quiet_alerts == []


def test_a_refusal_in_plain_words_is_not_an_error():
    from fastapi import HTTPException

    from app.billing.credits import CreditsExhausted

    assert errors.is_expected(HTTPException(status_code=404, detail="Not found."))
    assert errors.is_expected(CreditsExhausted({"message": "x"}))
    assert not errors.is_expected(HTTPException(status_code=500, detail="x"))
    assert not errors.is_expected(KeyError("x"))


async def test_a_screen_crash_reaches_the_log_and_browser_noise_does_not(make_client):
    client = make_client()
    res = await client.post("/api/v1/client-errors", json={"kind": "TypeError", "message": "x is undefined", "stack": "at Panel (/components/x.tsx:1)",
                                                            "page": "/dashboard/pipelines/image?x=1", "component": "image editor"})
    assert res.status_code == 204
    await client.post("/api/v1/client-errors", json={"message": "ResizeObserver loop completed with undelivered notifications."})
    await _settle()
    issue = await errors.issues_col().find_one({"source": "browser", "kind": "TypeError"})
    assert issue and issue["where"] == "/dashboard/pipelines/image > image editor"
    assert not await errors.issues_col().find_one({"title": {"$regex": "ResizeObserver"}})


async def test_a_scheduled_job_that_fails_is_logged_and_does_not_raise():
    from app.workers.jobs import Job, _guarded

    async def broken(ctx):
        raise RuntimeError("scheduler boom")

    assert await _guarded(Job("broken_job", broken))({}) is None
    await _settle()
    assert await errors.issues_col().find_one({"source": "scheduler", "where": "broken_job"})


# ── the status ───────────────────────────────────────────────────────────

def test_states_from_attempts_and_from_backup_providers():
    assert status.from_counts(2, 0) is None
    assert status.from_counts(10, 10) == "operational"
    assert status.from_counts(10, 8) == "degraded"
    assert status.from_counts(10, 3) == "down"
    assert status.from_providers({"groq": "down", "gemini": "healthy"}) == "operational"
    assert status.from_providers({"groq": "down", "gemini": "degraded"}) == "degraded"
    assert status.from_providers({"groq": "down", "gemini": "down"}) == "down"
    assert status.from_providers({"groq": "idle"}) == "operational"


def test_a_day_is_red_only_after_five_minutes_down():
    assert status.day_state({}) == "none"
    assert status.day_state({"operational": 1440}) == "operational"
    assert status.day_state({"operational": 1438, "down": 2}) == "degraded"
    assert status.day_state({"operational": 1430, "down": 10}) == "down"


async def test_a_part_down_five_minutes_opens_a_notice_and_closes_it_once_working(monkeypatch):
    states = {c["id"]: "operational" for c in status.COMPONENTS}
    clock = [datetime.now(timezone.utc)]

    async def fake_states():
        return dict(states)

    monkeypatch.setattr(status, "current_states", fake_states)
    monkeypatch.setattr(status, "_now", lambda: clock[0])
    states["pictures"] = "down"
    await status.collect()
    clock[0] += timedelta(minutes=6)
    await status.collect()
    opened = await status.incidents_col().find_one({"components": ["pictures"], "auto": True})
    assert opened and opened["status"] == "investigating" and opened["title"] == "Pictures is not working"

    states["pictures"] = "operational"
    await status.collect()
    clock[0] += timedelta(minutes=11)
    await status.collect()
    closed = await status.incidents_col().find_one({"_id": opened["_id"]})
    assert closed["status"] == "resolved" and closed["updates"][-1]["text"] == "This is working again."


async def test_after_a_crash_the_missing_time_is_recorded_as_an_outage(monkeypatch):
    now = datetime.now(timezone.utc)
    await get_db()["status_meta"].update_one({"_id": "heartbeat"}, {"$set": {"at": now - timedelta(minutes=20)}}, upsert=True)
    await get_db()["status_meta"].delete_one({"_id": "shutdown"})
    made = await status.on_startup()
    assert made and made["status"] == "resolved" and "about 20 minutes" in made["updates"][0]["text"]
    day = await get_db()["status_daily"].find_one({"_id": f"app:{(now - timedelta(minutes=20)).date().isoformat()}"})
    assert day and day["minutes"]["down"] >= 1
    # a deploy (clean shutdown) of a few minutes is not an outage
    await get_db()["status_meta"].update_one({"_id": "heartbeat"}, {"$set": {"at": now - timedelta(minutes=6)}}, upsert=True)
    await get_db()["status_meta"].update_one({"_id": "shutdown"}, {"$set": {"at": now - timedelta(minutes=5)}}, upsert=True)
    assert await status.on_startup() is None


async def test_planned_maintenance_shows_as_maintenance_while_it_runs(monkeypatch):
    async def fake_states():
        return {c["id"]: "operational" for c in status.COMPONENTS}

    monkeypatch.setattr(status, "current_states", fake_states)
    now = datetime.now(timezone.utc)
    await status.open_incident(kind="maintenance", title="Database upgrade", components=["app"], impact="maintenance", text="Short pause.",
                               status="scheduled", scheduled_for=now - timedelta(minutes=1), scheduled_until=now + timedelta(hours=1))
    states = await status.collect()
    assert states["app"] == "maintenance"
    summary = await status.summary()
    assert summary["overall"]["state"] == "maintenance" and summary["active"][0]["kind"] == "maintenance"


async def test_the_public_answer_names_no_provider(make_client):
    client = make_client()
    res = await client.get("/api/v1/public/status")
    assert res.status_code == 200
    body = res.text.lower()
    for provider in ("groq", "gemini", "cloudflare", "elevenlabs", "deepgram", "openrouter", "cloudinary", "resend"):
        assert provider not in body
    data = res.json()
    assert len(data["components"][0]["days"]) == status.DAYS and data["overall"]["state"] in status.STATES


async def test_email_and_upload_failures_show_on_the_status():
    for ok in (True, False, False, False):
        signals.record("email", ok)
    states = await status.current_states()
    assert states["email"] == "down"


# ── Ops ──────────────────────────────────────────────────────────────────

async def test_only_staff_work_errors_and_post_notices(signup_user):
    member, _ = await signup_user()
    staff, staff_profile = await signup_user()
    await users.update_one({"id": staff_profile["id"]}, {"$set": {"is_platform_staff": True}})
    try:
        assert (await member.get("/api/v1/ops/health/errors")).status_code == 403
        errors.capture(source="server", kind="KeyError", message="'title'", where="GET /api/v1/ops/test")
        await _settle()
        listed = (await staff.get("/api/v1/ops/health/errors")).json()
        number = next(i["number"] for i in listed["items"] if i["where"] == "GET /api/v1/ops/test")
        changed = await staff.patch(f"/api/v1/ops/health/errors/{number}", json={"status": "resolved", "note": "fixed in the next release"})
        assert changed.status_code == 200 and changed.json()["status"] == "resolved"
        assert [n["text"] for n in changed.json()["notes"]] == ["Marked resolved", "fixed in the next release"]

        made = await staff.post("/api/v1/ops/health/incidents", json={
            "title": "Pictures are slow", "components": ["pictures"], "impact": "degraded", "text": "We are looking into it."})
        assert made.status_code == 201
        incident_id = made.json()["id"]
        assert (await staff.post(f"/api/v1/ops/health/incidents/{incident_id}/updates", json={"status": "completed", "text": "x done"})).status_code == 400
        done = await staff.post(f"/api/v1/ops/health/incidents/{incident_id}/updates", json={"status": "resolved", "text": "Working again."})
        assert done.json()["status"] == "resolved"
        public = (await member.get(f"/api/v1/public/status/incidents/{incident_id}")).json()
        assert [u["status"] for u in public["updates"]] == ["resolved", "investigating"]
        bad = await staff.post("/api/v1/ops/health/incidents", json={"kind": "maintenance", "title": "Upgrade", "components": ["app"], "text": "Planned."})
        assert bad.status_code == 400
    finally:
        await users.update_one({"id": staff_profile["id"]}, {"$unset": {"is_platform_staff": ""}})


# ── the gaps closed on 2026-10-29 ────────────────────────────────────────

async def _settle_status():
    await asyncio.gather(*list(status._tasks), return_exceptions=True)


async def test_an_error_the_code_catches_and_only_logs_reaches_the_log():
    import logging

    errors.install_log_capture()
    try:
        logging.getLogger("app.pipelines.media.something").error("Could not draw the cover for %s", "piece-1")
        try:
            raise ValueError("bad frame")
        except ValueError:
            logging.getLogger("app.agents.personal.nodes").exception("Drift check failed")
        logging.getLogger("app.health.errors").error("the log's own failure is never captured")
        await asyncio.sleep(0)
        await _settle()
    finally:
        errors.uninstall_log_capture()
    plain = await errors.issues_col().find_one({"source": "logged", "kind": "Logged error", "where": {"$regex": "something"}})
    assert plain and plain["title"].endswith("Could not draw the cover for piece-1")
    caught = await errors.issues_col().find_one({"source": "logged", "kind": "ValueError"})
    assert caught and "bad frame" in caught["last_message"]
    assert not await errors.issues_col().find_one({"title": {"$regex": "own failure"}})


async def test_a_render_that_fails_for_our_reasons_is_logged_and_one_about_the_file_is_not():
    from app.video import jobs

    for code, n in (("ffmpeg_crashed", 1), ("bad_input", 2)):
        await get_db()["video_jobs"].insert_one({
            "id": f"j{n}", "workspace_id": "ws-r", "user_id": "u1", "status": "claimed", "lease_token": f"t{n}", "attempts": 9,
            "kind": "render", "duration_s": 10,
        })
        await jobs.fail(f"j{n}", f"t{n}", code, "It failed.", retryable=False, detail="decoder exited")
    await _settle()
    assert await errors.issues_col().find_one({"source": "worker", "kind": "ffmpeg_crashed"})
    assert not await errors.issues_col().find_one({"source": "worker", "kind": "bad_input"})


async def test_a_ticket_that_quotes_a_crash_reference_is_listed_on_the_error(make_client):
    client = make_client()
    await client.post("/api/v1/client-errors", json={"kind": "TypeError", "message": "cannot read x", "page": "/dashboard",
                                                    "component": "card", "ref": "CR-ABCD2345"})
    await _settle()
    linked = await errors.link_ticket("The card broke. Crash reference: CR-ABCD2345", {"id": "t-1", "number": 42, "subject": "Broken card"})
    assert linked == 1
    issue = await errors.issues_col().find_one({"source": "browser", "kind": "TypeError", "where": "/dashboard > card"})
    assert issue["tickets"][0]["number"] == 42


async def test_planned_maintenance_starts_and_finishes_by_itself(monkeypatch):
    async def fake_states():
        return {c["id"]: "operational" for c in status.COMPONENTS}

    monkeypatch.setattr(status, "current_states", fake_states)
    now = datetime.now(timezone.utc)
    doc = await status.open_incident(kind="maintenance", title="Storage move", components=["uploads"], impact="maintenance",
                                     text="Uploads pause for a moment.", status="scheduled",
                                     scheduled_for=now - timedelta(minutes=1), scheduled_until=now + timedelta(minutes=30))
    await status.collect()
    assert (await status.incidents_col().find_one({"_id": doc["_id"]}))["status"] == "in_progress"
    monkeypatch.setattr(status, "_now", lambda: now + timedelta(minutes=31))
    await status.collect()
    done = await status.incidents_col().find_one({"_id": doc["_id"]})
    assert done["status"] == "completed" and done["resolved_at"]


async def test_a_part_slow_for_half_an_hour_opens_a_notice_and_going_down_raises_it(monkeypatch):
    states = {c["id"]: "operational" for c in status.COMPONENTS}
    clock = [datetime.now(timezone.utc)]

    async def fake_states():
        return dict(states)

    monkeypatch.setattr(status, "current_states", fake_states)
    monkeypatch.setattr(status, "_now", lambda: clock[0])
    states["voice"] = "degraded"
    await status.collect()
    clock[0] += timedelta(minutes=20)
    await status.collect()
    assert not await status.incidents_col().find_one({"components": ["voice"]})
    clock[0] += timedelta(minutes=11)
    await status.collect()
    slow = await status.incidents_col().find_one({"components": ["voice"]})
    assert slow and slow["impact"] == "degraded" and slow["title"] == "Voice and audio is slow"
    states["voice"] = "down"
    clock[0] += timedelta(minutes=1)
    await status.collect()
    assert (await status.incidents_col().find_one({"_id": slow["_id"]}))["impact"] == "down"


async def test_status_emails_need_confirming_and_stop_with_their_link():
    await status.subscribe("Someone@Example.com")
    sub = await status.subscribers_col().find_one({"_id": "someone@example.com"})
    assert sub and sub["confirmed"] is False
    doc = await status.open_incident(title="Writing is slow", components=["writing"], impact="degraded", text="Looking into it.")
    await _settle_status()
    assert await status.email_subscribers(doc) == 0          # not confirmed yet
    assert await status.confirm(sub["token"])
    assert await status.email_subscribers(doc) == 1
    assert await status.unsubscribe(sub["token"])
    assert not await status.confirm(sub["token"])


async def test_a_notice_reaches_the_activity_of_active_workspaces(signup_user):
    from app.db.mongo import activity_entries

    client, profile = await signup_user()
    from tests.conftest import create_workspace

    ws = await create_workspace(client, "Status activity")
    await users.update_one({"id": profile["id"]}, {"$set": {"last_active": datetime.now(timezone.utc)}})
    doc = await status.open_incident(title="Pictures are slow", components=["pictures"], impact="degraded", text="We are on it.")
    await _settle_status()
    row = await activity_entries.find_one({"_id": f"system:status:{doc['_id']}:{ws}", "workspace_id": ws})
    assert row and row["title"] == "Pictures are slow" and row["status"] == "warning"
    await status.add_update(doc["_id"], status="resolved", text="Working again.", by="ops@example.com")
    await _settle_status()
    row = await activity_entries.find_one({"_id": f"system:status:{doc['_id']}:{ws}", "workspace_id": ws})
    assert row["status"] == "success" and row["description"] == "Working again."


async def test_a_notice_gives_every_active_workspace_its_own_row(signup_user):
    from app.db.mongo import activity_entries
    from tests.conftest import create_workspace

    found = []
    for name in ("Status first", "Status second"):
        client, profile = await signup_user()
        found.append(await create_workspace(client, name))
        await users.update_one({"id": profile["id"]}, {"$set": {"last_active": datetime.now(timezone.utc)}})
    doc = await status.open_incident(title="Voice is slow", components=["voice"], impact="degraded", text="Checking.")
    await _settle_status()
    for ws in found:
        assert await activity_entries.find_one({"_id": f"system:status:{doc['_id']}:{ws}", "workspace_id": ws})


async def test_a_notice_posted_from_an_error_links_both_ways_and_takes_a_write_up(signup_user):
    staff, staff_profile = await signup_user()
    await users.update_one({"id": staff_profile["id"]}, {"$set": {"is_platform_staff": True}})
    try:
        errors.capture(source="server", kind="KeyError", message="'brand'", where="GET /api/v1/linked")
        await _settle()
        number = (await errors.issues_col().find_one({"where": "GET /api/v1/linked"}))["number"]
        made = await staff.post("/api/v1/ops/health/incidents", json={
            "title": "Some pages fail", "components": ["app"], "impact": "degraded", "text": "Looking into it.", "error_number": number})
        incident_id = made.json()["id"]
        detail = (await staff.get(f"/api/v1/ops/health/errors/{number}")).json()
        assert detail["incident"]["id"] == incident_id
        early = await staff.patch(f"/api/v1/ops/health/incidents/{incident_id}", json={"writeup": "Too soon."})
        assert early.status_code == 400
        await staff.post(f"/api/v1/ops/health/incidents/{incident_id}/updates", json={"status": "resolved", "text": "Fixed."})
        written = await staff.patch(f"/api/v1/ops/health/incidents/{incident_id}", json={"writeup": "A missing brand broke two pages. Fixed and tested."})
        assert written.status_code == 200
        public = (await staff.get(f"/api/v1/public/status/incidents/{incident_id}")).json()
        assert public["writeup"].startswith("A missing brand")
    finally:
        await users.update_one({"id": staff_profile["id"]}, {"$unset": {"is_platform_staff": ""}})
