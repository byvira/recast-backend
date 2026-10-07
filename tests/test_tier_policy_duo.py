from uuid import uuid4

from app.agents.supervisor.rules import evaluate_rules
from app.db.mongo import workspace_members, workspaces
from app.pipelines.text.storage import ensure_session_exists, save_live_piece
from app.shared.tier_policy import policy_for
from tests.conftest import create_workspace, invite_and_accept, signup_new_user


def test_duo_keeps_team_rules_but_not_cohorts_or_the_signal_storm_check():
    duo = policy_for({"tier": "duo"})
    assert duo["team_rules"] is True and duo["cross_creator"] is True and duo["cohorts"] is False
    assert duo["hidden_rules"] == ["assistant_signal_storm"]
    assert policy_for({"tier": "large"})["hidden_rules"] == []
    assert "tier_seat_exceeded" in policy_for({"tier": "single"})["hidden_rules"]


def test_no_self_approval_applies_only_to_duo_with_a_review_step():
    assert policy_for({"tier": "duo"})["no_self_approval"] is False  # no review step by default
    assert policy_for({"tier": "duo", "require_review": True})["no_self_approval"] is True
    assert policy_for({"tier": "large"})["no_self_approval"] is False  # large behaves exactly as before
    assert policy_for({"tier": "single", "require_review": True})["no_self_approval"] is False


async def _post(ws: str, author: str) -> str:
    session = str(uuid4())
    await ensure_session_exists(session_id=session, workspace_id=ws, user_id=author, brand_id="b", source_type="text")
    return await save_live_piece(
        session_id=session, workspace_id=ws, user_id=author, brand_id="b", platform="LinkedIn",
        content="Hello", word_count=1, char_count=5,
    )


async def test_in_a_duo_with_review_you_cannot_approve_your_own_post_when_a_partner_can(api_client, make_client):
    owner = await signup_new_user(api_client)
    ws = await create_workspace(api_client, "Duo review", tier="duo")
    partner_client, partner = await invite_and_accept(api_client, make_client, ws, "admin")
    assert (await api_client.patch(f"/api/v1/workspaces/{ws}", json={"require_review": True})).status_code == 200
    headers = {"X-Workspace-Id": ws}

    mine = await _post(ws, owner["id"])
    blocked = await api_client.patch(f"/api/v1/content/pieces/{mine}/approve", headers=headers)
    assert blocked.status_code == 403 and "Someone else" in blocked.json()["detail"]
    # the partner can approve it
    assert (await partner_client.patch(f"/api/v1/content/pieces/{mine}/approve", headers=headers)).status_code == 200


async def test_the_only_approver_is_never_left_stuck(api_client, make_client):
    owner = await signup_new_user(api_client)
    ws = await create_workspace(api_client, "Duo editor", tier="duo")
    await invite_and_accept(api_client, make_client, ws, "editor")  # an editor cannot approve
    await api_client.patch(f"/api/v1/workspaces/{ws}", json={"require_review": True})
    mine = await _post(ws, owner["id"])
    ok = await api_client.patch(f"/api/v1/content/pieces/{mine}/approve", headers={"X-Workspace-Id": ws})
    assert ok.status_code == 200


async def test_a_duo_does_not_get_the_signal_storm_rule(api_client):
    await signup_new_user(api_client)
    ws = await create_workspace(api_client, "Duo rules", tier="duo")
    await workspaces.update_one({"id": ws}, {"$set": {"tier_config.seats": 1}})
    for _ in range(3):
        await workspace_members.insert_one({"workspace_id": ws, "user_id": str(uuid4()), "status": "active", "role": "editor"})
    flags = {f["flag_type"] for f in await evaluate_rules(ws)}
    assert "tier_seat_exceeded" in flags  # duo keeps the seat limit
    assert "assistant_signal_storm" not in flags


async def test_turning_review_off_can_let_the_waiting_posts_through(api_client):
    owner = await signup_new_user(api_client)
    ws = await create_workspace(api_client, "Let through", tier="large")
    headers = {"X-Workspace-Id": ws}
    waiting = [await _post(ws, owner["id"]) for _ in range(3)]
    from app.pipelines.text.storage import get_piece

    assert all((await get_piece(p, ws))["approval_status"] == "pending" for p in waiting)
    done = await api_client.post("/api/v1/content/approve-waiting", headers=headers)
    assert done.status_code == 200, done.text
    assert done.json()["approved_count"] == 3
    assert all((await get_piece(p, ws))["approval_status"] == "approved" for p in waiting)
    assert (await api_client.post("/api/v1/content/approve-waiting", headers=headers)).json()["approved_count"] == 0


async def test_old_alerts_for_rules_a_plan_does_not_use_are_not_shown(api_client):
    from datetime import datetime, timezone

    from app.db.mongo import activity_entries

    await signup_new_user(api_client)
    solo = await create_workspace(api_client, "Solo log", tier="single")
    team = await create_workspace(api_client, "Team log", tier="large")
    now = datetime.now(timezone.utc)
    for ws in (solo, team):
        await activity_entries.insert_one({
            "_id": f"seat-{ws}", "workspace_id": ws, "lane": "passive", "visibility": "admins", "status": "warning",
            "source": {"kind": "odette_flag", "id": "f1", "type": "tier_seat_exceeded"},
            "actor": {"type": "ai_agent", "name": "Odette"}, "category": "workspace_alert",
            "title": "Seats over plan limit", "description": "", "occurred_at": now, "search_text": "seats",
        })
    for ws, expected in ((solo, 0), (team, 1)):
        listed = (await api_client.get("/api/v1/activity", params={"lane": "passive"}, headers={"X-Workspace-Id": ws})).json()
        assert len(listed["items"]) == expected, (ws, listed["items"])
