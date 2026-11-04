"""Ops, Team: the three levels (Master Admin, Admin, staff), invites, the permission grid, exceptions, deactivation, and the rules
that keep it safe: nobody gives access equal to or above their own, nobody changes their own access, there is always a Master Admin,
and every Ops area answers only to the people its role allows."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from uuid import uuid4

from app.core import staff as ST
from app.db.mongo import users
from tests.test_payments import standin  # noqa: F401 - the fixture is used by name


async def _master(signup_user):
    client, profile = await signup_user("Founder")
    await users.update_one({"id": profile["id"]}, {"$set": {"is_master_admin": True, "is_platform_staff": True}})
    ST.forget()
    return client, profile


async def _join(master, signup_user, name: str, level: str, role_id: str | None = None):
    """Someone signs up, is invited at a level, and accepts."""
    client, profile = await signup_user(name)
    res = await master.post("/api/v1/ops/team/invites", json={
        "email": profile["email"], "name": name, "designation": f"{name} lead", "level": level, **({"role_id": role_id} if role_id else {}),
    })
    assert res.status_code == 200, res.text
    token = res.json()["link"].rsplit("/", 1)[-1]
    seen = (await client.get(f"/api/v1/ops/team/invite/{token}")).json()
    assert seen["email_matches"] is True
    assert (await client.post(f"/api/v1/ops/team/invite/{token}/accept")).status_code == 200
    ST.forget()
    return client, profile


async def test_the_starting_roles_and_the_master_admins_view(signup_user):
    master, _ = await _master(signup_user)
    me = (await master.get("/api/v1/ops/team/me")).json()
    assert me["level"] == "master_admin" and "team" in me["areas"]
    roles = (await master.get("/api/v1/ops/team/roles")).json()["roles"]
    assert len(roles) >= 17
    on = {r["name"] for r in roles if r["active"]}
    assert {"Support Agent", "Customer Success", "Finance", "Marketing", "Content Editor", "Engineer", "Trust & Safety", "Viewer"} <= on
    # roles below Admin never manage the team
    assert all("team" not in r["permissions"] for r in roles)


async def test_someone_who_is_not_staff_sees_nothing_in_ops(signup_user):
    member, _ = await signup_user("Customer")
    assert (await member.get("/api/v1/ops/team/me")).json()["level"] is None
    assert (await member.get("/api/v1/ops/support/staff")).status_code == 403
    assert (await member.get("/api/v1/ops/team/staff")).status_code == 403


async def test_a_staff_role_opens_only_its_areas_and_an_exception_adds_one(signup_user):
    master, _ = await _master(signup_user)
    agent, profile = await _join(master, signup_user, "Agent", "staff", "support_agent")
    me = (await agent.get("/api/v1/ops/team/me")).json()
    assert me["level"] == "staff" and me["role_name"] == "Support Agent" and me["designation"] == "Agent lead"
    assert (await agent.get("/api/v1/ops/support/staff")).status_code == 200
    # errors and status are not a Support Agent's
    assert (await agent.get("/api/v1/ops/health/staff")).status_code == 403
    # billing is view only for them: reading works, changing does not
    assert (await agent.get("/api/v1/ops/billing/overview")).status_code == 200
    assert (await agent.put("/api/v1/ops/billing/settings", json={"checkout_open": True, "reason": "try"})).status_code == 403
    # staff never manage the team
    assert (await agent.get("/api/v1/ops/team/staff")).status_code == 403
    # an exception from the Master Admin opens one more area for this person only
    res = await master.patch(f"/api/v1/ops/team/staff/{profile['id']}", json={"exceptions": {"health": ["view"]}, "reason": "Covers on-call"})
    assert res.status_code == 200, res.text
    ST.forget()
    assert (await agent.get("/api/v1/ops/health/staff")).status_code == 200


async def test_admins_run_ops_but_never_give_access_at_or_above_their_own(signup_user):
    master, _ = await _master(signup_user)
    admin, _ = await _join(master, signup_user, "Admin", "admin")
    me = (await admin.get("/api/v1/ops/team/me")).json()
    assert me["level"] == "admin" and me["areas"]["team"] == ["view"]
    assert (await admin.get("/api/v1/ops/health/staff")).status_code == 200
    # money settings stay with the Master Admin
    assert (await admin.put("/api/v1/ops/billing/settings", json={"checkout_open": True, "reason": "open it"})).status_code == 403
    # no inviting until the Master Admin allows it, and never an Admin
    other, other_profile = await signup_user("Newcomer")
    body = {"email": other_profile["email"], "name": "Newcomer", "level": "staff", "role_id": "finance"}
    assert (await admin.post("/api/v1/ops/team/invites", json=body)).status_code == 403
    assert (await master.put("/api/v1/ops/team/settings", json={"admins_may_invite": True, "reason": "Support lead hires"})).status_code == 200
    ST.forget()
    assert (await admin.post("/api/v1/ops/team/invites", json=body)).status_code == 200
    assert (await admin.post("/api/v1/ops/team/invites", json={**body, "level": "admin"})).status_code == 403
    # no changes to the grid or roles
    assert (await admin.patch("/api/v1/ops/team/roles/finance", json={"permissions": {"billing": ["view"]}, "reason": "x y z"})).status_code == 403
    assert (await admin.get("/api/v1/ops/team/history")).status_code == 403


async def test_nobody_changes_their_own_access_and_a_master_admin_always_remains(signup_user):
    master, master_profile = await _master(signup_user)
    res = await master.patch(f"/api/v1/ops/team/staff/{master_profile['id']}", json={"level": "admin", "reason": "step down"})
    assert res.status_code == 403
    second, second_profile = await _join(master, signup_user, "Second", "admin")
    # the only Master Admin can't be deactivated or demoted by anyone
    res = await master.post(f"/api/v1/ops/team/staff/{master_profile['id']}/deactivate", json={"reason": "leaving"})
    assert res.status_code == 403
    res = await second.post(f"/api/v1/ops/team/staff/{master_profile['id']}/deactivate", json={"reason": "take over"})
    assert res.status_code == 403
    # a second Master Admin, made by the first, then the first may step down
    assert (await master.patch(f"/api/v1/ops/team/staff/{second_profile['id']}", json={"level": "master_admin", "reason": "co-founder"})).status_code == 200
    ST.forget()
    assert (await second.get("/api/v1/ops/team/me")).json()["level"] == "master_admin"


async def test_deactivating_cuts_access_at_once_and_reactivating_restores_it(signup_user):
    master, _ = await _master(signup_user)
    agent, profile = await _join(master, signup_user, "Leaver", "staff", "support_agent")
    assert (await agent.get("/api/v1/ops/support/staff")).status_code == 200
    assert (await master.post(f"/api/v1/ops/team/staff/{profile['id']}/deactivate", json={"reason": "left the company"})).status_code == 200
    ST.forget()
    assert (await agent.get("/api/v1/ops/support/staff")).status_code == 403
    staff = (await master.get("/api/v1/ops/team/staff")).json()["staff"]
    assert next(s for s in staff if s["id"] == profile["id"])["status"] == "deactivated"
    assert (await master.post(f"/api/v1/ops/team/staff/{profile['id']}/reactivate", json={"reason": "came back"})).status_code == 200
    ST.forget()
    assert (await agent.get("/api/v1/ops/support/staff")).status_code == 200


async def test_an_invite_works_once_for_its_own_email_and_runs_out(signup_user):
    master, _ = await _master(signup_user)
    wrong, _ = await signup_user("Someone else")
    invited, profile = await signup_user("Invited")
    res = await master.post("/api/v1/ops/team/invites", json={"email": profile["email"], "name": "Invited", "level": "staff", "role_id": "viewer"})
    token = res.json()["link"].rsplit("/", 1)[-1]
    assert (await wrong.post(f"/api/v1/ops/team/invite/{token}/accept")).status_code == 403
    assert (await invited.post(f"/api/v1/ops/team/invite/{token}/accept")).status_code == 200
    assert (await invited.post(f"/api/v1/ops/team/invite/{token}/accept")).status_code == 410
    # an old invite no longer opens
    later, later_profile = await signup_user("Late")
    res = await master.post("/api/v1/ops/team/invites", json={"email": later_profile["email"], "name": "Late", "level": "staff", "role_id": "viewer"})
    token = res.json()["link"].rsplit("/", 1)[-1]
    await ST.invites_col().update_one({"token": token}, {"$set": {"expires_at": datetime.now(timezone.utc) - timedelta(minutes=1)}})
    assert (await later.post(f"/api/v1/ops/team/invite/{token}/accept")).status_code == 410
    # a role that isn't switched on can't be given
    res = await master.post("/api/v1/ops/team/invites", json={"email": "nobody@example.com", "name": "N", "level": "staff", "role_id": "analyst"})
    assert res.status_code == 400


async def test_the_master_admin_shapes_roles_and_a_role_in_use_stays_on(signup_user):
    master, _ = await _master(signup_user)
    res = await master.post("/api/v1/ops/team/roles", json={
        "name": f"Night support {datetime.now().microsecond}", "permissions": {"support": ["edit"], "team": ["edit"], "nowhere": ["view"]},
        "reason": "Night shift",
    })
    assert res.status_code == 200, res.text
    role = res.json()["role"]
    # edit brings view; unknown areas and the team are dropped
    assert role["permissions"] == {"support": ["view", "edit"]}
    await _join(master, signup_user, "Night", "staff", role["id"])
    res = await master.patch(f"/api/v1/ops/team/roles/{role['id']}", json={"active": False, "reason": "no longer needed"})
    assert res.status_code == 400
    assert (await master.delete(f"/api/v1/ops/team/roles/{role['id']}")).status_code == 400
    history = (await master.get("/api/v1/ops/team/history")).json()["history"]
    assert {"role.created", "invite.sent", "invite.accepted"} <= {h["action"] for h in history}


async def test_finance_refunds_up_to_the_limit_and_only_the_master_admin_above_it(signup_user, standin):  # noqa: F811
    from app.billing import invoices
    from tests.test_payments_lifecycle import _charge, _paid

    master, _ = await _master(signup_user)
    finance, _ = await _join(master, signup_user, "Money", "staff", "finance")
    _, ws_id, sid = await _paid(signup_user, standin, plan="duo")
    payment = await _charge(sid, ws_id, amount=189_900)
    await invoices.save_settings({"refund_limit": 100_000})
    # above the limit: the Master Admin only
    res = await finance.post(f"/api/v1/ops/billing/workspace/{ws_id}/refund", json={"payment_id": payment["id"], "reason": "Charged twice"})
    assert res.status_code == 403 and "Master Admin" in res.json()["detail"]
    # up to it: Finance may
    res = await finance.post(f"/api/v1/ops/billing/workspace/{ws_id}/refund", json={"payment_id": payment["id"], "amount": 50_000, "reason": "Part refund"})
    assert res.status_code == 200, res.text
    res = await master.post(f"/api/v1/ops/billing/workspace/{ws_id}/refund", json={"payment_id": payment["id"], "reason": "The rest"})
    assert res.status_code == 200, res.text


async def test_approve_is_needed_for_takedowns(signup_user):
    master, _ = await _master(signup_user)
    engineer, _ = await _join(master, signup_user, "Engineer", "staff", "engineer")
    res = await engineer.post("/api/v1/ops/video/takedown", json={"workspace_id": "nope", "media_id": "nope", "reason": "Reported"})
    assert res.status_code == 403


async def test_support_and_prompt_roles_come_from_team(signup_user):
    from app.db.mongo import users as users_col

    master, _ = await _master(signup_user)
    agent, agent_profile = await _join(master, signup_user, "Agent", "staff", "support_agent")
    admin, admin_profile = await _join(master, signup_user, "Admin", "admin")
    assert (await users_col.find_one({"id": agent_profile["id"]}))["support_role"] == "agent"
    assert (await users_col.find_one({"id": admin_profile["id"]}))["support_role"] == "admin"
    # Approve on Support makes a lead, at once for everyone with the role
    roles = (await master.get("/api/v1/ops/team/roles")).json()["roles"]
    perms = next(r for r in roles if r["id"] == "support_agent")["permissions"]
    perms["support"] = ["view", "edit", "approve"]
    assert (await master.patch("/api/v1/ops/team/roles/support_agent", json={"permissions": perms, "reason": "Agents may close"})).status_code == 200
    assert (await users_col.find_one({"id": agent_profile["id"]}))["support_role"] == "lead"
    # the prompt library: Admins run it, Approve on Prompts publishes, others draft
    ai, _ = await _join(master, signup_user, "Prompter", "staff", "engineer")
    assert (await admin.get("/api/v1/ops/prompt-library/session")).json()["role"] == "admin"
    assert (await ai.get("/api/v1/ops/prompt-library/session")).json()["role"] == "editor"
    # access to prompts is no longer given inside the library
    res = await master.post("/api/v1/ops/prompt-library/staff", json={"email": "x@y.co", "role": "editor"})
    assert res.status_code == 400


async def test_the_history_pages_filters_and_exports_and_the_team_exports_with_approve(signup_user):
    master, _ = await _master(signup_user)
    agent, _ = await _join(master, signup_user, "Paged", "staff", "support_agent")
    res = (await master.get("/api/v1/ops/team/history", params={"action": "invite"})).json()
    assert res["page"] == 0 and res["history"] and all(h["action"].startswith("invite.") for h in res["history"])
    assert (await master.get("/api/v1/ops/team/history", params={"action": "Bad Value"})).status_code == 422
    res = await master.get("/api/v1/ops/team/history/export.csv", params={"action": "invite"})
    assert res.status_code == 200 and res.content.decode("utf-8-sig").splitlines()[0] == "When,Who,What,About,Reason"
    assert (await master.get("/api/v1/ops/team/staff/export.csv")).status_code == 200
    assert (await agent.get("/api/v1/ops/team/staff/export.csv")).status_code == 403
    exports = (await master.get("/api/v1/ops/team/history", params={"action": "export"})).json()["history"]
    assert {e["after"]["what"] for e in exports} >= {"team", "history"}


async def test_the_staff_ledger_counts_what_each_person_did_and_is_for_the_master_only(signup_user):
    master, profile = await _master(signup_user)
    agent, _ = await _join(master, signup_user, "Ledgered", "staff", "support_agent")
    assert (await agent.get("/api/v1/ops/team/ledger")).status_code == 403
    ledger = (await master.get("/api/v1/ops/team/ledger")).json()
    assert ledger["days"] == 30
    me = next(p for p in ledger["people"] if p["email"] == profile["email"])
    assert me["actions"] >= 1 and me["last_at"] and me["kinds"], "the invite the master sent is in the ledger"
    assert any(p["email"] != profile["email"] and p["status"] in ("active", "former") for p in ledger["people"])


async def test_a_custom_role_made_first_never_leaves_out_the_starting_roles(signup_user):
    master, _ = await _master(signup_user)
    await ST.roles_col().delete_many({})
    await ST.settings_col().delete_one({"_id": "templates"})
    res = await master.post("/api/v1/ops/team/roles", json={"name": f"Early {uuid4().hex[:5]}", "permissions": {"support": ["view"]}, "reason": "First role"})
    assert res.status_code == 200
    await _join(master, signup_user, "Market", "staff", "marketing")
    # a starting role deleted on purpose is not written back
    assert (await master.delete("/api/v1/ops/team/roles/analyst")).status_code == 200
    await ST.ensure_templates()
    assert await ST.roles_col().find_one({"id": "analyst"}) is None
    assert await ST.roles_col().find_one({"id": "viewer"}) is not None
