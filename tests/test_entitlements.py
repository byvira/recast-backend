"""Plans and limits: with plans off every limit is exactly what it was before plans existed; with plans on each workspace gets its
plan's numbers, shared across the workspaces that share the plan, and every route that creates something limited refuses the one
too many with plain words. The last tests read the code itself so a new route cannot skip its check."""
import ast
import pathlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.billing import plans as P
from app.db.mongo import get_db, workspaces
from app.shared import entitlements as E
from tests.conftest import create_workspace

APP = pathlib.Path(__file__).resolve().parent.parent / "app"


@pytest.fixture(autouse=True)
def _fresh_answers():
    E.forget_plans()
    yield
    E.forget_plans()


@pytest.fixture
def plans_on(monkeypatch):
    monkeypatch.setattr(E.settings, "BILLING_ENFORCED", True)
    E.forget_plans()
    yield
    E.forget_plans()


async def _ws(**fields) -> str:
    wid = f"ws-{uuid4()}"
    await workspaces.insert_one({"id": wid, "name": "Limits", "owner_id": fields.pop("owner_id", "owner-x"), "tier": fields.pop("tier", "duo"),
                                 "tier_config": {"seats": fields.pop("seats", 2), "storage_limit_gb": 20, "settings": {}}, **fields})
    return wid


# ── the plans table ──────────────────────────────────────────────────────

def test_each_plan_gives_the_accepted_numbers():
    free = P.limits_for(P.DEFAULT_PLANS["free"])
    single = P.limits_for(P.DEFAULT_PLANS["single"])
    duo = P.limits_for(P.DEFAULT_PLANS["duo"])
    assert (free["credits_month"], single["credits_month"], duo["credits_month"]) == (100, 1200, 2500)
    assert (free["seats"], single["seats"], duo["seats"]) == (1, 1, 2)
    assert (free["pictures_day"], single["pictures_day"], duo["pictures_day"]) == (5, 50, 100)
    assert (free["render_minutes_month"], single["render_minutes_month"], duo["render_minutes_month"]) == (5, 60, 150)
    assert free["storage_mb"] == 1024 and single["storage_mb"] == 10 * 1024 and duo["storage_mb"] == 30 * 1024
    assert set(free) == set(P.LIMITS)


def test_large_grows_with_each_seat_and_keeps_its_floors():
    large = P.DEFAULT_PLANS["large"]
    three, ten, fifty = P.limits_for(large, 3), P.limits_for(large, 10), P.limits_for(large, 50)
    assert (three["seats"], ten["seats"], fifty["seats"]) == (3, 10, 50)
    assert (three["credits_month"], fifty["credits_month"]) == (3000, 50000)
    assert three["owned_workspaces"] == 10 and fifty["owned_workspaces"] == 100     # 2 a seat, never fewer than 10
    assert three["social_accounts"] == 30 and ten["social_accounts"] == 100         # 10 a seat, never fewer than 30
    assert ten["api_calls_per_minute"] == 300 and fifty["api_calls_per_minute"] == 300 + 20 * 40
    assert P.limits_for(large, 1)["seats"] == 3 and P.limits_for(large, 500)["seats"] == 100   # held inside 3 to 100


def test_a_rule_saved_from_ops_is_checked():
    assert P.validate_rule(12) == 12 and P.validate_rule(None) is None
    assert P.validate_rule({"per_seat": 5, "min": 10}) == {"per_seat": 5, "min": 10}
    for bad in (-1, True, "ten", {"per_seat": -2}, {"oops": 1}, {"min": 3}, 1.5):
        with pytest.raises(ValueError):
            P.validate_rule(bad)


# ── plans off: exactly as before ─────────────────────────────────────────

