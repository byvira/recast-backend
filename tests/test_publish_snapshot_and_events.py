"""What went out is kept as it was sent, and a live-saved post is announced once."""
from datetime import datetime, timezone
from uuid import uuid4

from app.api.v1.publish import _update_piece_status
from app.db.mongo import content_pieces
from app.pipelines.text import events


async def _piece(ws_id: str, content: str = "The full text that was written.", version: int = 3) -> str:
    piece_id = str(uuid4())
    await content_pieces.insert_one({
        "piece_id": piece_id, "workspace_id": ws_id, "user_id": "u1", "platform": "Twitter/X", "content": content,
        "version_count": version, "publish_status": "scheduled", "deleted": False,
        "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc),
    })
    return piece_id


async def test_the_text_that_went_out_is_kept_beside_the_full_text():
    ws_id = f"ws-{uuid4()}"
    piece_id = await _piece(ws_id)

    await _update_piece_status(piece_id, ws_id, "published", published_content="The text, trimmed.", published_version=3)

    piece = await content_pieces.find_one({"piece_id": piece_id})
    assert piece["published_content"] == "The text, trimmed."
    assert piece["published_version"] == 3
    assert piece["content"] == "The full text that was written."     # the piece keeps what the member wrote
    assert piece["published_at"] is not None


async def test_a_later_edit_never_rewrites_what_was_published():
    ws_id = f"ws-{uuid4()}"
    piece_id = await _piece(ws_id)
    await _update_piece_status(piece_id, ws_id, "published", published_content="As sent.", published_version=3)

    await content_pieces.update_one({"piece_id": piece_id}, {"$set": {"content": "Edited afterwards.", "version_count": 4}})

    piece = await content_pieces.find_one({"piece_id": piece_id})
    assert piece["published_content"] == "As sent."
    assert piece["published_version"] == 3
    assert piece["content"] == "Edited afterwards."


async def test_a_failed_or_queued_status_writes_no_published_snapshot():
    ws_id = f"ws-{uuid4()}"
    piece_id = await _piece(ws_id)
    await _update_piece_status(piece_id, ws_id, "failed", error_message="Rejected", published_content="Nope", published_version=3)

    piece = await content_pieces.find_one({"piece_id": piece_id})
    assert piece["publish_status"] == "failed"
    assert "published_content" not in piece and "published_version" not in piece
    assert piece["last_error"] == "Rejected"


async def test_a_live_saved_post_is_announced_with_a_key_that_stops_a_second_announcement(monkeypatch):
    seen: list[dict] = []

    async def _emit(**kwargs):
        seen.append(kwargs)

    async def _role(workspace_id, user_id):
        return "owner"

    monkeypatch.setattr(events, "emit_event", _emit)
    monkeypatch.setattr(events, "_actor_role", _role)

    doc = {"piece_id": "p1", "workspace_id": "w1", "user_id": "u1", "platform": "LinkedIn", "content": "Hello there.",
           "word_count": 2, "brand_id": "b1", "session_id": "s1"}
    await events._emit_live_piece_created(doc)

    [event] = seen
    assert event["idempotency_key"] == "content.created:p1"
    assert event["workspace_id"] == "w1" and event["actor_user_id"] == "u1" and event["actor_role"] == "owner"
    assert event["payload"].content_id == "p1" and event["payload"].target == "LinkedIn"


def test_announcing_without_a_workspace_or_member_does_nothing_and_never_raises():
    events.emit_live_piece_created({"piece_id": "p1"})
    events.emit_live_piece_created({"piece_id": "p1", "workspace_id": "w1"})
