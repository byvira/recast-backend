"""Tests for GET /api/v1/analytics/posts including real post content and
published_at.

PostMetrics (app.pipelines.analytics.base) has no content or published_at
field at all — post_id is really the originating piece_id (every
analytics fetcher sets post_id=piece_id) — so the Performance page's "Top
Posts" table always rendered an empty '""' where the post text should be,
and its expanded detail's publish date never showed. Fixed by joining
against content_pieces at read time.
"""

from datetime import datetime, timezone
from uuid import uuid4

from app.db.mongo import content_pieces, post_metrics
from tests.conftest import create_workspace


async def _seed_piece_with_metrics(ws_id: str, user_id: str, content: str) -> str:
    piece_id = str(uuid4())
    await content_pieces.insert_one({
        "piece_id": piece_id, "workspace_id": ws_id, "user_id": user_id,
        "brand_id": str(uuid4()), "platform": "LinkedIn", "content": content,
        "publish_status": "published", "deleted": False,
        "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc),
    })
    await post_metrics.insert_one({
        "workspace_id": ws_id, "platform": "linkedin", "post_id": piece_id,
        "platform_post_id": "urn:li:share:123", "likes": 5, "comments": 2,
        "engagement_rate": 3.5, "fetched_at": datetime.now(timezone.utc),
    })
    return piece_id


async def test_posts_endpoint_includes_real_content(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Analytics Posts WS")
    await _seed_piece_with_metrics(ws_id, profile["id"], "Real published post text.")

    res = await client.get("/api/v1/analytics/posts", headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 200, res.text
    body = res.json()
    assert len(body["metrics"]) == 1
    assert body["metrics"][0]["content"] == "Real published post text."
    assert body["metrics"][0]["published_at"] is not None


async def test_posts_endpoint_content_empty_string_when_piece_missing(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Analytics Posts WS 2")
    await post_metrics.insert_one({
        "workspace_id": ws_id, "platform": "linkedin", "post_id": "orphaned-piece-id",
        "platform_post_id": "urn:li:share:999", "likes": 0, "comments": 0,
        "engagement_rate": 0.0, "fetched_at": datetime.now(timezone.utc),
    })

    res = await client.get("/api/v1/analytics/posts", headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 200, res.text
    assert res.json()["metrics"][0]["content"] == ""
    assert res.json()["metrics"][0]["published_at"] is None


async def test_posts_endpoint_published_at_null_when_not_yet_published(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Analytics Posts WS 3")
    piece_id = str(uuid4())
    await content_pieces.insert_one({
        "piece_id": piece_id, "workspace_id": ws_id, "user_id": profile["id"],
        "brand_id": str(uuid4()), "platform": "LinkedIn", "content": "Draft, not published.",
        "publish_status": "pending", "deleted": False,
        "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc),
    })
    await post_metrics.insert_one({
        "workspace_id": ws_id, "platform": "linkedin", "post_id": piece_id,
        "platform_post_id": "urn:li:share:456", "likes": 0, "comments": 0,
        "engagement_rate": 0.0, "fetched_at": datetime.now(timezone.utc),
    })

    res = await client.get("/api/v1/analytics/posts", headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 200, res.text
    assert res.json()["metrics"][0]["published_at"] is None
