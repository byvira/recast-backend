"""Support data retention and deletion, and the per-request log line."""

import logging
from datetime import datetime, timedelta, timezone

import pytest

from app.db.mongo import (
    support_chats,
    support_files,
    support_notifications,
    support_ticket_context,
    support_ticket_events,
    support_tickets,
)
from app.shared import storage
from app.shared import support_privacy
from app.shared import support_rules as rules
from app.shared.support_context import enrich_ticket
from app.workers.support_lifecycle import run_support_lifecycle
from tests.test_support_tickets import _file, _member, _staff, support_tickets_doc

OPS = "/api/v1/ops/support"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture(autouse=True)
def _no_background_work(monkeypatch):
    import app.api.v1.support as member_routes

    monkeypatch.setattr(member_routes, "spawn", lambda coro: coro.close())


@pytest.fixture
def fake_storage(monkeypatch):
    """No real Cloudinary: uploads return a key, deletes are recorded."""
    deleted: list[str] = []
    monkeypatch.setattr(storage, "upload_private_file", lambda data, folder, pid: f"k/{folder}/{pid}")
    monkeypatch.setattr(storage, "signed_private_url", lambda key, secs=300: f"https://signed.example/{key}")
    monkeypatch.setattr(storage, "delete_private_file", lambda key: deleted.append(key) or True)
    return deleted


async def _closed_ticket_with_everything(member, staff, deleted_files_ok=True):
    """A ticket with an attachment, a context snapshot, a notification, a rating
    and a member note on its close: everything erasure has to reach."""
    up = await member.post("/api/v1/support/uploads", files={"file": ("a.png", PNG, "image/png")})
    file_id = up.json()["file_id"]
    created = await _file(
        member, subject="My private subject", description="Call me on 0123 secret", attachment_ids=[file_id],
        source_context={"type": "platform", "id": "linkedin", "route": "/dashboard/drafts?piece=123"},
        client_env={"user_agent": "Agent/1.0", "viewport": "1x1"},
    )
    tid = created["id"]
    await enrich_ticket(tid)
    await staff.post(f"{OPS}/tickets/{tid}/messages", json={"text": "Internal: member seems upset", "is_internal": True})
    await staff.post(f"{OPS}/tickets/{tid}/messages", json={"text": "We are on it", "set_status": "resolved"})
    await member.post(f"/api/v1/support/tickets/{tid}/rating", json={"value": "up", "comment": "Thanks, Jane at acme"})
    await member.post(f"/api/v1/support/tickets/{tid}/close", json={"reason": "all good, my email is jane@acme.com"})
    return tid, file_id


def _assert_erased(t: dict):
    assert t["subject"] == "Removed" and t["created_by_name"] == "Deleted user"
    assert t.get("created_by_email") is None and t.get("client_env") is None and t["tags"] == []
    assert all(m["text"] == support_privacy.REMOVED_TEXT and m["attachments"] == [] for m in t["messages"])
    assert not any("secret" in m["text"] or "upset" in m["text"] for m in t["messages"])
    assert (t.get("rating") or {}).get("comment") is None
    assert (t.get("source_context") or {}).get("route") is None


