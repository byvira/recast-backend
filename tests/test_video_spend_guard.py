"""Nothing on the video side is paid for when the owner has paused generation, switched video off, or the day's AI clips are used up."""
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.db.mongo import get_db, media_assets
from app.video import clipgen
from app.video.guard import video_spend_guard


async def _workspace(**fields) -> str:
    ws = f"ws-{uuid4()}"
    await get_db()["workspaces"].insert_one({"id": ws, **fields})
    return ws


async def test_a_paused_workspace_is_refused_before_any_video_spend():
    ws = await _workspace(generation_halted=True)
    with pytest.raises(HTTPException) as refused:
        await video_spend_guard(ctx=SimpleNamespace(workspace_id=ws))
    assert refused.value.status_code == 403


async def test_a_workspace_with_video_switched_off_is_refused():
    ws = await _workspace(video_access={"enabled": False})
    with pytest.raises(HTTPException) as refused:
        await video_spend_guard(ctx=SimpleNamespace(workspace_id=ws))
    assert refused.value.status_code == 403


async def test_an_open_workspace_passes_the_guard():
    ws = await _workspace()
    ctx = SimpleNamespace(workspace_id=ws)
    assert await video_spend_guard(ctx=ctx) is ctx


async def test_a_paused_workspace_makes_no_ai_clip(monkeypatch):
    ws = await _workspace(generation_halted=True)

    async def must_not_run(prompt):
        raise AssertionError("the provider was called")

    monkeypatch.setattr(clipgen, "generate", must_not_run)
    with pytest.raises(clipgen.ClipGenError):
        await clipgen.make_clip(workspace_id=ws, user_id="u1", prompt="a person writing a plan at a desk")


async def test_the_days_ai_clips_are_capped_per_workspace(monkeypatch):
    ws = await _workspace()
    await media_assets.insert_many([
        {"id": uuid4().hex, "workspace_id": ws, "kind": "video", "source": "ai_generated", "created_at": datetime.now(timezone.utc), "url": "https://x/v.mp4"}
        for _ in range(clipgen.AI_CLIPS_PER_DAY)
    ])

    async def must_not_run(prompt):
        raise AssertionError("the provider was called")

    monkeypatch.setattr(clipgen, "generate", must_not_run)
    with pytest.raises(clipgen.ClipGenError) as capped:
        await clipgen.make_clip(workspace_id=ws, user_id="u1", prompt="a different description of a clip")
    assert "today" in str(capped.value)


def test_one_narration_request_cannot_voice_more_than_the_ceiling():
    from pydantic import ValidationError

    from app.api.v1.video_narrate import NARRATION_MAX_CHARS, NarrationBody

    ok = NarrationBody(media_id="m", lines=[{"speaker": "", "text": "x" * 3000}] * 5)
    assert ok.media_id == "m"
    with pytest.raises(ValidationError):
        NarrationBody(media_id="m", lines=[{"speaker": "", "text": "x" * 3000}] * 11)
    assert NARRATION_MAX_CHARS == 30_000
