"""Results for pictures and recordings are ranked within their own kind, and top performers can be read per medium."""
from datetime import datetime, timezone
from uuid import uuid4

from app.db.mongo import audio_assets, image_assets, post_metric_checkpoints, post_metrics
from tests.conftest import create_workspace


def _h(ws: str) -> dict:
    return {"X-Workspace-Id": ws}


async def _asset(collection, ws: str, title: str, pieces: list[str]) -> str:
    asset_id = str(uuid4())
    await collection.insert_one({
        "id": asset_id, "workspace_id": ws, "title": title, "deleted": False,
        "linked_pieces": [{"piece_id": p} for p in pieces],
    })
    return asset_id


async def _metrics(ws: str, piece_id: str, rate: float, likes: int = 0, impressions: int = 100) -> None:
    await post_metrics.insert_one({
        "workspace_id": ws, "platform": "linkedin", "post_id": piece_id, "platform_post_id": f"urn:li:share:{piece_id}",
        "likes": likes, "comments": 0,
        "impressions": impressions, "engagement_rate": rate, "fetched_at": datetime.now(timezone.utc),
    })


async def test_pictures_are_ranked_best_first_and_unmeasured_ones_are_left_out(signup_user):
    client, _ = await signup_user()
    ws = await create_workspace(client, "Asset ranking")
    a, b, c = str(uuid4()), str(uuid4()), str(uuid4())
    await _metrics(ws, a, 2.0, likes=10)
    await _metrics(ws, b, 6.5, likes=40)
    best = await _asset(image_assets, ws, "Sunrise desk", [b])
    middle = await _asset(image_assets, ws, "Team photo", [a])
    await _asset(image_assets, ws, "Never measured", [c])
    res = await client.get("/api/v1/analytics/assets/image/top", headers=_h(ws))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["measured"] == 2
    assert [i["asset_id"] for i in body["items"]] == [best, middle]
    assert body["items"][0]["rank"] == 1 and body["items"][0]["average_engagement_rate"] == 6.5
    assert body["items"][0]["engagement"] == 40


async def test_an_asset_used_in_several_posts_is_averaged_across_them(signup_user):
    client, _ = await signup_user()
    ws = await create_workspace(client, "Asset average")
    p1, p2 = str(uuid4()), str(uuid4())
    await _metrics(ws, p1, 2.0)
    await _metrics(ws, p2, 4.0)
    only = await _asset(audio_assets, ws, "Episode 3", [p1, p2])
    body = (await client.get("/api/v1/analytics/assets/audio/top", headers=_h(ws))).json()
    assert body["items"][0]["asset_id"] == only
    assert body["items"][0]["measured_posts"] == 2 and body["items"][0]["average_engagement_rate"] == 3.0


async def test_one_workspace_never_sees_another_workspaces_assets(signup_user):
    client, _ = await signup_user()
    mine = await create_workspace(client, "Mine")
    other = str(uuid4())
    piece = str(uuid4())
    await _metrics(other, piece, 9.0)
    await _asset(image_assets, other, "Not yours", [piece])
    body = (await client.get("/api/v1/analytics/assets/image/top", headers=_h(mine))).json()
    assert body == {"asset_type": "image", "measured": 0, "items": []}


async def test_an_unknown_kind_is_refused_plainly(signup_user):
    client, _ = await signup_user()
    ws = await create_workspace(client, "Kind check")
    res = await client.get("/api/v1/analytics/assets/video/top", headers=_h(ws))
    assert res.status_code == 404 and "pictures" in res.json()["detail"]


async def test_top_performers_can_be_read_within_one_medium(signup_user):
    client, _ = await signup_user()
    ws = await create_workspace(client, "Top by medium")
    for piece, kind, rate in ((str(uuid4()), "text", 5.0), (str(uuid4()), "image", 8.0), (str(uuid4()), "audio", 7.0)):
        await post_metric_checkpoints.insert_one({
            "workspace_id": ws, "piece_id": piece, "checkpoint": "24h", "pipeline_type": kind, "platform": "linkedin",
            "metrics": {"engagement_rate": rate},
        })
    everything = (await client.get("/api/v1/analytics/top-performers", headers=_h(ws))).json()
    only_images = (await client.get("/api/v1/analytics/top-performers?pipeline=image", headers=_h(ws))).json()
    assert everything["measured"] == 3 and only_images["measured"] == 1
    assert only_images["items"][0]["engagement_rate"] == 8.0
