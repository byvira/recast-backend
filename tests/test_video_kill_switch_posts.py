"""The owner's kill switch holds scheduled posts that carry a video; text and picture posts and other workspaces carry on."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.db.mongo import content_pieces, get_db
from app.workers import scheduled_posts


async def _due_piece(ws: str, asset_type: str) -> str:
    pid = uuid4().hex
    await content_pieces.insert_one({
        "piece_id": pid, "workspace_id": ws, "platform": "LinkedIn", "deleted": False, "publish_status": "queued",
        "publish_scheduled_at": datetime.now(timezone.utc) - timedelta(minutes=2), "attachments": [{"asset_type": asset_type, "asset_id": "a"}],
    })
    return pid


async def test_a_paused_workspace_keeps_its_scheduled_video_posts_and_releases_them_after(monkeypatch):
    held_ws, other_ws = f"ws-{uuid4()}", f"ws-{uuid4()}"
    await get_db()["workspaces"].insert_one({"id": held_ws, "generation_halted": True})
    video, picture, elsewhere = await _due_piece(held_ws, "project"), await _due_piece(held_ws, "image"), await _due_piece(other_ws, "project")
    claimed: list[str] = []

    async def record(piece_id, now):
        claimed.append(piece_id)
        return None

    monkeypatch.setattr(scheduled_posts, "_claim_due_piece", record)

    await scheduled_posts.process_scheduled_posts()
    assert video not in claimed and picture in claimed and elsewhere in claimed

    await get_db()["workspaces"].update_one({"id": held_ws}, {"$set": {"generation_halted": False}})
    await scheduled_posts.process_scheduled_posts()
    assert video in claimed
