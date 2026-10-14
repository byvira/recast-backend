"""The cross-type toolkit: one registry through which any agent (a director, Remy, Odette) can use any content type's capability.

Every tool checks the caller's permission, honours the workspace kill switch for anything that generates, states its cost class,
and records what it did in the decision log through the plan executor. Tools that already exist as registered job actions
(text, audio) are exposed through one adapter, so they run exactly the code the Audio and Text pages run.

The caller supplies `state`: {"ctx": WorkspaceContext, "reporter": an object with async step(label)}.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import HTTPException

from app.agents.runtime.tools import ToolRegistry, ToolResult, ToolSpec
from app.core.rbac import assert_permission

logger = logging.getLogger(__name__)

TOOLKIT = ToolRegistry("toolkit")


class _NoSteps:
    async def step(self, label: str) -> None:
        return None


def _reporter(state: dict) -> Any:
    return state.get("reporter") or _NoSteps()


async def _allowed_to_generate(ctx: Any) -> None:
    from app.agents.supervisor.service import assert_generation_allowed

    await assert_generation_allowed(ctx.workspace_id)


def _bad_input(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=f"The request for this step is not valid: {str(exc)[:200]}")


# ── tools that are already registered job actions: one adapter ────────────────────────────────────────────────────────────

def _action_tool(tool: str, action: str, description: str, *, cost: str, generates: bool, timeout_s: float = 1800.0) -> None:
    async def run(*, state: dict, payload: Optional[dict] = None) -> Any:
        from app.shared import jobs
        from app.shared.job_actions import register_all

        if action not in jobs.ACTIONS:
            register_all()
        spec = jobs.ACTIONS[action]
        ctx = state["ctx"]
        assert_permission(ctx.member, spec.permission)
        if generates:
            await _allowed_to_generate(ctx)
        try:
            model = spec.payload_model(**(payload or {}))
        except Exception as exc:  # noqa: BLE001 - a validation error, reported plainly
            raise _bad_input(exc)
        return await spec.run(ctx, model, _reporter(state))

    TOOLKIT.register(ToolSpec(tool, description, cost=cost, timeout_s=timeout_s), run)


for _tool, _action, _desc, _cost, _gen in (
    ("text.write", "text.generate", "Write a post from a topic for a platform, in the brand's voice", "llm", True),
    ("text.regenerate", "text.regenerate", "Write a post again with a changed direction", "llm", True),
    ("text.repurpose", "text.repurpose", "Turn a post into versions for other platforms", "llm", True),
    ("audio.transcribe", "audio.transcribe", "Write out what was said in a recording, with word timing", "llm", False),
    ("audio.cleanup", "audio.cleanup", "Clean up a recording as a new version", "free", False),
    ("audio.assemble", "audio.assemble", "Build a finished episode from a recording and the audio kit", "free", False),
    ("audio.dialogue", "audio.dialogue", "Voice a conversation between several speakers", "provider", True),
    ("audio.import", "audio.import", "Fetch audio from a link and make a recording from it", "free", False),
    ("audio.director", "audio.director", "Run a goal for a recording: narrate, clean up, chapters, soundbites, show notes, checks", "llm", True),
):
    _action_tool(_tool, _action, _desc, cost=_cost, generates=_gen)


# ── tools with their own entry points ────────────────────────────────────────────────────────────────────────────────────

async def _audio_narrate(*, state: dict, request: dict) -> Any:
    """Voice a script, or a post by `source_piece_id`, into a new recording."""
    from app.api.v1 import audio_assets as audio

    ctx = state["ctx"]
    assert_permission(ctx.member, "create_content")
    await _allowed_to_generate(ctx)
    try:
        body = audio.GenerateAudioAssetRequest(**request)
    except Exception as exc:  # noqa: BLE001
        raise _bad_input(exc)
    asset = await audio.create_audio_from_script(body, ctx, _reporter(state))
    return {"asset_id": asset.id, "media_id": getattr(asset, "media_id", None), "origin": "ai"}


async def _audio_show_notes(*, state: dict, audio_asset_id: str, output_type: str = "show_notes") -> Any:
    from app.api.v1 import audio_assets as audio

    ctx = state["ctx"]
    assert_permission(ctx.member, "edit_content")
    handler = audio.generate_derivative
    inner = getattr(handler, "__wrapped__", handler)
    out = await inner(request=None, audio_asset_id=audio_asset_id, body=audio.DerivativeRequest(output_type=output_type), ctx=ctx)
    return {"output_type": out.output_type, "content": out.content, "quality": out.quality}


async def _image_generate(*, state: dict, topic: str, brand_id: str) -> Any:
    """A picture for a topic through the image agent (gates, provider chain, QA). The library item is flagged AI generated."""
    from app.db.mongo import brand_profiles
    from app.pipelines.media import image_generation as ig

    ctx = state["ctx"]
    assert_permission(ctx.member, "create_content")
    await _allowed_to_generate(ctx)
    brand = await brand_profiles.find_one({"id": brand_id, "workspace_id": ctx.workspace_id})
    if not brand:
        raise HTTPException(status_code=404, detail="That brand was not found in this workspace.")
    asset = await ig.generate_brand_image(topic=topic, brand_profile=brand, workspace_id=ctx.workspace_id, user_id=ctx.user_id)
    if asset is None:
        raise HTTPException(status_code=502, detail=ig.last_failure_reason() or "No picture could be made right now.")
    return {"media_id": asset.id, "url": asset.url, "origin": asset.origin, "qa_flagged": asset.qa_flagged}


async def _video_render(*, state: dict, project_id: str, preset: str = "standard", lufs: float = -14.0) -> Any:
    from app.video import service

    ctx = state["ctx"]
    assert_permission(ctx.member, "edit_content")
    await _allowed_to_generate(ctx)
    try:
        return await service.render_project_and_wait(
            workspace_id=ctx.workspace_id, user_id=ctx.user_id, project_id=project_id, preset=preset, lufs=lufs,
            reporter=_reporter(state),
        )
    except service.RenderFailed as exc:
        raise HTTPException(status_code=502, detail=str(exc))


async def _remy_check_voice(*, state: dict, text: str, pipeline_type: str = "text", target: str = "") -> Any:
    """Remy's voice check on any text a pipeline is about to use: a video hook, a narration script, a picture headline."""
    from app.agents.personal.assist import align_draft

    ctx = state["ctx"]
    assert_permission(ctx.member, "edit_content")
    if not (text or "").strip():
        raise HTTPException(status_code=400, detail="There is no text to check.")
    return await align_draft(
        workspace_id=ctx.workspace_id, user_id=ctx.user_id, pipeline_type=pipeline_type, draft_text=text[:8000], target=target,
    )


