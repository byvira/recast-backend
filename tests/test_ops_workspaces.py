"""Ops, Workspaces and people: finding any workspace or person, everything about each, notes and flags, a member's role changed with a
reason (never the owner's), exports that need Approve and are kept in the history, and who may see any of it."""
from __future__ import annotations

from uuid import uuid4

from app.core import staff as ST
from app.db.mongo import users
from tests.conftest import create_workspace
from tests.test_ops_team import _join, _master


async def _customer(signup_user, name: str):
    client, profile = await signup_user(name)
    ws_id = await create_workspace(client, f"{name} Studio {uuid4().hex[:5]}")
    return client, profile, ws_id


async def test_staff_find_a_workspace_and_see_its_whole_picture(signup_user):
    master, _ = await _master(signup_user)
    _, owner, ws_id = await _customer(signup_user, "Asha")
    res = await master.get("/api/v1/ops/workspaces", params={"q": owner["email"]})
    assert res.status_code == 200
    found = res.json()["workspaces"]
    assert any(w["id"] == ws_id and w["owner_email"] == owner["email"] for w in found)
    for tab in ("", "/brands", "/members", "/content", "/billing", "/activity", "/notes"):
        assert (await master.get(f"/api/v1/ops/workspaces/{ws_id}{tab}")).status_code == 200, tab
    overview = (await master.get(f"/api/v1/ops/workspaces/{ws_id}")).json()
    assert overview["owner"]["email"] == owner["email"] and overview["counts"]["members"] == 1
    # filters answer, even when nothing matches
    for params in ({"purpose": "work"}, {"plan": "free"}, {"flagged": "true"}, {"payment_trouble": "true"}, {"quiet": "true"}):
        assert (await master.get("/api/v1/ops/workspaces", params=params)).status_code == 200


async def test_the_person_view_shows_every_workspace_and_links_back(signup_user):
    master, _ = await _master(signup_user)
    _, owner, ws_id = await _customer(signup_user, "Ravi")
    people = (await master.get("/api/v1/ops/people", params={"q": owner["email"]})).json()["people"]
    assert people and people[0]["email"] == owner["email"] and people[0]["workspaces"] >= 1
    detail = (await master.get(f"/api/v1/ops/people/{owner['id']}")).json()
    assert any(w["id"] == ws_id and w["role"] == "owner" for w in detail["workspaces"])
    assert {"payments", "tickets", "devices", "recent"} <= set(detail)
    assert detail["staff"] is None
    assert (await master.get("/api/v1/ops/people/nobody-here")).status_code == 404


async def test_notes_flags_and_a_role_change_are_kept_and_the_owner_stays_owner(signup_user, make_client):
    from tests.conftest import invite_and_accept

    master, _ = await _master(signup_user)
    owner_client, owner, ws_id = await _customer(signup_user, "Mira")
    member_client, member = await invite_and_accept(owner_client, make_client, ws_id, "editor")
    res = await master.post(f"/api/v1/ops/workspaces/{ws_id}/notes", json={"text": "Asked about refunds", "flag": True})
    assert res.status_code == 200
    note_id = res.json()["note"]["id"]
    assert any(w["id"] == ws_id for w in (await master.get("/api/v1/ops/workspaces", params={"flagged": "true"})).json()["workspaces"])
    assert (await master.post(f"/api/v1/ops/workspaces/{ws_id}/notes/{note_id}/resolve", json={"reason": "Answered"})).status_code == 200
    assert (await master.post(f"/api/v1/ops/workspaces/{ws_id}/notes/{note_id}/resolve", json={"reason": "Again"})).status_code == 404
    res = await master.put(f"/api/v1/ops/workspaces/{ws_id}/members/{member['id']}/role", json={"role": "viewer", "reason": "Owner asked by email"})
    assert res.status_code == 200, res.text
    members = (await master.get(f"/api/v1/ops/workspaces/{ws_id}/members")).json()["members"]
    assert next(m for m in members if m["user_id"] == member["id"])["role"] == "viewer"
    res = await master.put(f"/api/v1/ops/workspaces/{ws_id}/members/{owner['id']}/role", json={"role": "viewer", "reason": "Try"})
    assert res.status_code == 400
    history = (await master.get(f"/api/v1/ops/workspaces/{ws_id}/notes")).json()["notes"]
    assert any(n["kind"] == "action" and "viewer" in n["text"] for n in history)


async def test_exports_need_approve_are_safe_to_open_and_are_kept(signup_user):
    master, _ = await _master(signup_user)
    _, owner, ws_id = await _customer(signup_user, "=Formula")
    res = await master.get("/api/v1/ops/workspaces/export.csv", params={"q": owner["email"]})
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/csv")
    text = res.content.decode("utf-8-sig")
    assert text.splitlines()[0].startswith("Workspace,Id")
    assert "'=Formula" in text  # a cell that starts like a formula never runs in a spreadsheet
    for what in ("members", "content", "payments", "notes"):
        assert (await master.get(f"/api/v1/ops/workspaces/{ws_id}/export/{what}.csv")).status_code == 200, what
    assert (await master.get("/api/v1/ops/people/export.csv")).status_code == 200
    # Customer Success can look and change, but not export customer data in bulk
    cs, _ = await _join(master, signup_user, "Success", "staff", "customer_success")
    assert (await cs.get(f"/api/v1/ops/workspaces/{ws_id}")).status_code == 200
    assert (await cs.get("/api/v1/ops/workspaces/export.csv")).status_code == 403
    history = (await master.get("/api/v1/ops/team/history")).json()["history"]
    assert any(h["action"] == "export" for h in history)


async def test_only_staff_with_the_area_see_workspaces_and_people(signup_user):
    master, _ = await _master(signup_user)
    customer, _, ws_id = await _customer(signup_user, "Outsider")
    assert (await customer.get("/api/v1/ops/workspaces")).status_code == 403
    assert (await customer.get("/api/v1/ops/people")).status_code == 403
    engineer, _ = await _join(master, signup_user, "Engineer", "staff", "engineer")
    assert (await engineer.get(f"/api/v1/ops/workspaces/{ws_id}")).status_code == 403
    # a Support Agent can look but not change
    agent, profile = await _join(master, signup_user, "Agent", "staff", "support_agent")
    assert (await agent.get(f"/api/v1/ops/workspaces/{ws_id}")).status_code == 200
    assert (await agent.post(f"/api/v1/ops/workspaces/{ws_id}/notes", json={"text": "hello there"})).status_code == 403
    await users.update_one({"id": profile["id"]}, {"$set": {"staff.status": "deactivated", "is_platform_staff": False}})
    ST.forget()
    assert (await agent.get(f"/api/v1/ops/workspaces/{ws_id}")).status_code == 403
