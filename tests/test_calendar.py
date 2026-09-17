"""Tests for GET /api/v1/analytics/calendar — Module 2 Stage 9's fix.

This endpoint used to read a "platform_results" array field that no write
path anywhere in the app ever populated (each content_pieces document is
already exactly one platform's content, not a multi-platform bundle), used
"id": piece.get("id", piece["_id"]) which always fell through to the raw
Mongo ObjectId since pieces only ever have "piece_id" (making every
returned item unusable for any real action), and filtered/summarized by
status values ("draft", "scheduled") that don't exist in the real
publish_status vocabulary (pending/queued/publishing/published/failed).
"""

from datetime import datetime, timezone
from uuid import uuid4

from app.pipelines.text.storage import ensure_session_exists, save_live_piece
from tests.conftest import create_workspace


async def _seed_piece(workspace_id: str, user_id: str, brand_id: str, platform: str = "LinkedIn") -> str:
    session_id = str(uuid4())
    await ensure_session_exists(
        session_id=session_id, workspace_id=workspace_id, user_id=user_id,
        brand_id=brand_id, source_type="text",
    )
    return await save_live_piece(
        session_id=session_id, workspace_id=workspace_id, user_id=user_id,
        brand_id=brand_id, platform=platform,
        content="Calendar test content.", word_count=3, char_count=25,
    )


async def test_calendar_returns_real_piece_id_not_object_id(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Calendar WS")
    piece_id = await _seed_piece(ws_id, profile["id"], str(uuid4()))

    now = datetime.now(timezone.utc)
    res = await client.get(
        f"/api/v1/analytics/calendar?year={now.year}&month={now.month}",
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    all_pieces = [p for day in body["days"].values() for p in day]
    assert len(all_pieces) == 1
    assert all_pieces[0]["id"] == piece_id


async def test_calendar_populates_platform_results_from_real_fields(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Calendar WS")
    await _seed_piece(ws_id, profile["id"], str(uuid4()), platform="Instagram")

    now = datetime.now(timezone.utc)
    res = await client.get(
        f"/api/v1/analytics/calendar?year={now.year}&month={now.month}",
        headers={"X-Workspace-Id": ws_id},
    )
    piece = [p for day in res.json()["days"].values() for p in day][0]
    assert piece["platforms"] == ["Instagram"]
    assert len(piece["platform_results"]) == 1
    assert piece["platform_results"][0]["platform"] == "Instagram"
    assert piece["platform_results"][0]["status"] == "pending"


async def test_calendar_returns_real_kanban_stage(signup_user):
    """Calendar used to only expose bare publish_status, which can't tell
    "drafting" apart from "staging" (both read as "pending") — added so
    the frontend's actions bar can offer the correct next stage transition
    instead of guessing. Approve moves approval_status to "approved",
    which compute_kanban_stage() reads as "staging"."""
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Calendar WS")
    piece_id = await _seed_piece(ws_id, profile["id"], str(uuid4()))
    now = datetime.now(timezone.utc)

    res = await client.get(
        f"/api/v1/analytics/calendar?year={now.year}&month={now.month}",
        headers={"X-Workspace-Id": ws_id},
    )
    piece = [p for day in res.json()["days"].values() for p in day][0]
    assert piece["stage"] == "drafting"

    approve = await client.patch(
        f"/api/v1/content/pieces/{piece_id}/approve", headers={"X-Workspace-Id": ws_id},
    )
    assert approve.status_code == 200, approve.text

    res2 = await client.get(
        f"/api/v1/analytics/calendar?year={now.year}&month={now.month}",
        headers={"X-Workspace-Id": ws_id},
    )
    piece2 = [p for day in res2.json()["days"].values() for p in day][0]
    assert piece2["stage"] == "staging"


async def test_calendar_summary_uses_real_status_vocabulary(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Calendar WS")
    await _seed_piece(ws_id, profile["id"], str(uuid4()))

    now = datetime.now(timezone.utc)
    res = await client.get(
        f"/api/v1/analytics/calendar?year={now.year}&month={now.month}",
        headers={"X-Workspace-Id": ws_id},
    )
    summary = res.json()["summary"]
    assert set(summary.keys()) == {"total", "published", "queued", "pending", "failed"}
    assert summary["total"] == 1
    assert summary["pending"] == 1