async def _top_assets(*, state: dict, asset_type: str, limit: int = 5) -> Any:
    from app.api.v1 import analytics

    ctx = state["ctx"]
    handler = analytics.top_assets
    return await getattr(handler, "__wrapped__", handler)(request=None, asset_type=asset_type, limit=limit, ctx=ctx)


for _spec, _fn in (
    (ToolSpec("audio.narrate", "Voice a script or a post into a new recording", cost="provider", timeout_s=900), _audio_narrate),
    (ToolSpec("audio.show_notes", "Write show notes, captions or a summary from a recording's transcript", cost="llm", timeout_s=180),
     _audio_show_notes),
    (ToolSpec("image.generate", "Make a picture for a topic through the image agent", cost="provider", timeout_s=300), _image_generate),
    (ToolSpec("video.render", "Render a video project and wait for the finished video", cost="worker", timeout_s=7800), _video_render),
    (ToolSpec("remy.check_voice", "Check text against the member's own voice", cost="llm", timeout_s=60), _remy_check_voice),
    (ToolSpec("analytics.top_assets", "Which pictures or recordings did best within their kind", cost="free", timeout_s=30), _top_assets),
):
    TOOLKIT.register(_spec, _fn)


async def use(name: str, *, ctx: Any, reporter: Any = None, **kwargs: Any) -> ToolResult:
    """Run one toolkit tool for a caller. Never raises: a refusal or failure is a result with a plain message."""
    return await TOOLKIT.run(name, state={"ctx": ctx, "reporter": reporter}, **kwargs)
