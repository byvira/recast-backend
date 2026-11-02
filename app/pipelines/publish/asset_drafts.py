"""A draft post made from a picture or a video, with the file attached, in one step. Used by "Save as draft" on the history cards of the
Image and Video pipelines.

Safe to repeat: the same source always gives back the same draft, so a second click (or a retry after a dropped connection) never makes
another one. A draft that was deleted is replaced by a fresh one with a new id.
"""
from __future__ import annotations

from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

from app.db.mongo import content_pieces
from app.models.text import InputSourceType
from app.pipelines.publish import attachments as piece_attachments
from app.pipelines.text.storage import ensure_session_exists, get_piece, save_live_piece


async def draft_from_asset(
    *, workspace_id: str, user_id: str, brand_id: str, platform: str, caption: str, origin: dict[str, str], attach: dict,
    group_by: str | None = None,
) -> dict:
    """`origin` names the source (for example the recording and the video) and is what makes the draft repeatable. `attach` is what
    to attach: the arguments of `attachments.resolve_asset` (asset_type plus the ids). Drafts with the same `group_by` share one run, so
    the Publish Workspace shows them together as one pill for each platform."""
    existing = await content_pieces.find_one({
        "workspace_id": workspace_id, "deleted": {"$ne": True},
        **{f"send_origin.{key}": value for key, value in origin.items()},
    })
    created = False
    if existing:
        piece_id = existing["piece_id"]
    else:
        # First time: one repeatable id per source. The unique index on piece_id is what makes two clicks at the same moment land on
        # the same draft instead of creating another.
        key = f"send-to-draft:{workspace_id}:{':'.join(origin.values())}"
        piece_id = str(uuid5(NAMESPACE_URL, key))
        repeatable = True
        if await content_pieces.find_one({"piece_id": piece_id, "deleted": True}, {"_id": 1}):
            piece_id, repeatable = str(uuid4()), False
        group_key = f"send-to-draft:{workspace_id}:{group_by}" if group_by else key
        session_id = str(uuid5(NAMESPACE_URL, group_key + ":session")) if repeatable else str(uuid4())
        try:
            await ensure_session_exists(
                session_id=session_id, workspace_id=workspace_id, user_id=user_id, brand_id=brand_id, source_type=InputSourceType.TEXT.value,
            )
        except DuplicateKeyError:
            pass  # the same moment's other click made the session first
        try:
            await save_live_piece(
                session_id=session_id, workspace_id=workspace_id, user_id=user_id, brand_id=brand_id, platform=platform, content=caption,
                word_count=len(caption.split()), char_count=len(caption), piece_id=piece_id, extra_fields={"send_origin": origin},
            )
            created = True
        except DuplicateKeyError:
            pass  # a click at the same moment made it first

    piece = await get_piece(piece_id, workspace_id)
    if not piece:
        raise HTTPException(status_code=500, detail="Couldn't make the draft. Try again.")
    try:
        resolved = await piece_attachments.resolve_asset(workspace_id, **attach)
        attachment, _ = await piece_attachments.attach(piece, workspace_id, user_id, resolved)
    except piece_attachments.AttachmentError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message)
    return {"piece_id": piece_id, "platform": piece["platform"], "content": piece["content"], "created": created, "attachment": attachment}


async def words_for_platform(
    *, workspace_id: str, user_id: str, brand_id: str, text: str, source_platform: str, target_platform: str,
) -> str:
    """The post's words rewritten for another platform in the brand's voice, through the same repurpose step the writing pipeline
    uses. Costs one post's credits (long form for a blog, newsletter or YouTube description), set aside first and given back when
    the rewrite fails; then the words come back as they were, so the draft is never lost."""
    from app.agents.text.nodes import _extract_enforcement_data, merge_member_lexicon_enforcement
    from app.billing import credits
    from app.db.mongo import brand_profiles
    from app.models.text import AgentTask, Platform
    from app.pipelines.text.brand_context import build_brand_context
    from app.pipelines.text.orchestrator import _post_action
    from app.pipelines.text.repurpose import run_repurpose_agent

    try:
        source, target = Platform(source_platform), Platform(target_platform)
    except ValueError:
        return text
    if source == target or not text.strip():
        return text
    brand = await brand_profiles.find_one({"id": brand_id, "workspace_id": workspace_id}, {"_id": 0}) or {}
    hold = await credits.reserve(workspace_id, user_id, _post_action(target), step=f"draft-words:{uuid4().hex}")
    try:
        enforcement = await merge_member_lexicon_enforcement(_extract_enforcement_data(brand), workspace_id=workspace_id, user_id=user_id)
        task = AgentTask(agent="repurpose", platform=target, content=text, brand_context=build_brand_context(brand),
                         session_id=uuid4().hex, metadata={**enforcement, "language": brand.get("language") or "en"})
        result = await run_repurpose_agent(task, source)
        words = (result.output or {}).get("content", "").strip() if result.success else ""
    except Exception:  # noqa: BLE001 - the original words are kept; the credits go back below
        words = ""
    if words:
        await credits.settle(hold)
        return words
    await credits.release(hold)
    return text