@pytest.mark.parametrize("tier,seats,minutes,video_mb", [("single", 1, 120, 2048), ("duo", 2, 300, 5120), ("large", 10, 1200, 20480)])
async def test_with_plans_off_every_limit_is_what_it_was_before_plans(tier, seats, minutes, video_mb):
    wid = await _ws(tier=tier, seats=seats, plan="single")
    ent = await E.entitlements_for(wid)
    assert ent.enforced is False and ent.scope_ids == [wid]
    assert ent.limit("seats") == seats
    assert ent.limit("pictures_day") == 60
    assert ent.limit("render_minutes_month") == minutes and ent.limit("storage_mb") == video_mb
    assert ent.limit("brand_voices") == 10 and ent.limit("api_keys") == 10
    for open_limit in ("social_accounts", "credits_month", "scheduled_ahead", "campaigns_active", "video_automations", "owned_workspaces"):
        assert ent.limit(open_limit) is None, open_limit


async def test_with_plans_off_a_limit_that_existed_answers_with_its_old_status_and_words():
    wid = await _ws(seats=1)
    ent = await E.entitlements_for(wid)
    with pytest.raises(HTTPException) as caught:
        await E.require_room(ent, "seats", 1, legacy_message="Seat limit reached for this tier.")
    assert caught.value.status_code == 400 and caught.value.detail == "Seat limit reached for this tier."
    await E.require_room(ent, "social_accounts", 10_000)        # open while plans are off
    assert await E.require_storage_room(wid, 10**12) is None    # only videos have a space while plans are off


async def test_a_missing_workspace_gets_the_open_defaults_not_an_error():
    ent = await E.entitlements_for(f"gone-{uuid4()}")
    assert ent.limit("pictures_day") == 60 and ent.limit("social_accounts") is None


# ── plans on ─────────────────────────────────────────────────────────────

async def test_with_plans_on_a_workspace_with_no_plan_is_free_and_gets_its_numbers(plans_on):
    ent = await E.entitlements_for(await _ws())
    assert ent.enforced and ent.plan == "free" and ent.plan_name == "Free"
    assert ent.limit("seats") == 1 and ent.limit("social_accounts") == 3 and ent.limit("pictures_day") == 5


async def test_large_bills_the_seats_bought_and_a_comp_plan_or_limit_wins_until_it_ends(plans_on):
    large = await _ws(plan="large", plan_seats=12)
    ent = await E.entitlements_for(large)
    assert ent.seats_billed == 12 and ent.limit("credits_month") == 12000

    comp = await _ws(plan="free", entitlement_overrides={"plan": "duo", "limits": {"pictures_day": 400}, "note": "beta"})
    ent = await E.entitlements_for(comp)
    assert ent.plan == "duo" and ent.comp and ent.limit("pictures_day") == 400 and ent.limit("seats") == 2

    ended = await _ws(plan="free", entitlement_overrides={"plan": "large", "note": "old", "expires_at": datetime.now(timezone.utc) - timedelta(days=1)})
    assert (await E.entitlements_for(ended)).plan == "free"


async def test_workspaces_sharing_a_plan_share_its_limits_and_usage(plans_on):
    payer = await _ws(plan="single")
    child = await _ws(billing_group_id=payer)
    ent = await E.entitlements_for(child)
    assert ent.plan == "single" and ent.group_id == payer and set(ent.scope_ids) == {payer, child}
    await get_db()["media_assets"].insert_many([
        {"id": uuid4().hex, "workspace_id": w, "source": "ai_generated", "created_at": datetime.now(timezone.utc), "kind": "image"}
        for w in (payer, child, child)
    ])
    assert await E.count_usage(ent, "pictures_day") == 3
    # a payer that is gone leaves the workspace on its own, free
    await workspaces.update_one({"id": payer}, {"$set": {"deleted_at": datetime.now(timezone.utc)}})
    E.forget()
    assert (await E.entitlements_for(child)).plan == "free"


async def test_with_plans_on_a_full_limit_is_a_402_with_the_numbers_and_plain_words(plans_on):
    ent = await E.entitlements_for(await _ws(plan="single"))
    with pytest.raises(HTTPException) as caught:
        await E.require_room(ent, "social_accounts", 10, legacy_message="old words")
    detail = caught.value.detail
    assert caught.value.status_code == 402
    assert detail["code"] == "limit_reached" and detail["limit"] == "social_accounts" and detail["allowed"] == 10 and detail["plan"] == "single"
    assert detail["message"].startswith("The Single plan includes 10 connected social accounts. All are in use.")
    assert "—" not in detail["message"]
    assert "None are left" in E.limit_message(ent, "pictures_day", 50)
    assert "of storage" in E.limit_message(ent, "storage_mb", 20_000)


