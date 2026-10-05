"""A picture's or recording's results are the added-up results of the posts it is attached to."""
from datetime import datetime, timezone
from uuid import uuid4

from app.db.mongo import audio_assets, content_pieces, get_db, image_assets
from tests.conftest import create_workspace


async def _asset_with_posts(collection, ws_id: str, posts: list[tuple[str, dict | None]]) -> str:
    asset_id = str(uuid4())
    links = []
    for name, metrics in posts:
        piece_id = f"{name}-{uuid4()}"
        await content_pieces.insert_one({
            "piece_id": piece_id, "workspace_id": ws_id, "platform": "LinkedIn", "publish_status": "published" if metrics else "scheduled",
            "platform_post_url": f"https://example.com/{name}", "content": f"Post {name}", "deleted": False,
            "published_at": datetime.now(timezone.utc),
        })
        if metrics:
            await get_db()["post_metrics"].insert_one({"workspace_id": ws_id, "post_id": piece_id, "platform": "linkedin", "platform_post_id": piece_id, **metrics})
        links.append({"piece_id": piece_id})
    await collection.insert_one({"id": asset_id, "workspace_id": ws_id, "linked_pieces": links})
    return asset_id


async def test_a_pictures_results_add_up_its_posts_and_skip_unmeasured_ones(signup_user):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Rollup WS")
    asset_id = await _asset_with_posts(image_assets, ws_id, [
        ("a", {"likes": 10, "comments": 2, "impressions": 500}),
        ("b", {"likes": 5, "shares": 1, "impressions": 250}),
        ("c", None),          # not published yet: listed, adds nothing
    ])

    res = await client.get(f"/api/v1/analytics/assets/image/{asset_id}", headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["post_count"] == 3 and body["measured_posts"] == 2
    assert body["totals"]["likes"] == 15 and body["totals"]["comments"] == 2 and body["totals"]["shares"] == 1
    assert body["totals"]["impressions"] == 750
    unmeasured = [p for p in body["posts"] if p["metrics"] is None]
    assert len(unmeasured) == 1 and unmeasured[0]["publish_status"] == "scheduled"


async def test_a_recordings_results_work_the_same_way(signup_user):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Rollup Audio WS")
    asset_id = await _asset_with_posts(audio_assets, ws_id, [("a", {"views": 40, "likes": 3})])

    res = await client.get(f"/api/v1/analytics/assets/audio/{asset_id}", headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 200, res.text
    assert res.json()["totals"]["views"] == 40 and res.json()["measured_posts"] == 1


async def test_an_unknown_kind_or_another_workspaces_asset_is_not_found(signup_user):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Rollup Guard WS")
    other_ws = f"ws-{uuid4()}"
    foreign = await _asset_with_posts(image_assets, other_ws, [("a", {"likes": 1})])

    assert (await client.get("/api/v1/analytics/assets/video/x", headers={"X-Workspace-Id": ws_id})).status_code == 404
    assert (await client.get(f"/api/v1/analytics/assets/image/{foreign}", headers={"X-Workspace-Id": ws_id})).status_code == 404
