"""Results typed in by hand for posts Recast cannot read results from are saved, labelled self-reported, and counted in the totals."""
from datetime import datetime, timezone
from uuid import uuid4

from app.db.mongo import content_pieces, get_db
from tests.conftest import create_workspace


async def _piece(ws_id: str, *, platform: str = "Blog", status: str = "published") -> str:
    piece_id = f"p-{uuid4()}"
    await content_pieces.insert_one({
        "piece_id": piece_id, "workspace_id": ws_id, "platform": platform, "publish_status": status, "deleted": False,
        "content": "A post.", "published_at": datetime.now(timezone.utc),
    })
    return piece_id


async def test_a_member_can_enter_results_for_a_published_post_and_they_are_marked_self_reported(signup_user):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Self Reported WS")
    piece_id = await _piece(ws_id)

    res = await client.put(f"/api/v1/analytics/posts/{piece_id}/self-reported", json={"views": 420, "clicks": 31},
                           headers={"X-Workspace-Id": ws_id})

    assert res.status_code == 200, res.text
    stored = await get_db()["post_metrics"].find_one({"workspace_id": ws_id, "post_id": piece_id})
    assert stored["source"] == "self_reported" and stored["views"] == 420 and stored["clicks"] == 31
    assert stored["impressions"] == 420 and stored["platform"] == "blog"


async def test_entering_results_again_replaces_the_typed_figures_instead_of_adding_a_second_record(signup_user):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Self Reported Twice WS")
    piece_id = await _piece(ws_id)
    headers = {"X-Workspace-Id": ws_id}

    await client.put(f"/api/v1/analytics/posts/{piece_id}/self-reported", json={"views": 10}, headers=headers)
    await client.put(f"/api/v1/analytics/posts/{piece_id}/self-reported", json={"views": 99}, headers=headers)

    rows = await get_db()["post_metrics"].find({"workspace_id": ws_id, "post_id": piece_id}).to_list(length=5)
    assert len(rows) == 1 and rows[0]["views"] == 99


async def test_results_cannot_be_entered_for_an_unpublished_or_unknown_post_or_with_negative_numbers(signup_user):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Self Reported Guard WS")
    headers = {"X-Workspace-Id": ws_id}
    draft = await _piece(ws_id, status="draft")
    published = await _piece(ws_id)

    assert (await client.put(f"/api/v1/analytics/posts/{draft}/self-reported", json={"views": 1}, headers=headers)).status_code == 400
    assert (await client.put("/api/v1/analytics/posts/nope/self-reported", json={"views": 1}, headers=headers)).status_code == 404
    assert (await client.put(f"/api/v1/analytics/posts/{published}/self-reported", json={"views": -5}, headers=headers)).status_code == 422


async def test_another_workspaces_post_cannot_be_given_results(signup_user):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Self Reported Foreign WS")
    foreign = await _piece(f"other-{uuid4()}")
    res = await client.put(f"/api/v1/analytics/posts/{foreign}/self-reported", json={"views": 1}, headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 404


async def test_the_overview_still_loads_and_counts_typed_in_results(signup_user):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Self Reported Totals WS")
    headers = {"X-Workspace-Id": ws_id}
    piece_id = await _piece(ws_id)
    await client.put(f"/api/v1/analytics/posts/{piece_id}/self-reported", json={"views": 250, "likes": 7}, headers=headers)

    res = await client.get("/api/v1/analytics/summary", headers=headers)

    assert res.status_code == 200, res.text