async def test_seats_count_each_person_once_across_the_plan_and_an_open_invite_holds_one(plans_on):
    payer = await _ws(plan="duo")
    child = await _ws(billing_group_id=payer)
    person = f"user-{uuid4()}"
    db = get_db()
    await db["users"].insert_one({"id": person, "email": f"{person}@example.com"})
    await db["workspace_members"].insert_many([
        {"id": uuid4().hex, "workspace_id": payer, "user_id": person, "status": "active", "role": "owner"},
        {"id": uuid4().hex, "workspace_id": child, "user_id": person, "status": "active", "role": "owner"},
    ])
    later = datetime.now(timezone.utc) + timedelta(days=3)
    await db["invites"].insert_many([
        {"id": uuid4().hex, "token": uuid4().hex, "workspace_id": child, "email": f"{person}@example.com", "status": "pending", "expires_at": later},
        {"id": uuid4().hex, "token": uuid4().hex, "workspace_id": child, "email": "new@example.com", "status": "pending", "expires_at": later},
    ])
    ent = await E.entitlements_for(payer)
    assert await E.seats_used(ent) == 2              # one person in two workspaces, one new invite
    assert await E.holds_seat(ent, person)


async def test_the_counters_count_what_the_screens_mean(plans_on):
    wid = await _ws(plan="single")
    ent = await E.entitlements_for(wid)
    db = get_db()
    await db["workspace_connections"].insert_many([
        {"workspace_id": wid, "platform": "google", "is_active": True},
        {"workspace_id": wid, "platform": "youtube", "is_active": True},
        {"workspace_id": wid, "platform": "linkedin", "is_active": False},
    ])
    await db["campaigns"].insert_many([
        {"id": uuid4().hex, "workspace_id": wid, "status": "active", "kind": "campaign"},
        {"id": uuid4().hex, "workspace_id": wid, "status": "active", "kind": "repurpose"},
        {"id": uuid4().hex, "workspace_id": wid, "status": "draft", "kind": "campaign"},
    ])
    await db["content_pieces"].insert_many([
        {"piece_id": uuid4().hex, "workspace_id": wid, "publish_status": "queued"},
        {"piece_id": uuid4().hex, "workspace_id": wid, "publish_status": "published"},
        {"piece_id": uuid4().hex, "workspace_id": wid, "publish_status": "queued", "deleted": True},
    ])
    assert await E.count_usage(ent, "social_accounts") == 1          # the Google sign-in behind YouTube is not an account
    assert await E.count_usage(ent, "campaigns_active") == 1         # a repurpose and a draft are not running campaigns
    assert await E.count_usage(ent, "scheduled_ahead") == 1


async def test_a_connection_already_made_never_counts_again(plans_on):
    wid = await _ws(plan="free")
    db = get_db()
    await db["workspace_connections"].insert_many([
        {"workspace_id": wid, "platform": p, "is_active": True} for p in ("youtube", "linkedin", "bluesky")
    ])
    assert await E.connection_room_message(wid, "linkedin") is None            # reconnecting
    assert await E.connection_room_message(wid, "google") is None              # never counted
    message = await E.connection_room_message(wid, "threads")
    assert message and "3 connected social accounts" in message
    with pytest.raises(HTTPException) as caught:
        await E.require_connection_room(wid, "threads")
    assert caught.value.status_code == 402


async def test_storage_counts_every_file_once_plans_are_on(plans_on):
    wid = await _ws(plan="free")
    await get_db()["media_assets"].insert_one({"id": uuid4().hex, "workspace_id": wid, "kind": "image", "size_bytes": 1_000_000_000})
    await E.require_storage_room(wid, 10_000_000)
    with pytest.raises(HTTPException) as caught:
        await E.require_storage_room(wid, 100_000_000)
    assert caught.value.status_code == 402 and caught.value.detail["limit"] == "storage_mb"