# ── Retention ────────────────────────────────────────────────────────────────
async def test_retention_cleans_old_closed_tickets_and_keeps_the_counts(make_client, fake_storage):
    member, _ = await _member(make_client)
    staff, _ = await _staff(make_client)
    tid, file_id = await _closed_ticket_with_everything(member, staff)
    fresh = (await _file(member, subject="Recent one", category="Billing"))["id"]
    await member.post(f"/api/v1/support/tickets/{fresh}/close", json={})

    before = await support_tickets_doc(tid)
    now = datetime.now(timezone.utc)
    aged_closed_at = (now - rules.TICKET_RETENTION - timedelta(days=1)).replace(microsecond=0)
    await support_tickets.update_one({"id": tid}, {"$set": {"closed_at": aged_closed_at}})
    assert await support_ticket_context.count_documents({"ticket_id": tid}) == 1

    cleaned = await support_privacy.enforce_retention(now)
    assert cleaned >= 1

    t = await support_tickets_doc(tid)
    _assert_erased(t)
    # The numbers that keep the metrics true are untouched.
    for keep in ("category", "severity", "status", "number", "created_at", "resolved_at", "workspace_tier"):
        assert t[keep] == before[keep], keep
    # (closed_at was aged on purpose above, and cleaning must leave it as it was.)
    assert t["closed_at"] == aged_closed_at.replace(tzinfo=None)
    assert t["sla"]["first_response_due"] == before["sla"]["first_response_due"]
    assert (t["rating"] or {}).get("value") == "up"
    assert len(t["messages"]) == len(before["messages"])
    # The file is gone from the database and from storage; context and notifications too.
    assert await support_files.count_documents({"id": file_id}) == 0
    assert any(file_id in key for key in fake_storage)
    assert await support_ticket_context.count_documents({"ticket_id": tid}) == 0
    assert await support_notifications.count_documents({"ticket_id": tid}) == 0
    events = await support_ticket_events.find({"ticket_id": tid}).to_list(50)
    assert all(e["actor_name"] == "Deleted user" for e in events if e["actor_type"] == "member")
    assert not any("jane@acme.com" in str(e) for e in events)
    assert "retention_cleaned" in [e["type"] for e in events]

    # A recently closed ticket keeps its words; running again cleans nothing more.
    assert (await support_tickets_doc(fresh))["subject"] == "Recent one"
    assert await support_privacy.enforce_retention(now) == 0
    # The member's list shows a cleaned ticket without any of its words.
    seen = (await member.get("/api/v1/support/tickets", params={"view": "resolved"})).text
    assert "My private subject" not in seen and "secret" not in seen


async def test_retention_runs_from_the_housekeeping_job_and_never_touches_open_tickets(make_client, fake_storage, monkeypatch):
    import app.workers.support_lifecycle as lifecycle

    calls: list[datetime] = []

    async def fake_retention(now):
        calls.append(now)
        return 7

    monkeypatch.setattr(lifecycle, "enforce_retention", fake_retention)
    when = datetime.now(timezone.utc)
    done = await run_support_lifecycle(when)
    assert done["retention"] == 7 and calls == [when]

    # And the real rule only ever looks at closed tickets: an old open one is left alone.
    member, _ = await _member(make_client)
    open_id = (await _file(member, subject="Still open"))["id"]
    await support_tickets.update_one({"id": open_id}, {"$set": {"created_at": datetime(2020, 1, 1, tzinfo=timezone.utc)}})
    await support_privacy.enforce_retention(when + rules.TICKET_RETENTION + timedelta(days=30))
    assert (await support_tickets_doc(open_id))["subject"] == "Still open"


