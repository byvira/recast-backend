"""The actions that can be started as background jobs through `POST /api/v1/jobs` (see `app.shared.jobs`).

Each is a thin wrapper over work the app already does: the bulk ones call the same storage functions the single-item routes use,
and the text ones call the same handlers as their plain routes."""
from __future__ import annotations

import re
from typing import Any, Optional

from pydantic import BaseModel, Field, create_model

from app.models.text import BatchGenerateRequest, GenerateTextRequest, RegenerateRequest, RepurposeRequest
from app.shared.jobs import JobAction, register

MAX_ITEMS = 200


class PieceIds(BaseModel):
    piece_ids: list[str] = Field(min_length=1, max_length=MAX_ITEMS)


class PieceIdsArchive(PieceIds):
    archived: bool = True


async def _bulk_delete(ctx: Any, payload: PieceIds, reporter: Any) -> dict:
    from app.pipelines.text.storage import delete_piece

    ids = list(dict.fromkeys(payload.piece_ids))
    done, missing = 0, []
    for number, piece_id in enumerate(ids, start=1):
        await reporter.step(f"Deleting {number} of {len(ids)}")
        if await delete_piece(piece_id, ctx.workspace_id):
            done += 1
        else:
            missing.append(piece_id)
    return {"deleted": done, "not_found": missing}


async def _bulk_archive(ctx: Any, payload: PieceIdsArchive, reporter: Any) -> dict:
    from app.pipelines.text.storage import update_piece_status

    ids = list(dict.fromkeys(payload.piece_ids))
    done, missing = 0, []
    verb = "Archiving" if payload.archived else "Restoring"
    for number, piece_id in enumerate(ids, start=1):
        await reporter.step(f"{verb} {number} of {len(ids)}")
        if await update_piece_status(piece_id=piece_id, workspace_id=ctx.workspace_id, archived=payload.archived):
            done += 1
        else:
            missing.append(piece_id)
    return {"archived" if payload.archived else "restored": done, "not_found": missing}


def _text_action(name: str, model: type[BaseModel], handler_name: str, title, steps) -> None:
    async def run(ctx: Any, payload: Any, reporter: Any) -> Any:
        from app.api.v1 import text

        handler = getattr(text, handler_name)
        return await getattr(handler, "__wrapped__", handler)(None, payload, ctx)

    register(JobAction(
        name=name, permission="create_content", payload_model=model, run=run, title=title, steps=steps, kind="text",
        href="/dashboard/drafts", gated=True, restartable=False, retries=0,
        description="The same work as the plain route, in the background. The posts it makes are saved as it goes.",
    ))


# ── Audio ────────────────────────────────────────────────────────────

_LIVE_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


class _AudioReporter:
    """Reports each step to the saved run (a pause and cancel point) and, when the browser named a live id, also to the live-steps
    tracker the Audio page already reads, so its progress panel works the same for a background job."""

    def __init__(self, base: Any, workspace_id: str, live_id: Optional[str]) -> None:
        self._base = base
        self._workspace_id = workspace_id
        self._live_id = live_id if live_id and _LIVE_ID.match(live_id) else None
        self._done = 0

    async def step(self, label: str) -> None:
        await self._base.step(label)
        if self._live_id:
            from app.shared.activity.runs import update_run

            await update_run(self._workspace_id, self._live_id, stage=label, steps_done=self._done)
        self._done += 1


async def _with_video_guard(guard: Any, work: Any) -> Any:
    """Runs `work` inside the video render guard (the record of the render and the double start check), the same as the route does."""
    await guard.__anext__()
    try:
        result = await work
    except BaseException as exc:  # noqa: BLE001 - recorded by the guard, then raised again
        try:
            await guard.athrow(exc)
        except BaseException:  # noqa: BLE001
            pass
        raise
    try:
        await guard.__anext__()
    except StopAsyncIteration:
        pass
    return result


def _audio_action(name: str, *, handler_name: str, settings_model: Optional[type[BaseModel]], with_asset: bool, title, steps_total: int,
                  restartable: bool, retries: int, guarded: bool = False, description: str = "") -> None:
    fields: dict[str, Any] = {"live_run_id": (Optional[str], None)}
    if with_asset:
        fields["audio_asset_id"] = (str, Field(min_length=1, max_length=64))
    if settings_model is not None:
        fields["settings"] = (settings_model, ... if not with_asset else Field(default_factory=settings_model))
    payload_model = create_model(f"{name.title().replace('.', '').replace('_', '')}Payload", **fields)

    async def run(ctx: Any, payload: Any, reporter: Any) -> Any:
        from app.api.v1 import audio_assets as audio
        from app.shared.activity.runs import end_run, start_run

        handler = getattr(audio, handler_name)
        inner = getattr(handler, "__wrapped__", handler)
        rep = _AudioReporter(reporter, ctx.workspace_id, payload.live_run_id)
        live = rep._live_id
        if live:
            await start_run(workspace_id=ctx.workspace_id, run_id=live, kind="audio", title=title(payload), steps_total=steps_total)
        kwargs: dict[str, Any] = {"request": None, "ctx": ctx, "run": rep}
        if with_asset:
            kwargs["audio_asset_id"] = payload.audio_asset_id
        if settings_model is not None:
            kwargs["body"] = payload.settings
        try:
            if guarded:
                kwargs["_guard"] = None
                return await _with_video_guard(audio._video_render_guard(payload.audio_asset_id, payload.settings, ctx), inner(**kwargs))
            return await inner(**kwargs)
        finally:
            if live:
                await end_run(ctx.workspace_id, live)

    register(JobAction(
        name=name, permission="edit_content" if with_asset else "create_content", payload_model=payload_model, run=run, title=title,
        steps=lambda p: steps_total, kind="audio", restartable=restartable, retries=retries, gated=False, description=description,
        shape=lambda asset: {"asset_id": asset.id, "version_count": getattr(asset, "version_count", None)},
        result_href=lambda asset: f"/dashboard/pipelines/audio?asset={asset.id}",
    ))