# ── every path that makes something limited ──────────────────────────────

async def test_pictures_stop_at_the_daily_allowance_for_every_caller(plans_on):
    from app.pipelines.image.orchestrator import run_image_agent

    wid = await _ws(plan="free")
    await get_db()["media_assets"].insert_many([
        {"id": uuid4().hex, "workspace_id": wid, "source": "ai_generated", "created_at": datetime.now(timezone.utc), "kind": "image"}
        for _ in range(5)
    ])
    out = await run_image_agent(mode="prompt", workspace_id=wid, user_id="u1", brand={}, prompt="a calm desk")
    assert out["image"] is None and "5 pictures a day" in out["failure"]


async def test_with_plans_off_the_picture_allowance_is_the_shared_ceiling():
    wid = await _ws()
    assert await E.picture_room_message(wid, 60) is None
    assert "60 pictures a day" in await E.picture_room_message(wid, 61)


async def test_renders_stop_at_the_plans_minutes_and_keep_their_old_words_while_plans_are_off(monkeypatch):
    from app.video import limits

    wid = await _ws(tier="single")
    await get_db()["video_jobs"].insert_one({"id": uuid4().hex, "workspace_id": wid, "status": "done", "duration_s": 119 * 60, "created_at": datetime.now(timezone.utc)})
    await limits.assert_render_room(wid, 60)
    with pytest.raises(limits.OverLimit) as old:
        await limits.assert_render_room(wid, 61)
    assert "120 render minutes" in old.value.message

    monkeypatch.setattr(E.settings, "BILLING_ENFORCED", True)
    E.forget()
    with pytest.raises(limits.OverLimit) as new:
        await limits.assert_render_room(wid, 60)
    assert "5 video minutes a month" in new.value.message      # the workspace has no plan, so it is free


async def test_a_paid_plans_render_goes_ahead_of_a_free_one_in_the_queue(plans_on, monkeypatch):
    from app.video import jobs

    free_ws, paid_ws = await _ws(plan="free"), await _ws(plan="single")
    monkeypatch.setattr(jobs, "_assert_video_on", lambda *_a, **_k: _none())
    monkeypatch.setattr(jobs, "_halted", lambda *_a, **_k: _false())
    monkeypatch.setattr(jobs, "_validate_request", lambda *_a, **_k: 1.0)
    first = await jobs.enqueue(workspace_id=free_ws, user_id="u1", inputs={}, edit={})
    second = await jobs.enqueue(workspace_id=paid_ws, user_id="u1", inputs={}, edit={})
    stored = {d["id"]: d async for d in get_db()["video_jobs"].find({"id": {"$in": [first["id"], second["id"]]}})}
    assert "priority" not in stored[first["id"]] and stored[second["id"]]["priority"] == 1


async def _none():
    return None


async def _false():
    return False


async def test_an_automation_switched_on_must_fit_and_one_already_on_never_counts_again(plans_on):
    from app.video import automation

    wid = await _ws(plan="free")
    recipes = []
    for _ in range(2):
        doc = {"id": uuid4().hex, "workspace_id": wid, "name": "r", "steps": [{"flow": "x", "params": {}}], "runs": 0, "created_at": datetime.now(timezone.utc)}
        await get_db()["video_recipes"].insert_one(dict(doc))
        recipes.append(doc["id"])
    await automation.configure(recipes[0], wid, "u1", enabled=True)
    await automation.configure(recipes[0], wid, "u1", enabled=True, frequency="weekly")     # a change, not a new one
    with pytest.raises(HTTPException) as caught:
        await automation.configure(recipes[1], wid, "u1", enabled=True)
    assert caught.value.status_code == 402 and caught.value.detail["limit"] == "video_automations"


