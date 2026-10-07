from uuid import uuid4

from app.agents.supervisor.rules import evaluate_rules
from app.db.mongo import workspace_members, workspaces
from app.pipelines.text.storage import ensure_session_exists, get_piece, save_live_piece
from app.shared.tier_policy import policy_for
from tests.conftest import create_workspace, signup_new_user


def test_each_plan_has_its_own_defaults():
    single, duo, large = (policy_for({"tier": t}) for t in ("single", "duo", "large"))
    assert (single["review_required"], single["team_rules"], single["cross_creator"]) == (False, False, False)
    assert (duo["review_required"], duo["team_rules"], duo["cross_creator"]) == (False, True, True)
    assert (large["review_required"], large["team_rules"], large["cross_creator"]) == (True, True, True)


def test_the_members_own_choice_wins_over_the_plan():
    assert policy_for({"tier": "single", "require_review": True})["review_required"] is True
    assert policy_for({"tier": "large", "require_review": False})["review_required"] is False
    assert policy_for({"tier": "large", "require_review": None})["review_is_default"] is True
    assert policy_for({"tier": "mystery"})["team_rules"] is True  # an unknown plan keeps everything on


async def _save(ws: str, user: str) -> dict:
    session = str(uuid4())
    await ensure_session_exists(session_id=session, workspace_id=ws, user_id=user, brand_id="b", source_type="text")
    piece_id = await save_live_piece(
        session_id=session, workspace_id=ws, user_id=user, brand_id="b", platform="LinkedIn",
        content="Hello world", word_count=2, char_count=11,
    )
    return await get_piece(piece_id, ws)


async def test_posts_start_approved_without_a_review_step_and_pending_with_one(api_client):
    await signup_new_user(api_client)
    solo = await create_workspace(api_client, "Solo", tier="single")
    team = await create_workspace(api_client, "Team", tier="large")

    assert (await _save(solo, "u"))["approval_status"] == "approved"
    assert (await _save(team, "u"))["approval_status"] == "pending"

    # the owner can switch the review step on for a solo workspace, and off for a team
    on = await api_client.patch(f"/api/v1/workspaces/{solo}", json={"require_review": True})
    assert on.status_code == 200, on.text
    assert on.json()["policy"]["review_required"] is True
    assert (await _save(solo, "u"))["approval_status"] == "pending"
    await api_client.patch(f"/api/v1/workspaces/{team}", json={"require_review": False})
    assert (await _save(team, "u"))["approval_status"] == "approved"


async def test_team_only_odette_rules_are_left_out_for_a_workspace_of_one(api_client):
    await signup_new_user(api_client)
    solo = await create_workspace(api_client, "Solo rules", tier="single")
    team = await create_workspace(api_client, "Team rules", tier="large")
    for ws in (solo, team):
        await workspaces.update_one({"id": ws}, {"$set": {"tier_config.seats": 1}})
        for _ in range(3):
            await workspace_members.insert_one({"workspace_id": ws, "user_id": str(uuid4()), "status": "active", "role": "editor"})

    solo_flags = {f["flag_type"] for f in await evaluate_rules(solo)}
    team_flags = {f["flag_type"] for f in await evaluate_rules(team)}
    assert "tier_seat_exceeded" not in solo_flags
    assert "tier_seat_exceeded" in team_flags
