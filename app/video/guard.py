"""One check for every route that spends something on a video: a model, a voice, an AI clip or a render.

It honours what the owner can switch off (the generation pause and the video-access switch), refuses when the workspace has used its
token budget, and counts the spend against this workspace. It runs first, so nothing is paid for and then refused."""
from __future__ import annotations

from fastapi import Depends, HTTPException

from app.agents.supervisor.service import assert_ai_budget_available, assert_generation_allowed
from app.core.workspace import WorkspaceContext, require
from app.shared.llm import set_usage_workspace
from app.video import governance


async def video_spend_guard(ctx: WorkspaceContext = Depends(require("create_content"))) -> WorkspaceContext:
    if not await governance.video_enabled(ctx.workspace_id):
        raise HTTPException(status_code=403, detail=governance.ACCESS_OFF)
    await assert_generation_allowed(ctx.workspace_id)
    await assert_ai_budget_available(ctx.workspace_id)
    set_usage_workspace(ctx.workspace_id)
    return ctx