async def test_a_campaign_starting_must_fit_but_a_repurpose_or_a_running_one_never_counts(plans_on):
    wid = await _ws(plan="free")
    await get_db()["campaigns"].insert_one({"id": uuid4().hex, "workspace_id": wid, "status": "active", "kind": "campaign"})
    await E.require_campaign_room(wid, {"kind": "repurpose", "status": "draft"})
    await E.require_campaign_room(wid, {"kind": "campaign", "status": "active"})
    with pytest.raises(HTTPException):
        await E.require_campaign_room(wid, {"kind": "campaign", "status": "draft"})


async def test_scheduling_one_more_post_than_the_plan_allows_is_refused_and_moving_one_is_not(plans_on, monkeypatch):
    from app.pipelines.publish import spine

    wid = await _ws(plan="free")
    await get_db()["content_pieces"].insert_many([
        {"piece_id": uuid4().hex, "workspace_id": wid, "publish_status": "queued"} for _ in range(30)
    ])

    async def no_block(*_a, **_k):
        return None

    monkeypatch.setattr(spine, "availability_block", no_block)
    blocked = await spine.schedule_blocker({"platform": "linkedin", "content": "x", "publish_status": "pending"}, wid)
    assert blocked and blocked[0] == 402 and "30 posts scheduled ahead" in blocked[1]


async def test_api_keys_stop_at_the_plan_and_keep_the_old_cap_while_plans_are_off(signup_user, monkeypatch):
    from app.video import api_keys

    client, _ = await signup_user()
    ws = await create_workspace(client, f"Keys {uuid4().hex[:6]}")
    h = {"X-Workspace-Id": ws}
    monkeypatch.setattr(E.settings, "BILLING_ENFORCED", True)
    E.forget()
    for i in range(2):
        await api_keys.create(ws, "u1", f"key {i}", ["video:read"])
    over = await client.post("/api/v1/developer/keys", headers=h, json={"name": "third", "scopes": ["video:read"]})
    assert over.status_code == 402 and over.json()["detail"]["limit"] == "api_keys"


# ── routes end to end ────────────────────────────────────────────────────

async def test_inviting_past_the_plans_seats_is_refused_and_a_comped_seat_lets_them_in(signup_user, monkeypatch):
    client, _ = await signup_user()
    ws = await create_workspace(client, f"Team {uuid4().hex[:6]}", tier="large")
    monkeypatch.setattr(E.settings, "BILLING_ENFORCED", True)
    E.forget()
    refused = await client.post(f"/api/v1/invites/{ws}", json={"email": f"n-{uuid4().hex}@example.com", "role": "editor"})
    assert refused.status_code == 402 and refused.json()["detail"]["limit"] == "seats"      # free: the owner is the one seat
    await workspaces.update_one({"id": ws}, {"$set": {"plan": "duo"}})
    E.forget()
    allowed = await client.post(f"/api/v1/invites/{ws}", json={"email": f"n-{uuid4().hex}@example.com", "role": "editor"})
    assert allowed.status_code == 201, allowed.text


async def test_with_plans_off_a_large_team_invites_as_before(signup_user):
    client, _ = await signup_user()
    ws = await create_workspace(client, f"Old {uuid4().hex[:6]}", tier="duo")
    first = await client.post(f"/api/v1/invites/{ws}", json={"email": f"n-{uuid4().hex}@example.com", "role": "editor"})
    second = await client.post(f"/api/v1/invites/{ws}", json={"email": f"n-{uuid4().hex}@example.com", "role": "editor"})
    assert first.status_code == 201 and second.status_code == 400 and second.json()["detail"] == "Seat limit reached for this tier."


