"""Integration tests for support tickets: the member side, the staff side, and
the rules that keep them apart (own tickets only, internal notes never leak,
one place for status changes, atomic claim).
"""

import asyncio
from datetime import datetime, timezone

from app.db.mongo import users
from tests.conftest import signup_new_user

TICKET = {
    "subject": "Publishing is stuck",
    "category": "Publishing",
    "severity": "P2",
    "description": "My LinkedIn post has said publishing for an hour.",
}


async def _member(make_client):
    client = make_client()
    user = await signup_new_user(client, name="Member")
    return client, user


async def _staff(make_client, role: str | None = None):
    client = make_client()
    user = await signup_new_user(client, name=f"Staff {role or 'agent'}")
    fields = {"is_platform_staff": True}
    if role:
        fields["support_role"] = role
    await users.update_one({"id": user["id"]}, {"$set": fields})
    return client, user


async def _file(client, **overrides) -> dict:
    # Tests file several tickets in one category, so skip the duplicate prompt
    # unless a test asks for it.
    res = await client.post("/api/v1/support/tickets", json={**TICKET, "allow_duplicate": True, **overrides})
    assert res.status_code == 200, res.text
    return res.json()


# ── Member side ──────────────────────────────────────────────────────────────
async def test_member_files_and_sees_own_ticket(make_client):
    member, _ = await _member(make_client)
    created = await _file(member)
    assert created["status"] == "open"
    assert isinstance(created["number"], int)

    listed = (await member.get("/api/v1/support/tickets")).json()
    assert [t["id"] for t in listed["tickets"]] == [created["id"]]

    got = await member.get(f"/api/v1/support/tickets/{created['id']}")
    assert got.status_code == 200
    assert got.json()["messages"][0]["text"] == TICKET["description"]


async def test_member_cannot_read_or_reply_to_someone_elses_ticket(make_client):
    owner, _ = await _member(make_client)
    other, _ = await _member(make_client)
    created = await _file(owner)

    # A guessed id reveals nothing: 404, never 403.
    assert (await other.get(f"/api/v1/support/tickets/{created['id']}")).status_code == 404
    reply = await other.post(f"/api/v1/support/tickets/{created['id']}/messages", json={"text": "hi"})
    assert reply.status_code == 404
    assert (await other.post(f"/api/v1/support/tickets/{created['id']}/close", json={})).status_code == 404
    assert (await other.get("/api/v1/support/tickets")).json()["tickets"] == []


async def test_member_never_receives_internal_notes(make_client):
    member, _ = await _member(make_client)
    staff, _ = await _staff(make_client)
    created = await _file(member)

    res = await staff.post(
        f"/api/v1/ops/support/tickets/{created['id']}/messages",
        json={"text": "SECRET: likely their token expired", "is_internal": True},
    )
    assert res.status_code == 200
    await staff.post(
        f"/api/v1/ops/support/tickets/{created['id']}/messages",
        json={"text": "Looking into it now."},
    )

    for path in (f"/api/v1/support/tickets/{created['id']}", "/api/v1/support/tickets"):
        body = (await member.get(path)).text
        assert "SECRET" not in body
        assert "Looking into it now." in body

    # Staff still see it.
    detail = (await staff.get(f"/api/v1/ops/support/tickets/{created['id']}")).json()
    assert any(m["is_internal"] for m in detail["ticket"]["messages"])


async def test_internal_note_does_not_notify_member_or_change_ownership(make_client):
    member, _ = await _member(make_client)
    staff, _ = await _staff(make_client)
    created = await _file(member)

    await staff.post(
        f"/api/v1/ops/support/tickets/{created['id']}/messages",
        json={"text": "note to self", "is_internal": True},
    )
    ticket = (await staff.get(f"/api/v1/ops/support/tickets/{created['id']}")).json()["ticket"]
    assert ticket["unread_for_member"] is False
    assert ticket["assignee_id"] is None
    assert ticket["status"] == "open"


async def test_non_staff_cannot_use_ops_routes(make_client):
    member, _ = await _member(make_client)
    created = await _file(member)
    assert (await member.get("/api/v1/ops/support/tickets")).status_code == 403
    assert (await member.get(f"/api/v1/ops/support/tickets/{created['id']}")).status_code == 403
    claim = await member.post(f"/api/v1/ops/support/tickets/{created['id']}/claim")
    assert claim.status_code == 403