def _register_audio() -> None:
    from app.api.v1.audio_assets import GenerateDialogueRequest, ImportLinkRequest, RegenerateAudioRequest
    from app.models.audio_asset import MakeVideoRequest
    from app.pipelines.media.audio_assemble import AssemblePlan
    from app.pipelines.media.audio_cleanup import CleanupSettings

    _audio_action("audio.transcribe", handler_name="transcribe_audio_asset", settings_model=None, with_asset=True,
                  title=lambda p: "Transcribe recording", steps_total=2, restartable=True, retries=1,
                  description="Write out what was said (safe to start again after a restart).")
    _audio_action("audio.cleanup", handler_name="cleanup_audio_asset", settings_model=CleanupSettings, with_asset=True,
                  title=lambda p: "Clean up recording", steps_total=3, restartable=False, retries=0,
                  description="Apply the chosen cleanup as a new version.")
    _audio_action("audio.assemble", handler_name="assemble_audio_asset", settings_model=AssemblePlan, with_asset=True,
                  title=lambda p: "Build episode", steps_total=3, restartable=True, retries=0,
                  description="Build a finished episode into a new file.")
    _audio_action("audio.video", handler_name="make_video_from_audio_asset", settings_model=MakeVideoRequest, with_asset=True,
                  title=lambda p: "Render video", steps_total=3, restartable=True, retries=0, guarded=True,
                  description="Make a captioned video from a recording.")
    _audio_action("audio.regenerate", handler_name="regenerate_audio_asset", settings_model=RegenerateAudioRequest, with_asset=True,
                  title=lambda p: "Voice the script again", steps_total=2, restartable=False, retries=1,
                  description="Voice a narration again as a new version.")
    _audio_action("audio.import", handler_name="import_audio_from_link", settings_model=ImportLinkRequest, with_asset=False,
                  title=lambda p: "Import audio", steps_total=5, restartable=True, retries=1,
                  description="Fetch audio from a link and make a recording from it.")
    _audio_action("audio.dialogue", handler_name="generate_dialogue", settings_model=GenerateDialogueRequest, with_asset=False,
                  title=lambda p: "Make dialogue", steps_total=3, restartable=False, retries=1,
                  description="Voice a conversation between several speakers.")


class PublishPostPayload(BaseModel):
    piece_id: str = Field(min_length=1, max_length=64)
    confirm_publish_anyway: bool = False
    youtube_metadata: Optional[dict] = None


class PublishBatchPayload(BaseModel):
    piece_ids: list[str] = Field(min_length=1, max_length=50)
    confirm_publish_anyway: bool = False


async def _publish_one(ctx: Any, piece_id: str, confirm: bool = False, youtube_metadata: Optional[dict] = None) -> dict:
    """Publish one post the way the Publish now button does, and always answer with a plain result: a refusal (not approved, platform
    paused, flagged) comes back with its code and reason instead of stopping everything."""
    from fastapi import HTTPException

    from app.api.v1 import publish

    handler = getattr(publish.publish_now, "__wrapped__", publish.publish_now)
    body = publish.PublishNowRequest(piece_id=piece_id, confirm_publish_anyway=confirm, youtube_metadata=youtube_metadata)
    try:
        result = await handler(None, body, ctx)
    except HTTPException as exc:
        detail = exc.detail
        code = detail.get("code") if isinstance(detail, dict) else None
        reason = detail.get("message") if isinstance(detail, dict) else str(detail)
        return {"success": False, "piece_id": piece_id, "status": "blocked", "code": code or "blocked", "reason": reason, "retryable": False}
    return dict(result)


async def _publish_post(ctx: Any, payload: PublishPostPayload, reporter: Any) -> dict:
    await reporter.step("Publishing")
    return await _publish_one(ctx, payload.piece_id, payload.confirm_publish_anyway, payload.youtube_metadata)


