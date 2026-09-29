"""Support tickets, Phase 5: triage, incidents, escalation, view-as-member,
muting, area routing and the edge cases around people who have gone."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.db.mongo import activity_entries, support_incidents, support_tickets, users
from app.shared import support_rules as rules
from app.shared.support_triage import suggest_category, triage_ticket
from app.workers.support_lifecycle import run_support_lifecycle
from tests.test_support_tickets import _file, _member, _staff, support_tickets_doc

OPS = "/api/v1/ops/support"


@pytest.fixture(autouse=True)
def _no_background_triage(monkeypatch):
    """Filing a ticket normally starts triage and the context snapshot in the
    background. Tests run triage by hand so the result is deterministic."""
    import app.api.v1.support as member_routes

    monkeypatch.setattr(member_routes, "spawn", lambda coro: coro.close())


@pytest.fixture(autouse=True)
async def _reset_routing():
    yield
    from app.db.mongo import support_settings

    await support_settings.delete_many({"_id": "routing"})


# ── Triage rules ─────────────────────────────────────────────────────────────
def test_category_suggestions_come_from_keywords():
    assert suggest_category("Invoice is wrong", "I was charged twice") == "Billing"
    assert suggest_category("Can't reconnect", "the token expired") == "Connecting accounts"
    assert suggest_category("Post stuck", "it never published") == "Publishing"
    assert suggest_category("Tone is off", "my brand voice sounds wrong") == "Brand voice"
    assert suggest_category("Hello", "just saying hi") is None


async def test_triage_tags_repeat_and_suggests_a_category(make_client):
    member, _ = await _member(make_client)
    await _file(member, subject="Post stuck", description="it never published")
    second = await _file(member, subject="Invoice wrong", description="I was charged twice", category="Question")

    assert (await triage_ticket(second["id"]))["repeat"] is True
    ticket = await support_tickets_doc(second["id"])
    assert "repeat" in ticket["tags"] and ticket["suggested_category"] == "Billing"


async def test_failure_burst_and_paid_plan_raise_priority(make_client, monkeypatch):
    member, _ = await _member(make_client)
    tid = (await _file(member, severity="P3"))["id"]
    workspace_id = (await support_tickets_doc(tid))["workspace_id"]
    now = datetime.now(timezone.utc)
    for i in range(3):
        await activity_entries.insert_one(
            {"_id": f"t:{uuid4()}", "workspace_id": workspace_id, "status": "failed", "title": f"Publish {i} failed",
             "occurred_at": now - timedelta(minutes=5)}
        )
    result = await triage_ticket(tid)
    assert result["priority"] == "P2"
    ticket = await support_tickets_doc(tid)
    assert ticket["severity"] == "P2"
    created = ticket["created_at"].replace(tzinfo=timezone.utc)
    # The response target follows the new priority.
    assert ticket["sla"]["first_response_due"].replace(tzinfo=timezone.utc) - created == timedelta(hours=4)

    # Paid plans (none exist yet) would add one more level.
    monkeypatch.setattr(rules, "PAID_TIERS", frozenset({"single"}))
    other = (await _file(member, severity="P3", subject="Another", category="Billing"))["id"]
    # Paid plan plus the failure burst: two levels, P3 to P1.
    assert (await triage_ticket(other))["priority"] == "P1"


async def test_several_tickets_about_the_same_thing_become_one_incident(make_client):
    lead, _ = await _staff(make_client, "lead")
    platform = f"plat{uuid4().hex[:8]}"  # unique, so tickets from other tests never join
    source = {"type": "platform", "id": platform}
    ids = []
    results = []
    # Triage runs as each ticket arrives; two are not enough, the third tips it.
    for _n in range(3):
        m, _u = await _member(make_client)
        ids.append((await _file(m, category="Publishing", source_context=source))["id"])
        results.append(await triage_ticket(ids[-1]))
    assert "incident_id" not in results[0] and "incident_id" not in results[1]
    result = results[2]
    assert "incident_id" in result
    incident = await support_incidents.find_one({"id": result["incident_id"]})
    assert sorted(incident["ticket_ids"]) == sorted(ids)
    assert incident["platform"] == platform and incident["status"] == "open"
    for tid in ids:
        assert (await support_tickets_doc(tid))["incident_id"] == incident["id"]

    # A fourth joins the same incident instead of opening another.
    m4, _ = await _member(make_client)
    fourth = (await _file(m4, category="Publishing", source_context=source))["id"]
    assert (await triage_ticket(fourth))["incident_id"] == incident["id"]
    assert fourth in (await support_incidents.find_one({"id": incident["id"]}))["ticket_ids"]

    listing = (await lead.get(f"{OPS}/incidents")).json()["incidents"]
    assert incident["id"] in [i["id"] for i in listing]
    detail = (await lead.get(f"{OPS}/incidents/{incident['id']}")).json()["incident"]
    assert len(detail["tickets"]) == 4 and detail["open_ticket_count"] == 4
    filtered = (await lead.get(f"{OPS}/tickets", params={"incident_id": incident["id"]})).json()
    assert filtered["total"] == 4


async def test_area_owner_gets_new_tickets(make_client):
    admin, admin_user = await _staff(make_client, "admin")
    agent, agent_user = await _staff(make_client, "agent")
    member, _ = await _member(make_client)

    assert (await agent.put(f"{OPS}/routing", json={"owners": {"Publishing": agent_user["id"]}})).status_code == 403
    outsider, outsider_user = await _member(make_client)
    bad = await admin.put(f"{OPS}/routing", json={"owners": {"Publishing": outsider_user["id"]}})
    assert bad.status_code == 400
    ok = await admin.put(f"{OPS}/routing", json={"owners": {"Publishing": agent_user["id"]}})
    assert ok.status_code == 200 and ok.json()["owners"] == {"Publishing": agent_user["id"]}

    tid = (await _file(member, category="Publishing"))["id"]
    assert (await triage_ticket(tid))["assigned_to"] == agent_user["id"]
    assert (await support_tickets_doc(tid))["assignee_id"] == agent_user["id"]
    other = (await _file(member, category="Billing", subject="x"))["id"]
    assert "assigned_to" not in await triage_ticket(other)
    await admin.put(f"{OPS}/routing", json={"owners": {}})


# ── Incidents by hand ────────────────────────────────────────────────────────
async def test_lead_builds_an_incident_and_replies_to_everyone(make_client):
    agent, _ = await _staff(make_client, "agent")
    lead, _ = await _staff(make_client, "lead")
    m1, _ = await _member(make_client)
    m2, _ = await _member(make_client)
    t1 = (await _file(m1))["id"]
    t2 = (await _file(m2))["id"]

    denied = await agent.post(f"{OPS}/incidents", json={"title": "x", "category": "Publishing", "ticket_ids": [t1]})
    assert denied.status_code == 403

    made = await lead.post(
        f"{OPS}/incidents", json={"title": "LinkedIn posts failing", "category": "Publishing", "platform": "linkedin", "ticket_ids": [t1, t2]}
    )
    assert made.status_code == 200, made.text
    incident_id = made.json()["incident"]["id"]
    assert made.json()["incident"]["ticket_count"] == 2
    # Anyone on the team can read it.
    assert (await agent.get(f"{OPS}/incidents/{incident_id}")).status_code == 200

    reply = await lead.post(
        f"{OPS}/incidents/{incident_id}/reply", json={"text": "We know, fixing it now.", "set_status": "waiting_on_engineering"}
    )
    assert sorted(reply.json()["done"]) == sorted([t1, t2])
    seen = (await m1.get(f"/api/v1/support/tickets/{t1}")).json()
    assert seen["messages"][-1]["text"] == "We know, fixing it now."
    assert seen["status"] == "waiting_on_engineering"

    # Fixed: tickets waiting on engineering are told and moved on as chosen.
    fixed = await lead.patch(
        f"{OPS}/incidents/{incident_id}",
        json={"status": "resolved", "after_fix_status": "resolved", "after_fix_message": "The fix is live."},
    )
    assert fixed.status_code == 200
    assert sorted(fixed.json()["incident"]["notified_ticket_ids"]) == sorted([t1, t2])
    after = (await m2.get(f"/api/v1/support/tickets/{t2}")).json()
    assert after["status"] == "resolved" and after["messages"][-1]["text"] == "The fix is live."
    assert (await agent.patch(f"{OPS}/incidents/{incident_id}", json={"title": "x"})).status_code == 403


async def test_bulk_add_to_incident(make_client):
    lead, _ = await _staff(make_client, "lead")
    m, _ = await _member(make_client)
    a = (await _file(m, subject="a"))["id"]
    b = (await _file(m, subject="b"))["id"]
    incident = (await lead.post(f"{OPS}/incidents", json={"title": "Thing", "category": "Publishing"})).json()["incident"]
    res = await lead.post(f"{OPS}/tickets/bulk", json={"ids": [a, b], "action": "add_to_incident", "payload": {"incident_id": incident["id"]}})
    assert sorted(res.json()["done"]) == sorted([a, b])
    again = await lead.post(f"{OPS}/tickets/bulk", json={"ids": [a], "action": "add_to_incident", "payload": {"incident_id": incident["id"]}})
    assert again.json()["failed"][0]["id"] == a


# ── Escalation ───────────────────────────────────────────────────────────────
async def test_escalation_hands_the_ticket_to_engineering(make_client):
    member, _ = await _member(make_client)
    staff, staff_user = await _staff(make_client)
    tid = (await _file(member))["id"]

    res = await staff.post(
        f"{OPS}/tickets/{tid}/escalate",
        json={"title": "LinkedIn token refresh loops", "notes": "Repro: connect, wait an hour.", "eng_issue_url": "https://example.com/issue/1"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["ticket"]["status"] == "waiting_on_engineering"
    assert res.json()["ticket"]["assignee_id"] == staff_user["id"]
    incident = res.json()["incident"]
    assert incident["eng_issue_url"] == "https://example.com/issue/1" and incident["ticket_count"] == 1
    # The engineering notes are internal.
    assert "Repro" not in (await member.get(f"/api/v1/support/tickets/{tid}")).text
    events = [e["type"] for e in (await staff.get(f"{OPS}/tickets/{tid}")).json()["events"]]
    assert "escalated" in events and "incident_linked" in events

    # Cannot escalate twice, or a finished ticket.
    assert (await staff.post(f"{OPS}/tickets/{tid}/escalate", json={"title": "again", "notes": "x"})).status_code == 409

    # A second ticket can join the same incident.
    other = (await _file(member, subject="also", category="Billing"))["id"]
    joined = await staff.post(
        f"{OPS}/tickets/{other}/escalate", json={"title": "same", "notes": "same issue", "incident_id": incident["id"]}
    )
    assert joined.status_code == 200 and joined.json()["incident"]["ticket_count"] == 2


# ── View as member ───────────────────────────────────────────────────────────
async def test_view_as_member_is_lead_only_needs_a_reason_and_is_recorded(make_client):
    member, _ = await _member(make_client)
    agent, _ = await _staff(make_client, "agent")
    lead, _ = await _staff(make_client, "lead")
    tid = (await _file(member))["id"]
    await lead.post(f"{OPS}/tickets/{tid}/messages", json={"text": "INTERNAL only", "is_internal": True})

    assert (await agent.post(f"{OPS}/tickets/{tid}/view-as", json={"reason": "Member says the page is blank"})).status_code == 403
    assert (await lead.post(f"{OPS}/tickets/{tid}/view-as", json={"reason": "short"})).status_code == 422

    res = await lead.post(f"{OPS}/tickets/{tid}/view-as", json={"reason": "Member says the page is blank"})
    assert res.status_code == 200
    assert "INTERNAL only" not in res.text  # exactly what the member sees
    events = (await lead.get(f"{OPS}/tickets/{tid}")).json()["events"]
    viewed = [e for e in events if e["type"] == "viewed_as_member"]
    assert viewed and viewed[0]["data"]["reason"] == "Member says the page is blank"


# ── Muting ───────────────────────────────────────────────────────────────────
async def test_lead_can_mute_and_unmute_a_member(make_client):
    member, _ = await _member(make_client)
    agent, _ = await _staff(make_client, "agent")
    lead, _ = await _staff(make_client, "lead")
    tid = (await _file(member))["id"]

    assert (await agent.post(f"{OPS}/tickets/{tid}/mute-member", json={"muted": True})).status_code == 403
    assert (await lead.post(f"{OPS}/tickets/{tid}/mute-member", json={"muted": True})).json() == {"muted": True}
    blocked = await member.post("/api/v1/support/tickets", json={"subject": "s", "category": "Other", "severity": "P2", "description": "d"})
    assert blocked.status_code == 403
    # Existing tickets still work for them.
    assert (await member.post(f"/api/v1/support/tickets/{tid}/messages", json={"text": "still here"})).status_code == 200
    await lead.post(f"{OPS}/tickets/{tid}/mute-member", json={"muted": False})
    allowed = await member.post("/api/v1/support/tickets", json={"subject": "s", "category": "Other", "severity": "P2", "description": "d"})
    assert allowed.status_code == 200


# ── People who have gone ─────────────────────────────────────────────────────
async def test_ticket_of_a_deleted_member_is_read_only(make_client):
    member, member_user = await _member(make_client)
    staff, _ = await _staff(make_client)
    tid = (await _file(member))["id"]
    await users.delete_one({"id": member_user["id"]})

    detail = (await staff.get(f"{OPS}/tickets/{tid}")).json()
    assert detail["ticket"]["created_by_deleted"] is True
    listed = next(t for t in (await staff.get(f"{OPS}/tickets")).json()["tickets"] if t["id"] == tid)
    assert listed["created_by_deleted"] is True

    reply = await staff.post(f"{OPS}/tickets/{tid}/messages", json={"text": "Hello?"})
    assert reply.status_code == 409
    note = await staff.post(f"{OPS}/tickets/{tid}/messages", json={"text": "Account gone", "is_internal": True})
    assert note.status_code == 200


async def test_tickets_of_a_leaver_go_back_to_the_queue(make_client):
    member, _ = await _member(make_client)
    lead, _ = await _staff(make_client, "lead")
    leaver, leaver_user = await _staff(make_client, "agent")
    tid = (await _file(member))["id"]
    await leaver.post(f"{OPS}/tickets/{tid}/claim")
    await lead.post(f"{OPS}/notifications/read")

    # They leave the support team.
    await users.update_one({"id": leaver_user["id"]}, {"$set": {"is_platform_staff": False}})
    done = await run_support_lifecycle(datetime.now(timezone.utc) + timedelta(minutes=1))
    assert done["unassigned"] >= 1
    ticket = await support_tickets_doc(tid)
    assert ticket["assignee_id"] is None
    events = [e["type"] for e in (await lead.get(f"{OPS}/tickets/{tid}")).json()["events"]]
    assert "unassigned" in events
    types = [n["type"] for n in (await lead.get(f"{OPS}/notifications")).json()["notifications"]]
    assert "tickets_orphaned" in types
    # And the lead can reassign in bulk.
    _, lead_user = await _staff(make_client, "lead")
    res = await lead.post(f"{OPS}/tickets/bulk", json={"ids": [tid], "action": "assign", "payload": {"assignee_id": lead_user["id"]}})
    assert res.json()["done"] == [tid]
