"""Old posts left waiting in a workspace with no review step are approved once, never queued or sent; review workspaces are untouched."""

from uuid import uuid4

from app.db import migrations
from app.db.mongo import content_pieces, get_db, workspaces


async def test_waiting_posts_are_approved_only_where_there_is_no_review_step():
    solo, team = "ws-solo-" + uuid4().hex[:6], "ws-team-" + uuid4().hex[:6]
    await workspaces.insert_many([{"id": solo, "tier": "single"}, {"id": team, "tier": "large"}])
    ids = {k: uuid4().hex for k in ("solo_wait", "solo_rejected", "solo_published", "team_wait")}
    await content_pieces.insert_many([
        {"piece_id": ids["solo_wait"], "workspace_id": solo, "approval_status": "pending", "publish_status": "draft"},
        {"piece_id": ids["solo_rejected"], "workspace_id": solo, "approval_status": "rejected"},
        {"piece_id": ids["solo_published"], "workspace_id": solo, "approval_status": "pending", "publish_status": "published"},
        {"piece_id": ids["team_wait"], "workspace_id": team, "approval_status": "pending", "publish_status": "draft"},
    ])
    db = get_db()
    await db["migrations"].delete_one({"_id": "2026-10-26_approve_waiting_posts_without_review"})
    try:
        await migrations._approve_waiting_posts_without_review(db)
        status = {k: (await content_pieces.find_one({"piece_id": v}))["approval_status"] for k, v in ids.items()}
        assert status == {"solo_wait": "approved", "solo_rejected": "rejected", "solo_published": "pending", "team_wait": "pending"}
        assert (await content_pieces.find_one({"piece_id": ids["solo_wait"]})).get("publish_status") == "draft"  # not queued
    finally:
        await content_pieces.delete_many({"piece_id": {"$in": list(ids.values())}})
        await workspaces.delete_many({"id": {"$in": [solo, team]}})