# ── Member deletion request ──────────────────────────────────────────────────
async def test_member_can_erase_their_support_data(make_client, fake_storage):
    member, member_user = await _member(make_client)
    other, _ = await _member(make_client)
    staff, _ = await _staff(make_client)
    closed_id, file_id = await _closed_ticket_with_everything(member, staff)
    open_id = (await _file(member, subject="Open one", category="Billing", description="Secret billing thing"))["id"]
    await support_chats.insert_one(
        {"id": "chat-1", "user_id": member_user["id"], "workspace_id": "w", "title": "t", "messages": [], "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc)}
    )
    other_id = (await _file(other, subject="Not mine"))["id"]

    # A deliberate second step is required.
    assert (await member.post("/api/v1/support/my-data/erase", json={"confirm": False})).status_code == 400
    assert (await member.post("/api/v1/support/my-data/erase", json={})).status_code == 422

    res = await member.post("/api/v1/support/my-data/erase", json={"confirm": True})
    assert res.status_code == 200, res.text
    assert res.json()["tickets"] == 2 and res.json()["chats"] == 1

    for tid in (closed_id, open_id):
        t = await support_tickets_doc(tid)
        _assert_erased(t)
        assert t["status"] == "closed"  # the open one was closed first
    assert (await support_tickets_doc(open_id))["closed_reason"] == "data_erased"
    assert await support_chats.count_documents({"user_id": member_user["id"]}) == 0
    assert await support_notifications.count_documents({"user_id": member_user["id"]}) == 0
    assert await support_files.count_documents({"uploaded_by": member_user["id"]}) == 0

    # Someone else's data is untouched.
    assert (await support_tickets_doc(other_id))["subject"] == "Not mine"
    # Staff can still see the counts, with no words.
    detail = (await staff.get(f"{OPS}/tickets/{closed_id}")).text
    assert "My private subject" not in detail and "upset" not in detail and "jane@acme.com" not in detail


async def test_only_an_admin_can_erase_a_member_on_request(make_client, fake_storage):
    member, _ = await _member(make_client)
    lead, _ = await _staff(make_client, "lead")
    admin, _ = await _staff(make_client, "admin")
    tid = (await _file(member, subject="Please delete me"))["id"]
    body = {"confirm": True, "reason": "Requested by email on 3 March"}

    assert (await lead.post(f"{OPS}/tickets/{tid}/erase-member", json=body)).status_code == 403
    assert (await admin.post(f"{OPS}/tickets/{tid}/erase-member", json={"confirm": False, "reason": "Requested by email"})).status_code == 400
    assert (await admin.post(f"{OPS}/tickets/{tid}/erase-member", json={"confirm": True, "reason": "short"})).status_code == 422

    res = await admin.post(f"{OPS}/tickets/{tid}/erase-member", json=body)
    assert res.status_code == 200 and res.json()["tickets"] == 1
    _assert_erased(await support_tickets_doc(tid))
    events = (await admin.get(f"{OPS}/tickets/{tid}")).json()["events"]
    recorded = [e for e in events if e["type"] == "member_data_erased_by_staff"]
    assert recorded and recorded[0]["data"]["reason"] == "Requested by email on 3 March"


# ── Per-request log line ─────────────────────────────────────────────────────
class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


async def test_every_support_request_logs_ticket_actor_route_and_latency_but_no_words(make_client):
    cap = _Capture()
    log = logging.getLogger("app.support")
    old_level = log.level
    log.addHandler(cap)
    log.setLevel(logging.INFO)
    try:
        member, member_user = await _member(make_client)
        staff, _ = await _staff(make_client)
        tid = (await _file(member, description="my very private words"))["id"]
        await staff.post(f"{OPS}/tickets/{tid}/messages", json={"text": "a reply with secret content"})
        await member.get(f"/api/v1/support/tickets/{tid}")
        await staff.get(f"{OPS}/tickets", params={"q": "jane@acme.com"})
        missing = await member.get("/api/v1/support/tickets/not-a-real-id")
        assert missing.status_code == 404
    finally:
        log.removeHandler(cap)
        log.setLevel(old_level)

    text = "\n".join(cap.lines)
    assert f"ticket={tid}" in text and f"actor={member_user['id']}" in text
    assert "/tickets/{ticket_id}/messages" in text  # the route template, not the raw path with a real id
    assert "method=POST" in text and "latency_ms=" in text and "outcome=ok" in text
    # Failed requests are logged with their status; the search term and message words never are.
    assert "outcome=error:404" in text
    for leaked in ("very private words", "secret content", "jane@acme.com"):
        assert leaked not in text


async def test_support_search_terms_are_kept_out_of_the_request_log(make_client):
    cap = _Capture()
    log = logging.getLogger("app")  # the request middleware logs on the app logger
    old_level = log.level
    log.addHandler(cap)
    log.setLevel(logging.INFO)
    try:
        staff, _ = await _staff(make_client)
        await staff.get(f"{OPS}/tickets", params={"q": "someone.private@example.com"})
    finally:
        log.removeHandler(cap)
        log.setLevel(old_level)
    text = "\n".join(cap.lines)
    assert "/ops/support/tickets" in text  # the request itself is still logged
    assert "someone.private" not in text


async def test_privacy_guide_is_listed(make_client):
    member, _ = await _member(make_client)
    guides = (await member.get("/api/v1/support/guides")).json()["guides"]
    assert any(g["id"] == "support-data-privacy" and "24 months" in g["body"] for g in guides)
