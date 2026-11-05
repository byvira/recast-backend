"""Groups of people in a workspace: the owner chooses who is in each, only the workspace's own people can be chosen, a person is
listed once and a name is used once."""
from __future__ import annotations

from uuid import uuid4

from tests.conftest import create_workspace, invite_and_accept

URL = "/api/v1/ops/cohorts"


async def test_the_owner_makes_a_group_and_chooses_who_is_in_it(signup_user, make_client):
    owner, profile = await signup_user("Owner")
    ws = await create_workspace(owner, f"Groups {uuid4().hex[:6]}")
    head = {"X-Workspace-Id": ws}
    _, member = await invite_and_accept(owner, make_client, ws, "editor")

    made = await owner.post(URL, json={"name": "  Marketing  ", "member_user_ids": [member["id"], member["id"]]}, headers=head)
    assert made.status_code == 200, made.text
    group = made.json()
    assert group["name"] == "Marketing" and group["member_user_ids"] == [member["id"]], "trimmed, and each person once"

    changed = await owner.put(f"{URL}/{group['id']}", json={"name": "Marketing", "member_user_ids": [profile["id"], member["id"]]}, headers=head)
    assert changed.status_code == 200 and set(changed.json()["member_user_ids"]) == {profile["id"], member["id"]}
    listed = (await owner.get(URL, headers=head)).json()
    assert listed["total"] == 1 and len(listed["cohorts"][0]["member_user_ids"]) == 2


async def test_someone_outside_the_workspace_cannot_be_put_in_a_group(signup_user):
    owner, _ = await signup_user("Owner")
    stranger, stranger_profile = await signup_user("Stranger")
    ws = await create_workspace(owner, f"Groups {uuid4().hex[:6]}")
    head = {"X-Workspace-Id": ws}
    refused = await owner.post(URL, json={"name": "Sneaky", "member_user_ids": [stranger_profile["id"]]}, headers=head)
    assert refused.status_code == 400 and "not in this workspace" in refused.json()["detail"]
    assert (await owner.get(URL, headers=head)).json()["total"] == 0


async def test_a_group_name_is_needed_and_used_once(signup_user):
    owner, _ = await signup_user("Owner")
    ws = await create_workspace(owner, f"Groups {uuid4().hex[:6]}")
    head = {"X-Workspace-Id": ws}
    assert (await owner.post(URL, json={"name": "   ", "member_user_ids": []}, headers=head)).status_code == 400
    first = (await owner.post(URL, json={"name": "Sales", "member_user_ids": []}, headers=head)).json()
    twice = await owner.post(URL, json={"name": "sales", "member_user_ids": []}, headers=head)
    assert twice.status_code == 409 and "already exists" in twice.json()["detail"]
    other = (await owner.post(URL, json={"name": "Support", "member_user_ids": []}, headers=head)).json()
    # renaming one to the other's name is refused, keeping its own name is fine
    assert (await owner.put(f"{URL}/{other['id']}", json={"name": "SALES", "member_user_ids": []}, headers=head)).status_code == 409
    assert (await owner.put(f"{URL}/{first['id']}", json={"name": "Sales", "member_user_ids": []}, headers=head)).status_code == 200


async def test_only_the_owner_manages_groups(signup_user, make_client):
    owner, _ = await signup_user("Owner")
    ws = await create_workspace(owner, f"Groups {uuid4().hex[:6]}")
    head = {"X-Workspace-Id": ws}
    editor, _ = await invite_and_accept(owner, make_client, ws, "editor")
    assert (await editor.post(URL, json={"name": "Mine", "member_user_ids": []}, headers=head)).status_code == 403
    assert (await editor.get(URL, headers=head)).status_code == 403


def test_a_group_is_scored_only_on_what_there_is_enough_posts_to_judge():
    from app.shared.group_consistency import MIN_POSTS, score_group

    thin = score_group([{"quality_passed": True}, {"quality_passed": False}])
    assert thin["posts"] == 2 and all(a["percent"] is None for a in thin["axes"]), f"fewer than {MIN_POSTS} posts is never scored"
    posts = (
        [{"quality_passed": True, "flagged_for_review": False, "approval_status": "approved"}] * 6
        + [{"quality_passed": False, "flagged_for_review": True, "approval_status": "rejected"}] * 2
        + [{"approval_status": "pending"}] * 2
    )
    got = {a["key"]: a["percent"] for a in score_group(posts)["axes"]}
    assert got == {"quality": 80, "safety": 80, "approval": 75}, "pending posts count toward quality and safety but not approval"
    undecided = {a["key"]: a["percent"] for a in score_group([{"approval_status": "pending"}] * 5)["axes"]}
    assert undecided["approval"] is None and undecided["quality"] == 100


async def test_the_consistency_of_each_group_comes_from_its_members_posts(signup_user, make_client):
    from datetime import datetime, timedelta, timezone

    from app.db.mongo import content_pieces

    owner, profile = await signup_user("Owner")
    ws = await create_workspace(owner, f"Radar {uuid4().hex[:6]}")
    head = {"X-Workspace-Id": ws}
    _, member = await invite_and_accept(owner, make_client, ws, "editor")
    mine = (await owner.post(URL, json={"name": "Marketing", "member_user_ids": [member["id"]]}, headers=head)).json()
    await owner.post(URL, json={"name": "Empty", "member_user_ids": []}, headers=head)
    now = datetime.now(timezone.utc)
    rows = [{"piece_id": uuid4().hex, "workspace_id": ws, "user_id": member["id"], "created_at": now - timedelta(days=2), "quality_passed": i != 0,
             "flagged_for_review": False, "approval_status": "approved"} for i in range(4)]
    rows += [{"piece_id": uuid4().hex, "workspace_id": ws, "user_id": member["id"], "created_at": now - timedelta(days=90), "quality_passed": False,
              "flagged_for_review": True, "approval_status": "rejected"}]  # too old to count
    rows += [{"piece_id": uuid4().hex, "workspace_id": ws, "user_id": profile["id"], "created_at": now, "quality_passed": False,
              "flagged_for_review": True, "approval_status": "rejected"} for _ in range(3)]  # someone who is not in the group
    await content_pieces.insert_many(rows)

    res = await owner.get(f"{URL}/consistency", headers=head)
    assert res.status_code == 200, res.text
    body = res.json()
    marketing = next(g for g in body["groups"] if g["id"] == mine["id"])
    empty = next(g for g in body["groups"] if g["name"] == "Empty")
    assert body["days"] == 30 and marketing["people"] == 1 and marketing["posts"] == 4, "only this group's people and this month's posts"
    assert {a["key"]: a["percent"] for a in marketing["axes"]} == {"quality": 75, "safety": 100, "approval": 100}
    assert empty["posts"] == 0 and all(a["percent"] is None for a in empty["axes"])
