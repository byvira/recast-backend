"""Support tickets, Phase 4: canned replies, presence, bulk actions, merge,
removing a message, the newer-reply guard and SLA targets."""

from datetime import datetime, timedelta, timezone

import pytest

from app.db.mongo import support_files, support_tickets
from app.shared import storage
from app.shared import support_rules as rules
from app.workers.support_lifecycle import run_support_lifecycle
from tests.test_support_tickets import TICKET, _file, _member, _staff, support_tickets_doc

OPS = "/api/v1/ops/support"


# ── Canned replies ───────────────────────────────────────────────────────────
async def test_canned_replies_private_shared_and_rendered(make_client):
    member, _ = await _member(make_client)
    agent, _ = await _staff(make_client, "agent")
    other_agent, _ = await _staff(make_client, "agent")
    lead, _ = await _staff(make_client, "lead")
    tid = (await _file(member, subject="Login problem"))["id"]

    mine = await agent.post(f"{OPS}/canned", json={"title": "Ask account", "body": "Hi {name}, which account in {workspace}? ({ticket_number})"})
    assert mine.status_code == 200 and mine.json()["shared"] is False
    reply_id = mine.json()["id"]

    # An agent cannot share with the whole team, a lead can.
    assert (await agent.post(f"{OPS}/canned", json={"title": "x", "body": "y", "shared": True})).status_code == 403
    shared = await lead.post(f"{OPS}/canned", json={"title": "Thanks", "body": "Thanks {name}", "shared": True})
    assert shared.status_code == 200 and shared.json()["shared"] is True

    # Someone else's private reply is invisible; the shared one is visible to all.
    titles = [r["title"] for r in (await other_agent.get(f"{OPS}/canned")).json()["replies"]]
    assert "Thanks" in titles and "Ask account" not in titles
    assert (await other_agent.put(f"{OPS}/canned/{reply_id}", json={"title": "hijack", "body": "x"})).status_code == 404
    assert (await other_agent.delete(f"{OPS}/canned/{reply_id}")).status_code == 404
    # An agent cannot edit a shared reply; a lead can edit anyone's.
    shared_id = shared.json()["id"]
    assert (await other_agent.put(f"{OPS}/canned/{shared_id}", json={"title": "t", "body": "b"})).status_code == 403
    assert (await lead.put(f"{OPS}/canned/{reply_id}", json={"title": "Ask account 2", "body": "Hello {name}"})).status_code == 200

    rendered = await agent.get(f"{OPS}/canned/{shared_id}/render/{tid}")
    assert rendered.json()["text"] == "Thanks Member"
    assert (await agent.get(f"{OPS}/canned/{reply_id}/render/{tid}")).status_code == 200
    assert (await lead.delete(f"{OPS}/canned/{reply_id}")).json() == {"deleted": True}


# ── Presence ─────────────────────────────────────────────────────────────────
async def test_presence_shows_other_viewers_only(make_client):
    member, _ = await _member(make_client)
    a, a_user = await _staff(make_client)
    b, b_user = await _staff(make_client)
    tid = (await _file(member))["id"]

    assert (await a.post(f"{OPS}/presence/{tid}")).json()["viewers"] == []
    seen = (await b.post(f"{OPS}/presence/{tid}")).json()["viewers"]
    assert [v["id"] for v in seen] == [a_user["id"]]
    assert (await member.post(f"{OPS}/presence/{tid}")).status_code == 403


