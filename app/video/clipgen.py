"""AI-made video clips for a scene. A short text description goes to a text-to-video model served through Hugging Face (Wan 2.2 by default
in the docs), and the clip that comes back is kept in the workspace's own storage like any other library video, marked as AI generated.

The feature is off until `HUGGINGFACE_VIDEO_MODEL` is set, so nothing is billed by accident. Every call is one paid clip of about five
seconds, so callers ask for it only when the member has chosen it, and a failure is reported plainly instead of retried in a loop.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.core.config import settings
from app.db.mongo import media_assets
from app.models.media import MediaAsset, MediaKind, MediaSource
from app.shared.llm_health.track import track
from app.shared.storage import ContentType as UploadContentType, upload_file_detailed

logger = logging.getLogger(__name__)

MAX_PROMPT = 400
#: A clip takes a minute or more. Past this the provider is not going to answer, and the scene goes without footage.
TIMEOUT_S = 420.0
#: What the model makes by default: about five seconds.
DEFAULT_SECONDS = 5.0
MIN_BYTES = 2_000
#: AI clips one workspace may make in a day. Every one is a paid request.
AI_CLIPS_PER_DAY = 20


class ClipGenError(Exception):
    """The clip could not be made. The message is plain and safe to show."""


def enabled() -> bool:
    return bool(settings.HUGGINGFACE_API_TOKEN and settings.HUGGINGFACE_VIDEO_MODEL)


def summary() -> dict:
    """What the screens need to say honestly: whether AI clips are on, and roughly how long each takes."""
    return {"available": enabled(), "model": settings.HUGGINGFACE_VIDEO_MODEL or None, "seconds_each": DEFAULT_SECONDS}


def _call(prompt: str) -> bytes:
    # The library handles the provider's own waiting and polling, so one call returns the finished clip.
    from huggingface_hub import InferenceClient

    client = InferenceClient(provider=settings.HUGGINGFACE_VIDEO_PROVIDER or "auto", api_key=settings.HUGGINGFACE_API_TOKEN, timeout=TIMEOUT_S)
    return client.text_to_video(prompt, model=settings.HUGGINGFACE_VIDEO_MODEL)


def _plain(exc: Exception) -> str:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (401, 403):
        return "The video service did not accept the access key. Check the Hugging Face key and its Inference Providers permission."
    if status == 402:
        return "The video service has run out of credit. Add credit on Hugging Face, then try again."
    if status == 429:
        return "The video service is busy. Try again in a few minutes."
    return "The AI clip could not be made right now."


async def generate(prompt: str) -> bytes:
    """The bytes of one clip made from the description, or a ClipGenError saying why not."""
    if not enabled():
        raise ClipGenError("AI clips are not set up on this server yet.")
    text = " ".join((prompt or "").split())[:MAX_PROMPT]
    if len(text) < 3:
        raise ClipGenError("Describe what the clip should show.")
    async with track("huggingface", settings.HUGGINGFACE_VIDEO_MODEL, feature="video_generation"):
        try:
            data = await asyncio.wait_for(asyncio.to_thread(_call, text), TIMEOUT_S + 30)
        except asyncio.TimeoutError:
            raise ClipGenError("The clip took too long to make.") from None
        except Exception as exc:  # noqa: BLE001 - every provider failure becomes one plain message
            logger.warning("AI clip failed: %s", exc)
            raise ClipGenError(_plain(exc)) from exc
    if not data or len(data) < MIN_BYTES:
        raise ClipGenError("The video service sent back an empty clip.")
    return data


async def save(*, workspace_id: str, user_id: str, data: bytes, prompt: str, upload=upload_file_detailed) -> MediaAsset:
    """The clip as a library video, kept in the workspace's storage and marked as AI generated."""
    try:
        info = await upload(data, UploadContentType.VIDEO, user_id, filename=f"ai-clip-{uuid.uuid4().hex[:8]}.mp4")
    except Exception as exc:  # noqa: BLE001
        logger.error("AI clip upload failed for workspace %s: %s", workspace_id, exc)
        raise ClipGenError("The clip was made but could not be saved. Please try again.") from exc
    media = MediaAsset(
        id=str(uuid.uuid4()), workspace_id=workspace_id, kind=MediaKind.VIDEO, url=info["url"], mime_type="video/mp4",
        width=info.get("width") or None, height=info.get("height") or None, duration_s=info.get("duration_s") or DEFAULT_SECONDS,
        source=MediaSource.AI_GENERATED, created_by=user_id, created_at=datetime.now(timezone.utc), size_bytes=info.get("bytes") or len(data),
        poster_url=info.get("poster_url"),
    )
    await media_assets.insert_one(media.model_dump())
    return media


#: A clip made from the same description in the same workspace this recently is handed back instead of being paid for again. A video
#: that failed after its clips were made, and is started again, then does not buy them a second time.
REUSE_WITHIN = timedelta(hours=24)


def _prompt_key(prompt: str) -> str:
    return hashlib.sha1(" ".join((prompt or "").lower().split())[:MAX_PROMPT].encode("utf-8")).hexdigest()


async def make_clip(*, workspace_id: str, user_id: str, prompt: str) -> MediaAsset:
    """Generate and keep one clip, or hand back the one already made from this description in the last day."""
    from fastapi import HTTPException

    from app.agents.content_guard.media import ContentRejected, assert_speech_ok
    from app.agents.supervisor.service import assert_generation_allowed

    try:
        await assert_generation_allowed(workspace_id)
    except HTTPException as exc:
        raise ClipGenError(str(exc.detail)) from exc
    try:
        # the description is checked against the same rules as any script before it is sent to a paid service
        await assert_speech_ok(prompt, noun="clip description", workspace_id=workspace_id, where="ai video clip")
    except ContentRejected as exc:
        raise ClipGenError(exc.detail.get("message") if isinstance(exc.detail, dict) else "That description cannot be used.") from exc
    key = _prompt_key(prompt)
    recent = await media_assets.find_one(
        {"workspace_id": workspace_id, "ai_prompt_key": key, "created_at": {"$gte": datetime.now(timezone.utc) - REUSE_WITHIN}},
        sort=[("created_at", -1)],
    )
    if recent:
        return MediaAsset(**recent)
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    if await media_assets.count_documents({"workspace_id": workspace_id, "source": MediaSource.AI_GENERATED.value, "created_at": {"$gte": today}}) >= AI_CLIPS_PER_DAY:
        raise ClipGenError(f"This workspace has made its {AI_CLIPS_PER_DAY} AI clips for today. More can be made tomorrow, or use stock footage.")
    data = await generate(prompt)
    media = await save(workspace_id=workspace_id, user_id=user_id, data=data, prompt=prompt)
    await media_assets.update_one({"id": media.id, "workspace_id": workspace_id}, {"$set": {"ai_prompt_key": key}})
    return media