# ── Lifecycle ────────────────────────────────────────────────────────────────
async def test_full_happy_path(make_client):
    member, _ = await _member(make_client)
    staff, staff_user = await _staff(make_client)
    created = await _file(member)
    tid = created["id"]

    claim = await staff.post(f"/api/v1/ops/support/tickets/{tid}/claim")
    assert claim.status_code == 200
    assert claim.json()["ticket"]["status"] == "investigating"
    assert claim.json()["ticket"]["assignee_id"] == staff_user["id"]

    reply = await staff.post(
        f"/api/v1/ops/support/tickets/{tid}/messages",
        json={"text": "Can you reconnect LinkedIn?", "set_status": "waiting_on_member"},
    )
    assert reply.json()["ticket"]["status"] == "waiting_on_member"
    assert reply.json()["ticket"]["unread_for_member"] is True

    seen = (await member.get(f"/api/v1/support/tickets/{tid}")).json()
    assert seen["status"] == "waiting_on_member"
    # Reading it clears the unread marker.
    assert (await member.get(f"/api/v1/support/tickets/{tid}")).json()["unread_for_member"] is False

    answer = await member.post(f"/api/v1/support/tickets/{tid}/messages", json={"text": "Done, reconnected."})
    assert answer.json()["status"] == "investigating"

    resolve = await staff.patch(f"/api/v1/ops/support/tickets/{tid}", json={"status": "resolved"})
    assert resolve.json()["ticket"]["status"] == "resolved"
    assert resolve.json()["ticket"]["resolved_at"] is not None

    closed = await member.post(f"/api/v1/support/tickets/{tid}/close", json={})
    assert closed.json()["status"] == "closed"
    assert closed.json()["closed_reason"] == "confirmed_resolved"

    events = (await staff.get(f"/api/v1/ops/support/tickets/{tid}")).json()["events"]
    types = [e["type"] for e in events]
    for expected in ("created", "claimed", "replied", "status_changed", "closed"):
        assert expected in types


async def test_invalid_transitions_are_rejected(make_client):
    member, _ = await _member(make_client)
    staff, _ = await _staff(make_client)
    tid = (await _file(member))["id"]

    # open -> waiting_on_member is not a move the table allows.
    bad = await staff.patch(f"/api/v1/ops/support/tickets/{tid}", json={"status": "waiting_on_member"})
    assert bad.status_code == 409

    # Staff can only close a resolved ticket.
    early_close = await staff.patch(f"/api/v1/ops/support/tickets/{tid}", json={"status": "closed"})
    assert early_close.status_code == 409


async def test_member_reply_on_resolved_reopens(make_client):
    member, _ = await _member(make_client)
    staff, _ = await _staff(make_client)
    tid = (await _file(member))["id"]
    await staff.patch(f"/api/v1/ops/support/tickets/{tid}", json={"status": "resolved"})

    reply = await member.post(f"/api/v1/support/tickets/{tid}/messages", json={"text": "Still broken."})
    assert reply.status_code == 200
    assert reply.json()["status"] == "investigating"
    assert reply.json()["resolved_at"] is None

    ticket = (await staff.get(f"/api/v1/ops/support/tickets/{tid}")).json()["ticket"]
    assert ticket["unread_for_ops"] is False  # opening the detail cleared it
    events = (await staff.get(f"/api/v1/ops/support/tickets/{tid}")).json()["events"]
    assert "reopened" in [e["type"] for e in events]


async def test_member_cannot_reply_to_closed_ticket(make_client):
    member, _ = await _member(make_client)
    tid = (await _file(member))["id"]
    withdrawn = await member.post(f"/api/v1/support/tickets/{tid}/close", json={"reason": "sorted it"})
    assert withdrawn.json()["status"] == "closed"
    assert withdrawn.json()["closed_reason"] == "withdrawn"

    reply = await member.post(f"/api/v1/support/tickets/{tid}/messages", json={"text": "wait"})
    assert reply.status_code == 409
    # Closed is final: no way back through reopen either.
    assert (await member.post(f"/api/v1/support/tickets/{tid}/reopen")).status_code == 409


async def test_member_can_reopen_only_resolved(make_client):
    member, _ = await _member(make_client)
    staff, _ = await _staff(make_client)
    tid = (await _file(member))["id"]
    assert (await member.post(f"/api/v1/support/tickets/{tid}/reopen")).status_code == 409
    await staff.patch(f"/api/v1/ops/support/tickets/{tid}", json={"status": "resolved"})
    reopened = await member.post(f"/api/v1/support/tickets/{tid}/reopen")
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "investigating"


