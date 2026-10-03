"""Media for a campaign's posts: the audio and images a member asked for on the campaign.

Runs after a day's text is saved. It never raises: media is an extra on top of the text, so a
failed narration or image is logged and counted, and the text and every other post are kept.
Each asset carries `source_piece_id`, which is how a post is tied to its media.

Audio is one narration per post (the same script voiced again would be an identical file);
`count_per_post` is how many images each post gets. Video is stored in the plan but is not made yet.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.workspace import WorkspaceContext
from app.pipelines.media import duration
from app.db.mongo import audio_assets, content_pieces, image_assets, users, workspace_members, workspaces
from app.pipelines.publish.attachments import attach_generated_image

logger = logging.getLogger(__name__)

GENERATED_KINDS = ("image", "audio")  # video is wired in the plan, not generated yet
MAX_POSTS_WITH_MEDIA_PER_RUN = 30


class _NoRun:
    async def step(self, label: str) -> None:
        return None


def wanted_kinds(media_plan: dict | None) -> list[str]:
    """The kinds that will really be made for this plan (empty when media is off)."""
    plan = media_plan or {}
    if not plan.get("enabled"):
        return []
    return [k for k in GENERATED_KINDS if k in (plan.get("kinds") or [])]


def estimate(media_plan: dict | None, posts: int) -> dict[str, int]:
    """How many files a run of `posts` posts will make, so the member can see it before starting."""
    plan = media_plan or {}
    kinds = wanted_kinds(plan)
    return {
        "images": posts * int(plan.get("count_per_post", 1)) if "image" in kinds else 0,
        "audio": posts if "audio" in kinds else 0,
    }


def _headline(content: str) -> str:
    for line in (content or "").splitlines():
        line = line.strip().lstrip("#*- ").strip()
        if line:
            return line[:90]
    return "Post"


async def _context(workspace_id: str, user_id: str) -> WorkspaceContext:
    workspace = await workspaces.find_one({"id": workspace_id}) or {"id": workspace_id}
    member = await workspace_members.find_one({"workspace_id": workspace_id, "user_id": user_id}) or {
        "workspace_id": workspace_id, "user_id": user_id, "role": "owner", "status": "active",
    }
    user = await users.find_one({"id": user_id}) or {"id": user_id}
    return WorkspaceContext(workspace, member, user)


async def _image_request(campaign: dict[str, Any], piece: dict):
    from app.api.v1.image_assets import GenerateImageAssetRequest
    from app.pipelines.media.headline import make_headline

    plan = campaign.get("media_plan") or {}
    count = int(plan.get("count_per_post", 1))
    image_plan = plan.get("image") or {}
    layout = image_plan.get("layout") or "quote_1_1"
    content = (piece.get("content") or "").strip()
    title = _headline(content)  # the asset's name in the library
    show_text = image_plan.get("text", "headline") != "none"
    # The headline is written in the language the post was written in (it used to always be English).
    from app.pipelines.text.generator import resolve_language_name

    language = (piece.get("language") or "").strip()
    language_name = resolve_language_name(language) if language and language != "en" else "English"
    headline = (await make_headline(content, language_name=language_name)) if show_text else ""
    return GenerateImageAssetRequest(
        title=title, brand_id=campaign["brand_id"], headline=headline, show_text=show_text,
        show_logo=image_plan.get("logo", True), show_mascot=bool(image_plan.get("mascot", False)),
        source_piece_id=piece["piece_id"], count=min(count, 5), active_layout=layout,
    )


async def _make_for_piece(
    campaign: dict[str, Any], piece: dict, kinds: list[str], ctx: WorkspaceContext,
) -> dict[str, str]:
    """Make the planned media for one post. Returns {kind: "ready" | "failed"} for each kind tried."""
    # imported here: these modules import the whole API layer
    from app.api.v1.audio_assets import GenerateAudioAssetRequest, create_audio_from_script
    from app.api.v1.image_assets import GenerateImageAssetRequest, create_image_asset

    plan = campaign.get("media_plan") or {}
    count = int(plan.get("count_per_post", 1))
    audio_opts = plan.get("audio") or {}
    layout = (plan.get("image") or {}).get("layout") or "quote_1_1"
    content = (piece.get("content") or "").strip()
    title = _headline(content)
    states: dict[str, str] = {}
    if "image" in kinds:
        try:
            made = await create_image_asset(await _image_request(campaign, piece), ctx)
            states["image"] = "ready"
            await image_assets.update_one({"id": getattr(made, "id", ""), "workspace_id": ctx.workspace_id}, {"$set": {"campaign_id": campaign["id"]}})
            # The picture goes onto its post, so it is what gets published.
            await attach_generated_image(piece["piece_id"], ctx.workspace_id, ctx.user_id, getattr(made, "id", ""))
        except Exception as exc:  # noqa: BLE001 - media must never fail the text
            states["image"] = "failed"
            logger.warning("Campaign %s: image for piece %s failed: %s", campaign["id"], piece["piece_id"], exc)
    if "audio" in kinds:
        try:
            wpm = audio_opts.get("words_per_minute")
            script = content
            if audio_opts.get("max_seconds"):
                script = duration.trim_to_seconds(content, audio_opts["max_seconds"], wpm)
            # A re-run for the same post and the same words reuses the recording already made, instead of voicing it again
            # (which spends provider quota and leaves a second identical recording).
            already = await audio_assets.find_one(
                {"workspace_id": ctx.workspace_id, "campaign_id": campaign["id"], "source_piece_id": piece["piece_id"], "script": script},
                {"id": 1},
            )
            if not already:
                made_audio = await create_audio_from_script(
                    GenerateAudioAssetRequest(
                        title=title, brand_id=campaign["brand_id"], source_piece_id=piece["piece_id"],
                        script=script, words_per_minute=wpm,
                    ),
                    ctx,
                    _NoRun(),
                )
                await audio_assets.update_one({"id": made_audio.id, "workspace_id": ctx.workspace_id}, {"$set": {"campaign_id": campaign["id"]}})
            states["audio"] = "ready"
        except Exception as exc:  # noqa: BLE001
            states["audio"] = "failed"
            logger.warning("Campaign %s: audio for piece %s failed: %s", campaign["id"], piece["piece_id"], exc)
    return states


async def _record_status(workspace_id: str, piece_id: str, states: dict[str, str]) -> None:
    """Saved on the post so the stage matrix can show ready or failed for each kind."""
    if not states:
        return
    await content_pieces.update_one(
        {"workspace_id": workspace_id, "piece_id": piece_id},
        {"$set": {f"media_status.{kind}": state for kind, state in states.items()}},
    )


async def generate_media_for_pieces(
    campaign: dict[str, Any], piece_ids: list[str], *, workspace_id: str, user_id: str,
) -> dict[str, int]:
    """Make the planned media for each of `piece_ids`. Returns counts of what was made and what failed."""
    kinds = wanted_kinds(campaign.get("media_plan"))
    outcome = {"images": 0, "audio": 0, "failed": 0}
    if not kinds or not piece_ids:
        return outcome

    ctx = await _context(workspace_id, user_id)
    count = int((campaign.get("media_plan") or {}).get("count_per_post", 1))
    pieces = await content_pieces.find(
        {"workspace_id": workspace_id, "piece_id": {"$in": piece_ids}, "deleted": {"$ne": True}},
    ).to_list(length=None)

    for piece in pieces[:MAX_POSTS_WITH_MEDIA_PER_RUN]:
        if not (piece.get("content") or "").strip():
            continue
        states = await _make_for_piece(campaign, piece, kinds, ctx)
        await _record_status(workspace_id, piece["piece_id"], states)
        outcome["images"] += count if states.get("image") == "ready" else 0
        outcome["audio"] += 1 if states.get("audio") == "ready" else 0
        outcome["failed"] += sum(1 for v in states.values() if v == "failed")
    return outcome


async def regenerate_image_for_piece(
    campaign: dict[str, Any], piece_id: str, *, workspace_id: str, user_id: str,
) -> dict[str, Any]:
    """Make this post's picture again, even though one exists. The new picture replaces the old one on the
    post; the old one is kept in the media library, marked as replaced. A new card with no AI picture never
    replaces a real picture: the old one stays and the reason is returned.

    Returns {"state": "missing" | "failed" | "kept" | "card" | "ready", "note": str | None}."""
    from app.api.v1.image_assets import create_image_asset
    from app.db.mongo import image_assets, media_assets

    piece = await content_pieces.find_one({"workspace_id": workspace_id, "piece_id": piece_id, "deleted": {"$ne": True}})
    if not piece or piece.get("campaign_id") != campaign["id"] or not (piece.get("content") or "").strip():
        return {"state": "missing", "note": None}
    if "image" not in wanted_kinds(campaign.get("media_plan")):
        return {"state": "missing", "note": "This campaign is not set up to make pictures."}

    ctx = await _context(workspace_id, user_id)
    current = {"workspace_id": workspace_id, "source_piece_id": piece_id, "replaced_by": {"$exists": False}}
    old = await image_assets.find(current).to_list(length=None)
    old_ids = [d["id"] for d in old]
    old_media = [s["media_id"] for d in old for s in d.get("slides", []) if s.get("media_id")]
    old_real = bool(old_media) and await media_assets.count_documents({"id": {"$in": old_media}, "qa_flagged": {"$ne": True}}) > 0

    try:
        new = await create_image_asset(await _image_request(campaign, piece), ctx)
    except Exception as exc:  # noqa: BLE001 - never fail the post
        logger.warning("Campaign %s: regenerate image for piece %s failed: %s", campaign["id"], piece_id, exc)
        if not old_ids:
            await _record_status(workspace_id, piece_id, {"image": "failed"})
        return {"state": "failed", "note": "The picture could not be made. Try again in a moment."}

    new_media = [s.media_id for s in new.slides if s.media_id]
    flagged_docs = await media_assets.find({"id": {"$in": new_media}, "qa_flagged": True}).to_list(length=None)
    new_real = not flagged_docs
    note = (flagged_docs[0].get("qa_flag_reason") if flagged_docs else None)

    if old_real and not new_real:
        # keep the real picture; hide the card that was just made
        await image_assets.update_one({"id": new.id}, {"$set": {"replaced_by": "kept_previous"}})
        return {"state": "kept", "note": note}

    if old_ids:
        await image_assets.update_many({"id": {"$in": old_ids}}, {"$set": {"replaced_by": new.id}})
    # The new picture replaces the old one on the post itself, not only in the library.
    await attach_generated_image(piece_id, workspace_id, user_id, new.id, replace_ids=old_ids)
    await _record_status(workspace_id, piece_id, {"image": "ready"})
    return {"state": "ready" if new_real else "card", "note": note}


async def retry_media_for_piece(
    campaign: dict[str, Any], piece_id: str, *, workspace_id: str, user_id: str,
) -> dict[str, str]:
    """Try again only the kinds that failed for this post. Returns the new state of each kind tried."""
    piece = await content_pieces.find_one({"workspace_id": workspace_id, "piece_id": piece_id, "deleted": {"$ne": True}})
    if not piece or piece.get("campaign_id") != campaign["id"] or not (piece.get("content") or "").strip():
        return {}
    failed = [k for k, v in (piece.get("media_status") or {}).items() if v == "failed" and k in GENERATED_KINDS]
    kinds = [k for k in wanted_kinds(campaign.get("media_plan")) if k in failed]
    if not kinds:
        return {}
    ctx = await _context(workspace_id, user_id)
    states = await _make_for_piece(campaign, piece, kinds, ctx)
    await _record_status(workspace_id, piece_id, states)
    return states