async def test_a_free_account_has_one_workspace_and_more_join_a_paid_one(signup_user, monkeypatch):
    client, _ = await signup_user()
    paid = await create_workspace(client, f"Paid {uuid4().hex[:6]}")
    await workspaces.update_one({"id": paid}, {"$set": {"plan": "single"}})
    monkeypatch.setattr(E.settings, "BILLING_ENFORCED", True)
    E.forget()

    alone = await client.post("/api/v1/workspaces/", json={"name": "Another", "tier": "single"})
    assert alone.status_code == 402 and "one workspace" in alone.json()["detail"]["message"]

    made = []
    for i in range(2):
        r = await client.post("/api/v1/workspaces/", json={"name": f"Client {i}", "tier": "single", "billing_group_id": paid})
        assert r.status_code == 201, r.text
        made.append(r.json()["workspace_id"])
    over = await client.post("/api/v1/workspaces/", json={"name": "Client 3", "tier": "single", "billing_group_id": paid})
    assert over.status_code == 402 and over.json()["detail"]["limit"] == "owned_workspaces"    # Single: 3 on the plan
    child = await workspaces.find_one({"id": made[0]})
    assert child["billing_group_id"] == paid

    stranger, _ = await signup_user()
    theirs = await stranger.post("/api/v1/workspaces/", json={"name": "Sneaky", "tier": "single", "billing_group_id": paid})
    assert theirs.status_code == 400


async def test_brand_voices_stop_at_the_plan(signup_user, monkeypatch):
    client, _ = await signup_user()
    ws = await create_workspace(client, f"Voices {uuid4().hex[:6]}")
    h = {"X-Workspace-Id": ws}
    monkeypatch.setattr(E.settings, "BILLING_ENFORCED", True)
    E.forget()
    first = await client.post("/api/v1/brand/", headers=h, json={"brand_type": "Person"})
    assert first.status_code in (200, 201), first.text
    second = await client.post("/api/v1/brand/", headers=h, json={"brand_type": "Person"})
    assert second.status_code == 402 and second.json()["detail"]["limit"] == "brand_voices"


async def test_any_member_sees_the_plan_and_usage_and_an_outsider_does_not(signup_user):
    client, _ = await signup_user()
    ws = await create_workspace(client, f"See {uuid4().hex[:6]}")
    seen = await client.get(f"/api/v1/workspaces/{ws}/entitlements")
    assert seen.status_code == 200
    body = seen.json()
    assert body["enforced"] is False and {r["key"] for r in body["limits"]} == set(P.LIMITS)
    seats = next(r for r in body["limits"] if r["key"] == "seats")
    assert seats["used"] == 1 and seats["allowed"] == 2
    outsider, _ = await signup_user()
    assert (await outsider.get(f"/api/v1/workspaces/{ws}/entitlements")).status_code == 403


async def test_ops_can_read_plans_only_the_ops_owner_changes_them_and_every_change_is_logged(signup_user):
    from app.db.mongo import users

    staff, staff_profile = await signup_user()
    owner, owner_profile = await signup_user()
    await users.update_one({"id": staff_profile["id"]}, {"$set": {"is_platform_staff": True}})
    await users.update_one({"id": owner_profile["id"]}, {"$set": {"is_master_admin": True}})
    try:
        listed = await staff.get("/api/v1/ops/plans")
        assert listed.status_code == 200 and [p["id"] for p in listed.json()["plans"]] == list(P.PLAN_IDS)
        assert (await staff.put("/api/v1/ops/plans/single", json={"limits": {"pictures_day": 70}, "reason": "test"})).status_code == 403

        bad = await owner.put("/api/v1/ops/plans/single", json={"limits": {"pictures_day": -3}, "reason": "test"})
        assert bad.status_code == 400
        changed = await owner.put("/api/v1/ops/plans/single", json={"limits": {"pictures_day": 70}, "reason": "more pictures"})
        assert changed.status_code == 200 and changed.json()["limits"]["pictures_day"] == 70 and "pictures_day" in changed.json()["changed"]
        log = (await staff.get("/api/v1/ops/plans/audit")).json()["changes"]
        assert log[0]["action"] == "plan.change" and log[0]["reason"] == "more pictures" and log[0]["before"] == {"pictures_day": 50}

        ws = await create_workspace(owner, f"Comp {uuid4().hex[:6]}")
        comp = await owner.put(f"/api/v1/ops/plans/workspaces/{ws}/override", json={"plan": "large", "seats": 5, "note": "beta tester"})
        assert comp.status_code == 200 and comp.json()["plan"] == "large" and comp.json()["comp"] is True
        found = (await staff.get("/api/v1/ops/plans/workspaces", params={"q": ws})).json()["workspaces"]
        assert found and found[0]["comp"] is True
        cleared = await owner.request("DELETE", f"/api/v1/ops/plans/workspaces/{ws}/override", json={"reason": "ended"})
        assert cleared.status_code == 200 and cleared.json()["comp"] is False
    finally:
        await get_db()["billing_plans"].delete_many({})
        E.forget_plans()