# ── Claim and assignment ─────────────────────────────────────────────────────
async def test_claim_race_has_exactly_one_winner(make_client):
    member, _ = await _member(make_client)
    tid = (await _file(member))["id"]
    staff_a, _ = await _staff(make_client)
    staff_b, _ = await _staff(make_client)

    results = await asyncio.gather(
        staff_a.post(f"/api/v1/ops/support/tickets/{tid}/claim"),
        staff_b.post(f"/api/v1/ops/support/tickets/{tid}/claim"),
    )
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409]
    loser = next(r for r in results if r.status_code == 409)
    assert "already has this ticket" in loser.json()["detail"]


async def test_only_a_lead_can_assign_someone_else(make_client):
    member, _ = await _member(make_client)
    tid = (await _file(member))["id"]
    agent, agent_user = await _staff(make_client, "agent")
    lead, _ = await _staff(make_client, "lead")
    other_agent, other_user = await _staff(make_client, "agent")

    denied = await agent.patch(
        f"/api/v1/ops/support/tickets/{tid}", json={"assignee_id": other_user["id"]}
    )
    assert denied.status_code == 403

    own = await agent.patch(f"/api/v1/ops/support/tickets/{tid}", json={"assignee_id": agent_user["id"]})
    assert own.status_code == 200

    moved = await lead.patch(
        f"/api/v1/ops/support/tickets/{tid}", json={"assignee_id": other_user["id"]}
    )
    assert moved.status_code == 200
    assert moved.json()["ticket"]["assignee_id"] == other_user["id"]

    # Can only assign to people on the support team.
    outsider, outsider_user = await _member(make_client)
    bad = await lead.patch(
        f"/api/v1/ops/support/tickets/{tid}", json={"assignee_id": outsider_user["id"]}
    )
    assert bad.status_code == 400


# ── Queue ────────────────────────────────────────────────────────────────────
async def test_queue_views_and_stats(make_client):
    member, _ = await _member(make_client)
    staff, staff_user = await _staff(make_client)
    first = (await _file(member, subject="First unique issue"))["id"]
    await _file(member, subject="Second issue")
    await staff.post(f"/api/v1/ops/support/tickets/{first}/claim")

    mine = (await staff.get("/api/v1/ops/support/tickets", params={"view": "mine"})).json()
    assert [t["id"] for t in mine["tickets"]] == [first]

    unassigned = (await staff.get("/api/v1/ops/support/tickets", params={"view": "unassigned"})).json()
    assert all(t["assignee_id"] is None for t in unassigned["tickets"])
    assert first not in [t["id"] for t in unassigned["tickets"]]

    found = (await staff.get("/api/v1/ops/support/tickets", params={"q": "first unique"})).json()
    assert [t["id"] for t in found["tickets"]] == [first]
    # The queue never ships the whole thread.
    assert found["tickets"][0]["messages"] == []
    assert found["tickets"][0]["message_count"] == 1

    stats = (await staff.get("/api/v1/ops/support/stats")).json()
    assert stats["mine"] >= 1
    assert stats["unassigned"] >= 1


async def test_ticket_numbers_are_sequential_and_unique(make_client):
    member, _ = await _member(make_client)
    a = await _file(member)
    b = await _file(member)
    assert b["number"] == a["number"] + 1


# ── Phase 2: notifications and context ───────────────────────────────────────
from app.db.mongo import support_notifications, support_ticket_context, workspace_connections  # noqa: E402
from app.shared.support_context import enrich_ticket  # noqa: E402


async def test_filing_a_ticket_notifies_member_and_staff(make_client):
    member, member_user = await _member(make_client)
    staff, staff_user = await _staff(make_client)
    tid = (await _file(member))["id"]

    mine = (await member.get("/api/v1/support/notifications")).json()
    assert mine["unread"] == 1
    assert mine["notifications"][0]["type"] == "ticket_created"
    assert mine["notifications"][0]["ticket_id"] == tid

    theirs = (await staff.get("/api/v1/ops/support/notifications")).json()
    assert any(n["type"] == "new_ticket" and n["ticket_id"] == tid for n in theirs["notifications"])


async def test_staff_reply_and_resolve_notify_member_but_notes_do_not(make_client):
    member, _ = await _member(make_client)
    staff, _ = await _staff(make_client)
    tid = (await _file(member))["id"]
    await member.post("/api/v1/support/notifications/read")

    await staff.post(
        f"/api/v1/ops/support/tickets/{tid}/messages", json={"text": "private", "is_internal": True}
    )
    assert (await member.get("/api/v1/support/notifications")).json()["unread"] == 0

    await staff.post(f"/api/v1/ops/support/tickets/{tid}/messages", json={"text": "On it."})
    body = (await member.get("/api/v1/support/notifications")).json()
    assert body["unread"] == 1
    assert body["notifications"][0]["type"] == "staff_reply"

    await staff.patch(f"/api/v1/ops/support/tickets/{tid}", json={"status": "resolved"})
    types = [n["type"] for n in (await member.get("/api/v1/support/notifications")).json()["notifications"]]
    assert "ticket_resolved" in types