async def _publish_batch(ctx: Any, payload: PublishBatchPayload, reporter: Any) -> dict:
    from app.db.mongo import content_pieces
    from app.pipelines.publish.spine import platform_key

    ids = list(dict.fromkeys(payload.piece_ids))
    platforms = {p["piece_id"]: p.get("platform", "") async for p in content_pieces.find({"piece_id": {"$in": ids}, "workspace_id": ctx.workspace_id}, {"piece_id": 1, "platform": 1})}
    results: list[dict] = []
    for number, piece_id in enumerate(ids, start=1):
        await reporter.step(f"Publishing {number} of {len(ids)}")
        if piece_id not in platforms:
            results.append({"success": False, "piece_id": piece_id, "status": "blocked", "code": "not_found", "reason": "This post could not be found."})
        elif platform_key(platforms[piece_id]) == "youtube":
            # A YouTube upload is reviewed (title, visibility, thumbnail) one post at a time, never sent in bulk.
            results.append({"success": False, "piece_id": piece_id, "status": "skipped", "code": "review_first", "reason": "YouTube posts are reviewed one at a time."})
        else:
            results.append(await _publish_one(ctx, piece_id, payload.confirm_publish_anyway))
    published = sum(1 for r in results if r.get("success"))
    retrying = sum(1 for r in results if r.get("status") == "retry_scheduled")
    return {"results": results, "published": published, "retrying": retrying, "left_out": len(results) - published - retrying}


class CampaignRef(BaseModel):
    campaign_id: str = Field(min_length=1, max_length=64)


class CampaignPieceRef(CampaignRef):
    piece_id: str = Field(min_length=1, max_length=64)


def _campaign_action(name: str, model: type[BaseModel], handler_name: str, title, steps: int, description: str) -> None:
    async def run(ctx: Any, payload: Any, reporter: Any) -> Any:
        from app.api.v1 import campaigns

        handler = getattr(campaigns, handler_name)
        args = [payload.campaign_id] + ([payload.piece_id] if hasattr(payload, "piece_id") else [])
        await reporter.step("Working on it")
        return await getattr(handler, "__wrapped__", handler)(None, *args, ctx)

    register(JobAction(
        name=name, permission="create_content", payload_model=model, run=run, kind="campaign", title=title,
        steps=lambda p: steps, href="/dashboard/pipelines/new", gated=True, retries=0, description=description,
    ))


def register_all() -> None:
    from app.shared.activity.runs import run_label

    register(JobAction(
        name="content.bulk_delete", permission="edit_content", payload_model=PieceIds, run=_bulk_delete, kind="text",
        title=lambda p: f"Delete {len(p.piece_ids)} posts", steps=lambda p: len(set(p.piece_ids)), href="/dashboard/library",
        restartable=True, description="Delete many posts (they can be restored by support).",
    ))
    register(JobAction(
        name="content.bulk_archive", permission="edit_content", payload_model=PieceIdsArchive, run=_bulk_archive, kind="text",
        title=lambda p: f"{'Archive' if p.archived else 'Restore'} {len(p.piece_ids)} posts", steps=lambda p: len(set(p.piece_ids)),
        href="/dashboard/library", restartable=True, description="Archive or restore many posts.",
    ))
    _text_action("text.repurpose", RepurposeRequest, "repurpose_content",
                 lambda p: f"Repurpose: {run_label(p.source_content)}", lambda p: max(1, len(p.target_platforms)))
    _text_action("text.generate", GenerateTextRequest, "generate_text_content",
                 lambda p: f"Text: {run_label(p.content)}", lambda p: max(1, len(p.platforms)))
    _text_action("text.batch", BatchGenerateRequest, "batch_generate",
                 lambda p: f"Week of posts: {run_label(p.topic_cluster)}", lambda p: max(1, p.days))
    _text_action("text.regenerate", RegenerateRequest, "regenerate_content",
                 lambda p: f"Regenerate {p.platform}", lambda p: 1)
    _campaign_action("campaign.next_batch", CampaignRef, "generate_next_batch", lambda p: "Campaign: next batch", 1,
                     "Make the next batch of posts for a campaign.")
    _campaign_action("campaign.retry_media", CampaignPieceRef, "retry_post_media", lambda p: "Campaign: retry media", 1,
                     "Make again the media that failed for one post.")
    _campaign_action("campaign.regenerate_media", CampaignPieceRef, "regenerate_post_image", lambda p: "Campaign: new picture", 1,
                     "Make one post's picture again.")
    register(JobAction(
        name="publish.now", permission="publish_content", payload_model=PublishPostPayload, run=_publish_post, kind="text",
        title=lambda p: "Publish a post", steps=lambda p: 1, href="/dashboard/drafts", restartable=False, retries=0,
        description="Publish one post now. Never started again after a restart, so a post is never sent twice.",
    ))
    register(JobAction(
        name="publish.batch", permission="publish_content", payload_model=PublishBatchPayload, run=_publish_batch, kind="text",
        title=lambda p: f"Publish {len(set(p.piece_ids))} posts", steps=lambda p: len(set(p.piece_ids)), href="/dashboard/drafts",
        restartable=False, retries=0,
        description="Publish several posts one after another. Each post reports its own result; one failing never stops the others.",
    ))
    _register_audio()