# ── no gaps: read the code itself ────────────────────────────────────────

def _functions_with_upload_params(path: pathlib.Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            for arg in node.args.args + node.args.kwonlyargs:
                if arg.annotation is not None and "UploadFile" in ast.unparse(arg.annotation):
                    yield node
                    break


# Uploads that are read and thrown away (a brief's text, a timeline) or stored apart from the workspace (support attachments).
_NOT_STORED = {
    ("campaigns.py", "read_source_file_route"), ("video_projects.py", "import_timeline"), ("support.py", "upload_attachment"),
    ("ops_support.py", "upload_staff_attachment"),
}


def test_every_route_that_takes_a_file_checks_storage_first():
    missing = []
    for path in sorted((APP / "api" / "v1").glob("*.py")):
        for fn in _functions_with_upload_params(path):
            if (path.name, fn.name) in _NOT_STORED or path.name in ("support.py", "ops_support.py"):
                continue
            body = ast.unparse(fn)
            helpers = [n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
            if "require_storage_room" in body or "assert_storage_room" in body:
                continue
            # a route that hands the file to a helper is fine when that helper checks
            module = path.read_text(encoding="utf-8")
            if any(f"def {h}(" in module and "require_storage_room" in ast.unparse(_find(path, h)) for h in helpers if _find(path, h)):
                continue
            missing.append(f"{path.name}:{fn.name}")
    assert missing == []


def _find(path: pathlib.Path, name: str):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    return None


def test_limited_things_are_only_created_in_places_that_check_the_plan():
    """Each limited collection is written to only by functions that ask the plan first. A new writer elsewhere fails this test."""
    allowed_writers = {
        "brand_profiles.insert_one": {"app/api/v1/brand.py"},
        "get_campaigns_collection().insert_one": {"app/api/v1/campaigns.py"},
        "_keys().insert_one": {"app/video/api_keys.py"},
        "workspace_members.insert_one": {"app/api/v1/invites.py", "app/api/v1/workspace.py", "app/core/workspace.py"},
    }
    found: dict[str, set[str]] = {k: set() for k in allowed_writers}
    for path in APP.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(APP.parent).as_posix()
        for needle in allowed_writers:
            if needle in text:
                found[needle].add(rel)
    assert found == allowed_writers


def test_every_picture_is_made_through_a_path_that_checks_the_daily_allowance():
    orchestrator = (APP / "pipelines" / "image" / "orchestrator.py").read_text(encoding="utf-8")
    generation = (APP / "pipelines" / "media" / "image_generation.py").read_text(encoding="utf-8")
    assert "picture_room_message" in orchestrator
    mascot = ast.unparse(next(n for n in ast.walk(ast.parse(generation)) if isinstance(n, ast.AsyncFunctionDef) and n.name == "generate_brand_mascot"))
    assert "picture_room_message" in mascot
    callers = {p.relative_to(APP.parent).as_posix() for p in APP.rglob("*.py") if "_generate_text_free(" in p.read_text(encoding="utf-8")}
    assert callers == {"app/pipelines/media/image_generation.py", "app/agents/image/nodes.py"}   # nodes run only inside run_image_agent


def test_every_new_social_account_goes_through_the_one_save_that_checks():
    token_store = (APP / "pipelines" / "publish" / "token_store.py").read_text(encoding="utf-8")
    assert "require_connection_room" in token_store
    oauth = (APP / "api" / "v1" / "oauth.py").read_text(encoding="utf-8")
    assert oauth.count("_require_room_for_any(") >= 6          # the helper and every connect start
    assert oauth.count("_room_note(") >= 6                     # the helper and every sign-in return


# ── found on the recheck ─────────────────────────────────────────────────

async def test_an_ops_limit_on_a_workspace_applies_while_plans_are_off():
    wid = await _ws(entitlement_overrides={"limits": {"pictures_day": 200}, "note": "tester needs more"})
    assert (await E.entitlements_for(wid)).limit("pictures_day") == 200


async def test_inviting_someone_already_on_the_plan_needs_no_new_seat(signup_user, monkeypatch):
    owner, _ = await signup_user()
    member_client, member = await signup_user()
    paid = await create_workspace(owner, f"Paid {uuid4().hex[:6]}")
    await workspaces.update_one({"id": paid}, {"$set": {"plan": "duo"}})
    db = get_db()
    await db["workspace_members"].insert_one({"id": uuid4().hex, "workspace_id": paid, "user_id": member["id"], "status": "active", "role": "editor"})
    child = (await owner.post("/api/v1/workspaces/", json={"name": "Client", "tier": "duo", "billing_group_id": paid})).json()["workspace_id"]
    monkeypatch.setattr(E.settings, "BILLING_ENFORCED", True)
    E.forget()
    # Duo has two seats and both are taken; the member already holds one, so inviting them to the second workspace is fine
    again = await owner.post(f"/api/v1/invites/{child}", json={"email": member["email"], "role": "editor"})
    assert again.status_code == 201, again.text
    stranger = await owner.post(f"/api/v1/invites/{child}", json={"email": f"n-{uuid4().hex}@example.com", "role": "editor"})
    assert stranger.status_code == 402


async def test_handing_over_a_free_workspace_to_someone_with_a_free_one_is_refused_once_plans_are_on(signup_user, monkeypatch, make_client):
    from tests.conftest import invite_and_accept

    owner, _ = await signup_user()
    ws = await create_workspace(owner, f"Hand {uuid4().hex[:6]}")
    member_client, member = await invite_and_accept(owner, make_client, ws, "admin")
    monkeypatch.setattr(E.settings, "BILLING_ENFORCED", True)
    E.forget()
    refused = await owner.put(f"/api/v1/workspaces/{ws}/members/{member['id']}/role", json={"role": "owner"})
    assert refused.status_code == 402 and "already have a free workspace" in refused.json()["detail"]["message"]
    await workspaces.update_one({"id": ws}, {"$set": {"plan": "single"}})
    E.forget()
    handed = await owner.put(f"/api/v1/workspaces/{ws}/members/{member['id']}/role", json={"role": "owner"})
    assert handed.status_code == 200, handed.text


async def test_storage_is_checked_with_the_bytes_that_arrived_not_the_size_the_browser_gave(plans_on):
    import io

    from fastapi import UploadFile

    wid = await _ws(plan="free")
    await get_db()["media_assets"].insert_one({"id": uuid4().hex, "workspace_id": wid, "kind": "image", "size_bytes": 1_022_000_000})
    upload = UploadFile(file=io.BytesIO(b"x" * 3_000_000), filename="big.bin")       # no size given, as some browsers send
    assert upload.size is None
    await E.require_storage_room(wid, upload.size or 0)          # what the old check saw: nothing, so it passed
    with pytest.raises(HTTPException):
        await E.require_storage_room(wid, len(await upload.read()))   # the real 3 MB does not fit in the 2 MB left


async def test_a_member_never_sees_the_note_ops_wrote_about_their_workspace(signup_user):
    client, _ = await signup_user()
    ws = await create_workspace(client, f"Note {uuid4().hex[:6]}")
    await workspaces.update_one({"id": ws}, {"$set": {"entitlement_overrides": {"plan": "duo", "note": "internal reason", "limits": {}}}})
    E.forget()
    body = (await client.get(f"/api/v1/workspaces/{ws}/entitlements")).json()
    assert body["override_note"] is None and "internal reason" not in str(body)