async def test_reply_is_held_back_when_a_newer_message_arrived(make_client):
    member, _ = await _member(make_client)
    staff, _ = await _staff(make_client)
    tid = (await _file(member))["id"]

    await member.post(f"/api/v1/support/tickets/{tid}/messages", json={"text": "one more thing"})
    stale = await staff.post(
        f"{OPS}/tickets/{tid}/messages", json={"text": "Reply", "known_message_total": 1}
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "newer_reply"
    forced = await staff.post(
        f"{OPS}/tickets/{tid}/messages", json={"text": "Reply", "known_message_total": 1, "force": True}
    )
    assert forced.status_code == 200
    fresh = await staff.post(
        f"{OPS}/tickets/{tid}/messages", json={"text": "Another", "known_message_total": 3}
    )
    assert fresh.status_code == 200


# ── Bulk ─────────────────────────────────────────────────────────────────────
async def test_bulk_actions_need_a_lead_and_report_partial_failure(make_client):
    member, _ = await _member(make_client)
    agent, _ = await _staff(make_client, "agent")
    lead, lead_user = await _staff(make_client, "lead")
    a = (await _file(member, subject="A"))["id"]
    b = (await _file(member, subject="B"))["id"]
    await lead.patch(f"{OPS}/tickets/{b}", json={"status": "resolved"})
    await lead.patch(f"{OPS}/tickets/{b}", json={"status": "closed"})

    denied = await agent.post(f"{OPS}/tickets/bulk", json={"ids": [a], "action": "add_tag", "payload": {"tag": "x"}})
    assert denied.status_code == 403

    res = await lead.post(f"{OPS}/tickets/bulk", json={"ids": [a, b], "action": "assign", "payload": {"assignee_id": lead_user["id"]}})
    assert res.status_code == 200
    body = res.json()
    assert body["done"] == [a]
    assert body["failed"][0]["id"] == b  # a closed ticket cannot be assigned
    tag = await lead.post(f"{OPS}/tickets/bulk", json={"ids": [a], "action": "add_tag", "payload": {"tag": "Billing"}})
    assert tag.json()["done"] == [a]
    assert "billing" in (await support_tickets_doc(a))["tags"]

    # An untouched ticket cannot jump to waiting on engineering; it is reported, not skipped.
    early = await lead.post(f"{OPS}/tickets/bulk", json={"ids": [a], "action": "set_status", "payload": {"status": "waiting_on_engineering"}})
    assert early.json()["done"] == [] and early.json()["failed"][0]["id"] == a
    started = await lead.post(f"{OPS}/tickets/bulk", json={"ids": [a], "action": "set_status", "payload": {"status": "investigating"}})
    assert started.json()["done"] == [a]
    status = await lead.post(f"{OPS}/tickets/bulk", json={"ids": [a], "action": "set_status", "payload": {"status": "waiting_on_engineering"}})
    assert status.json()["done"] == [a]
    bad = await lead.post(f"{OPS}/tickets/bulk", json={"ids": [a], "action": "set_status", "payload": {"status": "open"}})
    assert bad.json()["failed"][0]["id"] == a


async def test_bulk_canned_reply_reaches_each_member(make_client):
    m1, _ = await _member(make_client)
    m2, _ = await _member(make_client)
    lead, _ = await _staff(make_client, "lead")
    t1 = (await _file(m1))["id"]
    t2 = (await _file(m2))["id"]
    canned = (await lead.post(f"{OPS}/canned", json={"title": "Known", "body": "Hi {name}, we know about this.", "shared": True})).json()

    res = await lead.post(
        f"{OPS}/tickets/bulk",
        json={"ids": [t1, t2], "action": "canned_reply", "payload": {"canned_id": canned["id"], "set_status": "waiting_on_engineering"}},
    )
    assert sorted(res.json()["done"]) == sorted([t1, t2])
    seen = (await m1.get(f"/api/v1/support/tickets/{t1}")).json()
    assert seen["status"] == "waiting_on_engineering"
    assert seen["messages"][-1]["text"] == "Hi Member, we know about this."


# ── Merge ────────────────────────────────────────────────────────────────────
async def test_merge_moves_messages_and_files_and_tells_the_member(make_client, monkeypatch):
    monkeypatch.setattr(storage, "upload_private_file", lambda data, folder, pid: f"k/{folder}/{pid}")
    monkeypatch.setattr(storage, "signed_private_url", lambda key, secs=300: f"https://signed.example/{key}")
    member, _ = await _member(make_client)
    lead, _ = await _staff(make_client, "lead")
    agent, _ = await _staff(make_client, "agent")

    up = await member.post(
        "/api/v1/support/uploads", files={"file": ("a.png", b"\x89PNG\r\n\x1a\n" + b"\0" * 32, "image/png")}
    )
    file_id = up.json()["file_id"]
    dup = (await _file(member, subject="Same issue again", category="Billing", attachment_ids=[file_id]))["id"]
    keep = (await _file(member, subject="Original"))["id"]
    await member.post("/api/v1/support/notifications/read")

    assert (await agent.post(f"{OPS}/tickets/{dup}/merge", json={"target_ticket_id": keep})).status_code == 403
    assert (await lead.post(f"{OPS}/tickets/{dup}/merge", json={"target_ticket_id": dup})).status_code == 400

    merged = await lead.post(f"{OPS}/tickets/{dup}/merge", json={"target_ticket_id": keep})
    assert merged.status_code == 200, merged.text
    texts = [m["text"] for m in merged.json()["ticket"]["messages"]]
    assert any("Merged from" in t for t in texts) and TICKET["description"] in texts

    source = await support_tickets_doc(dup)
    assert source["status"] == "closed" and source["closed_reason"] == "merged" and source["merged_into"] == keep
    # The file now lives on the kept ticket, so the member can still open it there.
    assert (await support_files.find_one({"id": file_id}))["ticket_id"] == keep
    assert (await member.get(f"/api/v1/support/attachments/{file_id}")).status_code == 200
    # The note that explains the merge is internal.
    assert "Merged from" not in (await member.get(f"/api/v1/support/tickets/{keep}")).text
    notes = (await member.get("/api/v1/support/notifications")).json()["notifications"]
    assert [n["type"] for n in notes].count("ticket_merged") == 1
    # A merged ticket cannot be merged again.
    assert (await lead.post(f"{OPS}/tickets/{dup}/merge", json={"target_ticket_id": keep})).status_code == 409


async def test_merge_refuses_tickets_from_different_people(make_client):
    a, _ = await _member(make_client)
    b, _ = await _member(make_client)
    lead, _ = await _staff(make_client, "lead")
    ta = (await _file(a))["id"]
    tb = (await _file(b))["id"]
    res = await lead.post(f"{OPS}/tickets/{ta}/merge", json={"target_ticket_id": tb})
    assert res.status_code == 400


# ── Remove a message ─────────────────────────────────────────────────────────
async def test_only_an_admin_can_remove_a_message_and_it_leaves_a_marker(make_client):
    member, _ = await _member(make_client)
    lead, _ = await _staff(make_client, "lead")
    admin, _ = await _staff(make_client, "admin")
    tid = (await _file(member, description="my password is hunter2"))["id"]

    assert (await lead.delete(f"{OPS}/tickets/{tid}/messages/0")).status_code == 403
    assert (await admin.delete(f"{OPS}/tickets/{tid}/messages/9")).status_code == 404
    res = await admin.delete(f"{OPS}/tickets/{tid}/messages/0")
    assert res.status_code == 200
    assert res.json()["ticket"]["messages"][0]["is_deleted"] is True

    seen = (await member.get(f"/api/v1/support/tickets/{tid}")).text
    assert "hunter2" not in seen and "This message was removed." in seen
    detail = (await admin.get(f"{OPS}/tickets/{tid}")).json()
    assert "hunter2" not in str(detail)  # not in the messages, not in the audit trail
    assert "message_deleted" in [e["type"] for e in detail["events"]]
    assert (await admin.delete(f"{OPS}/tickets/{tid}/messages/0")).status_code == 404  # already removed


# ── SLA ──────────────────────────────────────────────────────────────────────
async def test_sla_targets_follow_priority_and_are_recomputed(make_client):
    member, _ = await _member(make_client)
    staff, _ = await _staff(make_client)
    tid = (await _file(member, severity="P2"))["id"]
    t = await support_tickets_doc(tid)
    created = t["created_at"].replace(tzinfo=timezone.utc)
    sla = t["sla"]
    assert sla["first_response_due"].replace(tzinfo=timezone.utc) - created == timedelta(hours=4)
    assert sla["resolve_due"].replace(tzinfo=timezone.utc) - created == timedelta(hours=72)

    await staff.patch(f"{OPS}/tickets/{tid}", json={"severity": "P1"})
    sla = (await support_tickets_doc(tid))["sla"]
    assert sla["first_response_due"].replace(tzinfo=timezone.utc) - created == timedelta(hours=1)

    # The first public reply stops the first-response clock; a note does not.
    await staff.post(f"{OPS}/tickets/{tid}/messages", json={"text": "note", "is_internal": True})
    assert (await support_tickets_doc(tid))["sla"]["first_responded_at"] is None
    await staff.post(f"{OPS}/tickets/{tid}/messages", json={"text": "Hello"})
    assert (await support_tickets_doc(tid))["sla"]["first_responded_at"] is not None


async def test_paid_plan_gets_a_faster_target(make_client, monkeypatch):
    monkeypatch.setattr(rules, "PAID_TIERS", frozenset({"single"}))  # personal workspaces are "single"
    member, _ = await _member(make_client)
    tid = (await _file(member, severity="P3"))["id"]
    t = await support_tickets_doc(tid)
    created = t["created_at"].replace(tzinfo=timezone.utc)
    # P3 on a paid plan is treated as P2 (4 hours), not 24.
    assert t["sla"]["first_response_due"].replace(tzinfo=timezone.utc) - created == timedelta(hours=4)


async def test_overdue_view_stats_and_breach_alerts(make_client):
    member, _ = await _member(make_client)
    lead, _ = await _staff(make_client, "lead")
    tid = (await _file(member, severity="P2"))["id"]
    base = datetime.now(timezone.utc)

    assert tid not in [t["id"] for t in (await lead.get(f"{OPS}/tickets", params={"view": "overdue"})).json()["tickets"]]
    await lead.post(f"{OPS}/notifications/read")

    # Two hours in, nobody has taken it: a digest goes to the leads, once.
    # (Other tickets left by earlier tests may be in the digest too, so check this one.)
    first = await run_support_lifecycle(base + timedelta(hours=2, minutes=1))
    assert first["sla_digest"] >= 1 and first["sla_breached"] == 0
    assert (await support_tickets_doc(tid))["sla"]["digest_sent"] is True
    assert (await run_support_lifecycle(base + timedelta(hours=2, minutes=30)))["sla_digest"] == 0

    # Past four hours with no reply: breached, flagged once, alert to the leads.
    late = await run_support_lifecycle(base + timedelta(hours=5))
    assert late["sla_breached"] >= 1
    assert (await support_tickets_doc(tid))["sla"]["breached_first"] is True
    assert (await run_support_lifecycle(base + timedelta(hours=6)))["sla_breached"] == 0
    types = [n["type"] for n in (await lead.get(f"{OPS}/notifications")).json()["notifications"]]
    assert "sla_digest" in types and "sla_breached" in types
    events = [e["type"] for e in (await lead.get(f"{OPS}/tickets/{tid}")).json()["events"]]
    assert "sla_breached" in events

    # The overdue view uses the real clock, so age the ticket for it.
    await support_tickets.update_one(
        {"id": tid}, {"$set": {"sla.first_response_due": base - timedelta(minutes=5)}}
    )
    overdue = [t["id"] for t in (await lead.get(f"{OPS}/tickets", params={"view": "overdue"})).json()["tickets"]]
    assert tid in overdue
    assert (await lead.get(f"{OPS}/stats")).json()["overdue"] >= 1
    await lead.post(f"{OPS}/tickets/{tid}/messages", json={"text": "Sorry for the wait"})
    still = [t["id"] for t in (await lead.get(f"{OPS}/tickets", params={"view": "overdue"})).json()["tickets"]]
    assert tid not in still  # answered in the end, and the fix is not due for days


async def test_only_an_admin_can_change_sla_targets(make_client):
    lead, _ = await _staff(make_client, "lead")
    admin, _ = await _staff(make_client, "admin")
    body = {
        "first_response_hours": {"P1": 2, "P2": 6, "P3": 30},
        "resolution_hours": {"P1": 24, "P2": 72, "P3": 168},
    }
    assert (await lead.put(f"{OPS}/sla", json=body)).status_code == 403
    assert (await lead.get(f"{OPS}/sla")).json()["can_edit"] is False
    assert (await admin.put(f"{OPS}/sla", json={**body, "first_response_hours": {"P1": 0, "P2": 6, "P3": 30}})).status_code == 400
    assert (await admin.put(f"{OPS}/sla", json=body)).status_code == 200

    member, _ = await _member(make_client)
    tid = (await _file(member, severity="P1"))["id"]
    t = await support_tickets_doc(tid)
    created = t["created_at"].replace(tzinfo=timezone.utc)
    assert t["sla"]["first_response_due"].replace(tzinfo=timezone.utc) - created == timedelta(hours=2)


@pytest.fixture(autouse=True)
async def _reset_sla_settings():
    """SLA overrides are stored globally; leave the defaults for the next test."""
    yield
    from app.db.mongo import support_settings

    await support_settings.delete_many({"_id": "sla"})