async def test_opening_a_ticket_clears_its_notifications(make_client):
    member, _ = await _member(make_client)
    staff, _ = await _staff(make_client)
    tid = (await _file(member))["id"]
    await staff.post(f"/api/v1/ops/support/tickets/{tid}/messages", json={"text": "Hello"})
    assert (await member.get("/api/v1/support/notifications")).json()["unread"] >= 1

    await member.get(f"/api/v1/support/tickets/{tid}")
    assert (await member.get("/api/v1/support/notifications")).json()["unread"] == 0


async def test_member_only_sees_their_own_notifications(make_client):
    a, _ = await _member(make_client)
    b, _ = await _member(make_client)
    await _file(a)
    assert (await b.get("/api/v1/support/notifications")).json()["unread"] == 0


async def test_member_assignee_notified_when_member_replies(make_client):
    member, _ = await _member(make_client)
    staff, staff_user = await _staff(make_client)
    tid = (await _file(member))["id"]
    await staff.post(f"/api/v1/ops/support/tickets/{tid}/claim")
    await staff.post("/api/v1/ops/support/notifications/read")

    await member.post(f"/api/v1/support/tickets/{tid}/messages", json={"text": "More detail."})
    n = (await staff.get("/api/v1/ops/support/notifications")).json()
    assert n["unread"] == 1
    assert n["notifications"][0]["type"] == "member_replied"


async def test_context_snapshot_is_safe_and_useful(make_client):
    member, member_user = await _member(make_client)
    staff, _ = await _staff(make_client)
    created = await _file(
        member,
        source_context={"type": "platform", "id": "linkedin", "route": "/dashboard/drafts"},
        client_env={"user_agent": "TestAgent/1.0", "viewport": "1280x720"},
    )
    tid = created["id"]
    ticket = await support_tickets_doc(tid)

    # A connection whose token has expired, with secrets that must never be copied.
    await workspace_connections.insert_one(
        {
            "id": "conn-1",
            "workspace_id": ticket["workspace_id"],
            "platform": "linkedin",
            "username": "acme",
            "is_active": True,
            "expires_at": datetime(2020, 1, 1, tzinfo=timezone.utc),
            "access_token": "SECRET-ACCESS",
            "refresh_token": "SECRET-REFRESH",
        }
    )
    context = await enrich_ticket(tid)
    assert context["enrich_status"] == "done"
    snap = context["snapshot"]
    assert snap["platforms"][0]["health"] == "expired"
    assert snap["route"] == "/dashboard/drafts"
    assert snap["env"]["viewport"] == "1280x720"
    assert "SECRET" not in str(context)

    detail = (await staff.get(f"/api/v1/ops/support/tickets/{tid}")).json()
    assert detail["context"]["enrich_status"] == "done"
    # Members never receive the snapshot.
    assert "snapshot" not in (await member.get(f"/api/v1/support/tickets/{tid}")).text


async def test_context_failure_leaves_ticket_usable_and_retry_works(make_client, monkeypatch):
    member, _ = await _member(make_client)
    staff, _ = await _staff(make_client)
    tid = (await _file(member))["id"]

    import app.shared.support_context as ctx_mod

    async def boom(_ticket):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(ctx_mod, "build_snapshot", boom)
    monkeypatch.setattr(ctx_mod, "_BACKOFF_SECONDS", (0.0, 0.0))
    failed = await enrich_ticket(tid)
    assert failed["enrich_status"] == "failed"

    # The ticket still works.
    assert (await staff.post(f"/api/v1/ops/support/tickets/{tid}/messages", json={"text": "Hi"})).status_code == 200

    monkeypatch.undo()
    retried = await staff.post(f"/api/v1/ops/support/tickets/{tid}/enrich")
    assert retried.status_code == 200
    assert retried.json()["context"]["enrich_status"] == "done"


async def test_non_staff_cannot_retry_context(make_client):
    member, _ = await _member(make_client)
    tid = (await _file(member))["id"]
    assert (await member.post(f"/api/v1/ops/support/tickets/{tid}/enrich")).status_code == 403


async def support_tickets_doc(ticket_id: str) -> dict:
    from app.db.mongo import support_tickets

    return await support_tickets.find_one({"id": ticket_id})
