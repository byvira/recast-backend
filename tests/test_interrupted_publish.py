"""A post stuck in "publishing" after a restart is marked failed and never retried by itself, because the platform may have posted it."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.db.mongo import content_pieces
from app.workers import scheduled_posts


async def _publishing(ws_id: str, *, started_minutes_ago: float) -> str:
    piece_id = f"pub-{uuid4()}"
    await content_pieces.insert_one({
        "piece_id": piece_id, "workspace_id": ws_id, "platform": "LinkedIn", "publish_status": "publishing",
        "publishing_started_at": datetime.now(timezone.utc) - timedelta(minutes=started_minutes_ago),
        "content": "Going out.", "deleted": False,
    })
    return piece_id


async def test_a_post_stuck_publishing_is_failed_with_a_message_that_says_to_check_the_platform():
    ws_id = f"ws-{uuid4()}"
    stuck = await _publishing(ws_id, started_minutes_ago=40)

    reaped = await scheduled_posts.reap_stuck_publishing()

    assert reaped >= 1
    piece = await content_pieces.find_one({"piece_id": stuck})
    assert piece["publish_status"] == "failed"
    assert piece["last_error"] == scheduled_posts.INTERRUPTED_MESSAGE
    assert "Check the platform before trying again" in piece["last_error"]


async def test_a_post_that_started_publishing_a_moment_ago_is_left_alone():
    ws_id = f"ws-{uuid4()}"
    running = await _publishing(ws_id, started_minutes_ago=2)

    await scheduled_posts.reap_stuck_publishing()

    assert (await content_pieces.find_one({"piece_id": running}))["publish_status"] == "publishing"


async def test_a_reaped_post_is_not_queued_again_by_the_worker():
    ws_id = f"ws-{uuid4()}"
    stuck = await _publishing(ws_id, started_minutes_ago=60)
    await scheduled_posts.reap_stuck_publishing()

    due = await content_pieces.count_documents({**scheduled_posts._due_filter(datetime.now(timezone.utc)), "piece_id": stuck})
    assert due == 0
