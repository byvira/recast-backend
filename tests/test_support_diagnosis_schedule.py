"""The support snapshot and diagnosis explain "why did my post not go out" and "why is my campaign paused" from status, error
codes and times, never from the text of a post."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.db.mongo import content_pieces, get_campaigns_collection
from app.shared import support_ai, support_context

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731


def _ticket(ws_id: str, **extra) -> dict:
    return {"workspace_id": ws_id, "created_by": "u1", "created_by_name": "Asha", "category": "Publishing", **extra}


async def _piece(ws_id: str, **fields) -> str:
    piece_id = f"p-{uuid4()}"
    await content_pieces.insert_one({
        "piece_id": piece_id, "workspace_id": ws_id, "platform": "LinkedIn", "deleted": False,
        "content": "PRIVATE TEXT OF THE POST", "updated_at": NOW(), **fields,
    })
    return piece_id


async def test_overdue_held_and_failed_posts_are_counted_without_any_post_text():
    ws_id = f"ws-{uuid4()}"
    await _piece(ws_id, publish_status="queued", publish_scheduled_at=NOW() - timedelta(hours=2))       # overdue
    await _piece(ws_id, publish_status="queued", publish_scheduled_at=NOW() + timedelta(hours=2))       # waiting, fine
    await _piece(ws_id, publish_status="queued", publish_scheduled_at=NOW() - timedelta(hours=3),
                 hold={"reason": "platform_paused", "platform_key": "linkedin"})                         # held, not overdue
    await _piece(ws_id, publish_status="failed", last_error="The LinkedIn login expired.")

    snapshot = await support_context.build_snapshot(_ticket(ws_id))

    schedule = snapshot["schedule"]
    assert schedule["queued"] == 3 and schedule["overdue"] == 1
    assert schedule["held"] == {"platform_paused": 1}
    assert schedule["recent_failed"][0]["error"] == "The LinkedIn login expired."
    assert "PRIVATE TEXT" not in repr(snapshot)


async def test_the_diagnosis_says_what_is_overdue_held_and_failed():
    ws_id = f"ws-{uuid4()}"
    await _piece(ws_id, publish_status="queued", publish_scheduled_at=NOW() - timedelta(hours=2))
    await _piece(ws_id, publish_status="queued", hold={"reason": "platform_paused"})
    await _piece(ws_id, publish_status="failed", last_error="Rejected by the platform.")
    ticket = _ticket(ws_id)
    snapshot = await support_context.build_snapshot(ticket)

    findings = await support_ai.diagnose({**ticket, "workspace_id": ws_id}, snapshot)

    text = " ".join(f["text"] for f in findings)
    assert "1 scheduled post is overdue" in text
    assert "on hold because Recast has paused that platform" in text
    assert "A LinkedIn post failed in the last 7 days: Rejected by the platform." in text
    assert "PRIVATE TEXT" not in text


async def test_a_paused_campaign_is_named_and_the_one_the_member_reported_is_a_problem():
    ws_id = f"ws-{uuid4()}"
    reported = f"c-{uuid4()}"
    other = f"c-{uuid4()}"
    campaigns = get_campaigns_collection()
    await campaigns.insert_one({"id": reported, "workspace_id": ws_id, "name": "Spring launch", "status": "paused",
                                "cadence": {"frequency": "weekly"}, "updated_at": NOW()})
    await campaigns.insert_one({"id": other, "workspace_id": ws_id, "name": "Autumn push", "status": "active",
                                "cadence": {"frequency": "weekly"}, "updated_at": NOW()})
    ticket = _ticket(ws_id, category="Campaigns", source_context={"type": "campaign", "id": reported})

    snapshot = await support_context.build_snapshot(ticket)
    findings = await support_ai.diagnose(ticket, snapshot)

    assert snapshot["linked_object"]["type"] == "campaign" and snapshot["linked_object"]["status"] == "paused"
    paused = [f for f in findings if "is paused" in f["text"]]
    assert len(paused) == 1                                          # the active one is not listed as paused
    assert "Spring launch" in paused[0]["text"] and "the one the member reported" in paused[0]["text"]
    assert paused[0]["level"] == "problem"


async def test_a_workspace_with_nothing_wrong_gets_the_nothing_obvious_note():
    ws_id = f"ws-{uuid4()}"
    ticket = _ticket(ws_id, category="Other")
    snapshot = await support_context.build_snapshot(ticket)
    findings = await support_ai.diagnose(ticket, snapshot)
    assert [f["text"] for f in findings][-1].startswith("Nothing obvious stands out")
