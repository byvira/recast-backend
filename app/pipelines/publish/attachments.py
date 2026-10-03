"""
Attaching images, audio and video to a post.

A post (content_pieces row) holds ``attachments``, a list of PieceAttachment
dicts. The first one is the primary: its media is also copied onto
``piece.media`` (the same MediaAsset snapshot PATCH /content/pieces/{id}/media
writes), which is what the publishers read, so publishing needs no change.

Each asset an attachment points at (image_assets / audio_assets) also carries a
``linked_pieces`` list of {piece_id, attached_at}, so a post's results can later
roll up to the assets on it.

Used by the /content/pieces/{id}/attachments routes, the audio send-to-draft
route, PATCH /content/pieces/{id}/media and campaign media generation, so every
way of getting a picture or video onto a post leaves the same record.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from app.db.mongo import audio_assets, content_pieces, image_assets, media_assets
from app.models.media import MediaAsset

logger = logging.getLogger(__name__)

LOCKED_STATUSES = ("publishing", "published")
AUDIO_NOTE = "Audio can't be posted on its own. Render it as a video and attach that."


def attach_advice(resolved: dict, piece: dict) -> list[str]:
    """Plain sentences about whether this attachment suits the post's platform, said when it is attached and not found out when
    it is refused. Advice only: nothing here blocks an attachment. Platform limits as the platforms publish them; not checked
    against a live account."""
    from app.pipelines.media import video_presets
    from app.pipelines.publish.spine import platform_key

    notes: list[str] = []
    if resolved["asset_type"] == "video":
        clip = next((c for c in (resolved.get("asset") or {}).get("video_clips") or [] if c.get("id") == resolved.get("clip_id")), None)
        if clip:
            length = float(clip.get("end_s") or 0) - float(clip.get("start_s") or 0)
            notes += video_presets.advice(clip.get("platform"), clip.get("size") or "square", length)
    if resolved["asset_type"] == "image" and platform_key(piece.get("platform")) == "instagram":
        media = resolved.get("media") or {}
        width, height = media.get("width"), media.get("height")
        if width and height:
            ratio = width / height
            if ratio < 0.79 or ratio > 1.92:
                notes.append(
                    f"Instagram feed pictures need a shape between 4:5 (tall) and 1.91:1 (wide). This one is {width} by {height}, "
                    "so it may be cropped or refused. A square or 4:5 layout fits."
                )
    return notes


class AttachmentError(Exception):
    """A refusal with a plain message and the HTTP status it maps to."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ─────────────────────────────────────────────────────────────────────────────
# RESOLVING AN ASSET
# ─────────────────────────────────────────────────────────────────────────────

async def _media_or_error(media_id: str, workspace_id: str, what: str) -> dict:
    doc = await media_assets.find_one({"id": media_id, "workspace_id": workspace_id})
    if not doc:
        raise AttachmentError(422, f"The file for this {what} is missing.")
    return doc


async def resolve_asset(
    workspace_id: str,
    asset_type: str,
    *,
    asset_id: Optional[str] = None,
    slide_number: Optional[int] = None,
    clip_id: Optional[str] = None,
    media_id: Optional[str] = None,
) -> dict:
    """What an attachment would point at right now, checked inside the
    workspace. Raises AttachmentError (404 for an unknown asset, 422 when it has
    nothing to attach yet)."""
    if asset_type == "image":
        if not asset_id:
            raise AttachmentError(422, "Choose an image to attach.")
        doc = await image_assets.find_one({"id": asset_id, "workspace_id": workspace_id})
        if not doc:
            raise AttachmentError(404, "Image not found.")
        slides = doc.get("slides") or []
        if slide_number is None:
            slide = next((s for s in slides if s.get("media_id")), None)
        else:
            slide = next((s for s in slides if s.get("slide_number") == slide_number), None)
            if slide is None:
                raise AttachmentError(404, "That slide wasn't found.")
        if not slide or not slide.get("media_id"):
            raise AttachmentError(422, "That slide has no picture yet.")
        media = await _media_or_error(slide["media_id"], workspace_id, "picture")
        return {
            "asset_type": "image", "asset_id": asset_id, "asset_version": doc.get("version_count", 1),
            "media_id": slide["media_id"], "slide_number": slide.get("slide_number"), "clip_id": None,
            "qa_flagged": bool(doc.get("qa_flagged") or media.get("qa_flagged")), "media": media, "asset": doc,
        }

    if asset_type == "audio":
        if not asset_id:
            raise AttachmentError(422, "Choose a recording to attach.")
        doc = await audio_assets.find_one({"id": asset_id, "workspace_id": workspace_id})
        if not doc:
            raise AttachmentError(404, "Recording not found.")
        current = doc.get("approved_master_media_id") or doc.get("media_id")
        if not current:
            raise AttachmentError(422, "This recording has no audio file yet.")
        media = await _media_or_error(current, workspace_id, "recording")
        return {
            "asset_type": "audio", "asset_id": asset_id, "asset_version": doc.get("version_count", 1),
            "media_id": current, "slide_number": None, "clip_id": None,
            "qa_flagged": bool(doc.get("qa_flagged") or media.get("qa_flagged")), "media": media, "asset": doc,
        }

    if asset_type == "video":
        if not clip_id:
            raise AttachmentError(422, "Choose a video to attach.")
        query: dict = {"workspace_id": workspace_id, "video_clips.id": clip_id}
        if asset_id:
            query["id"] = asset_id
        doc = await audio_assets.find_one(query)
        clip = next((c for c in (doc or {}).get("video_clips") or [] if c.get("id") == clip_id), None)
        if not doc or not clip:
            raise AttachmentError(404, "Video not found.")
        media = await _media_or_error(clip["media_id"], workspace_id, "video")
        return {
            "asset_type": "video", "asset_id": doc["id"], "asset_version": None,
            "media_id": clip["media_id"], "slide_number": None, "clip_id": clip_id,
            "qa_flagged": bool(media.get("qa_flagged")), "media": media, "asset": doc,
        }

    if asset_type == "upload":
        if not media_id:
            raise AttachmentError(422, "Choose a file to attach.")
        media = await media_assets.find_one({"id": media_id, "workspace_id": workspace_id})
        if not media:
            raise AttachmentError(404, "Media not found.")
        return {
            "asset_type": "upload", "asset_id": None, "asset_version": None,
            "media_id": media_id, "slide_number": None, "clip_id": None,
            "qa_flagged": bool(media.get("qa_flagged")), "media": media, "asset": None,
        }

    raise AttachmentError(422, "That kind of attachment isn't supported.")


def media_snapshot(media_doc: dict, qa_flagged: bool = False, alt_text: Optional[str] = None) -> dict:
    """The MediaAsset copy kept on piece.media (same shape PATCH .../media writes). `alt_text` is the asset's own description
    of the picture, carried so a platform that takes alt text gets it."""
    snap = MediaAsset(**media_doc).model_dump()
    if qa_flagged:
        snap["qa_flagged"] = True
    if alt_text:
        snap["alt_text"] = alt_text
    return snap


def _same_target(att: dict, resolved: dict) -> bool:
    if att.get("asset_type") != resolved["asset_type"]:
        return False
    if resolved["asset_type"] == "upload":
        return att.get("media_id") == resolved["media_id"]
    if att.get("asset_id") != resolved["asset_id"]:
        return False
    return att.get("slide_number") == resolved["slide_number"] and att.get("clip_id") == resolved["clip_id"]


def _target_match(resolved: dict) -> dict:
    """The attachment fields that make two attachments "the same thing", for Mongo."""
    if resolved["asset_type"] == "upload":
        return {"asset_type": "upload", "media_id": resolved["media_id"]}
    return {
        "asset_type": resolved["asset_type"], "asset_id": resolved["asset_id"],
        "slide_number": resolved["slide_number"], "clip_id": resolved["clip_id"],
    }


def _locked(piece: dict) -> bool:
    return piece.get("publish_status") in LOCKED_STATUSES


def _backlink_collection(asset_type: str):
    return image_assets if asset_type == "image" else audio_assets if asset_type in ("audio", "video") else None


async def _add_backlink(asset_type: str, asset_id: Optional[str], piece_id: str, workspace_id: str, when: datetime) -> None:
    coll = _backlink_collection(asset_type)
    if coll is None or not asset_id:
        return
    await coll.update_one(
        {"id": asset_id, "workspace_id": workspace_id, "linked_pieces.piece_id": {"$ne": piece_id}},
        {"$addToSet": {"linked_pieces": {"piece_id": piece_id, "attached_at": when}}},
    )


async def _drop_backlink_if_unused(att: dict, remaining: list[dict], piece_id: str, workspace_id: str) -> None:
    """Remove the asset's pointer to this post unless another attachment on the
    post still uses the same asset (another slide of the same image, say)."""
    coll = _backlink_collection(att.get("asset_type", ""))
    if coll is None or not att.get("asset_id"):
        return
    family = ("audio", "video") if att["asset_type"] in ("audio", "video") else (att["asset_type"],)
    if any(a.get("asset_id") == att["asset_id"] and a.get("asset_type") in family for a in remaining):
        return
    await coll.update_one(
        {"id": att["asset_id"], "workspace_id": workspace_id},
        {"$pull": {"linked_pieces": {"piece_id": piece_id}}},
    )


# ─────────────────────────────────────────────────────────────────────────────
# ATTACH / DETACH / REFRESH
# ─────────────────────────────────────────────────────────────────────────────

async def attach(piece: dict, workspace_id: str, user_id: str, resolved: dict) -> tuple[dict, bool]:
    """Attach a resolved asset to a post. Returns (attachment, created); attaching
    the same asset again returns the attachment already there."""
    existing = piece.get("attachments") or []
    for att in existing:
        if _same_target(att, resolved):
            return att, False
    if _locked(piece):
        raise AttachmentError(409, "This post is already published or being published, so its attachments can't change.")

    now = datetime.now(timezone.utc)
    attachment = {
        "id": uuid4().hex,
        "asset_type": resolved["asset_type"],
        "asset_id": resolved["asset_id"],
        "asset_version": resolved["asset_version"],
        "media_id": resolved["media_id"],
        "slide_number": resolved["slide_number"],
        "clip_id": resolved["clip_id"],
        "piece_version": piece.get("version_count", 1),
        "attached_at": now,
        "attached_by": user_id,
        "refreshed_at": None,
    }
    update: dict = {"$push": {"attachments": attachment}, "$set": {"updated_at": now}}
    if not existing:
        # The first attachment is the primary one: it is what gets published.
        update["$set"]["media"] = [media_snapshot(resolved["media"], resolved["qa_flagged"], alt_text=((resolved.get("asset") or {}).get("alt_text") if resolved["asset_type"] == "image" else None))]
    # The same-target check is repeated inside the update, so two requests at the
    # same moment cannot both add it.
    result = await content_pieces.update_one(
        {"piece_id": piece["piece_id"], "workspace_id": workspace_id, "attachments": {"$not": {"$elemMatch": _target_match(resolved)}}},
        update,
    )
    if result.matched_count == 0:
        latest = await content_pieces.find_one({"piece_id": piece["piece_id"], "workspace_id": workspace_id}) or {}
        for att in latest.get("attachments") or []:
            if _same_target(att, resolved):
                return att, False
        raise AttachmentError(404, "Piece not found.")
    await _add_backlink(resolved["asset_type"], resolved["asset_id"], piece["piece_id"], workspace_id, now)
    return attachment, True


async def detach(piece: dict, workspace_id: str, attachment_id: str) -> None:
    if _locked(piece):
        raise AttachmentError(409, "This post is already published or being published, so its attachments can't change.")
    existing = piece.get("attachments") or []
    target = next((a for a in existing if a.get("id") == attachment_id), None)
    if target is None:
        raise AttachmentError(404, "That attachment isn't on this post.")
    remaining = [a for a in existing if a.get("id") != attachment_id]

    updates: dict = {"attachments": remaining, "updated_at": datetime.now(timezone.utc)}
    if existing and existing[0].get("id") == attachment_id:
        # The primary one went: the next one (if any) becomes what gets published.
        media: list[dict] = []
        if remaining:
            doc = await media_assets.find_one({"id": remaining[0]["media_id"], "workspace_id": workspace_id})
            if doc:
                media = [media_snapshot(doc)]
        updates["media"] = media
    await content_pieces.update_one({"piece_id": piece["piece_id"], "workspace_id": workspace_id}, {"$set": updates})
    await _drop_backlink_if_unused(target, remaining, piece["piece_id"], workspace_id)


async def refresh(piece: dict, workspace_id: str, asset_id: Optional[str] = None) -> tuple[list[dict], list[str]]:
    """Re-snapshot attachments to their asset's current version. Returns
    (attachments, problems): an attachment whose asset can no longer be found is
    left as it was and named in problems."""
    if _locked(piece):
        raise AttachmentError(409, "This post is already published or being published, so its attachments can't change.")
    existing = [dict(a) for a in (piece.get("attachments") or [])]
    problems: list[str] = []
    now = datetime.now(timezone.utc)
    primary_media: Optional[dict] = None
    for index, att in enumerate(existing):
        if att.get("asset_type") == "upload" or (asset_id and att.get("asset_id") != asset_id):
            continue
        try:
            resolved = await resolve_asset(
                workspace_id, att["asset_type"], asset_id=att.get("asset_id"),
                slide_number=att.get("slide_number"), clip_id=att.get("clip_id"),
            )
        except AttachmentError as exc:
            problems.append(exc.message)
            continue
        att.update({
            "asset_version": resolved["asset_version"], "media_id": resolved["media_id"],
            "piece_version": piece.get("version_count", 1), "refreshed_at": now,
        })
        if index == 0:
            primary_media = media_snapshot(resolved["media"], resolved["qa_flagged"], alt_text=((resolved.get("asset") or {}).get("alt_text") if resolved["asset_type"] == "image" else None))
    updates: dict = {"attachments": existing, "updated_at": now}
    if primary_media is not None:
        updates["media"] = [primary_media]
    await content_pieces.update_one({"piece_id": piece["piece_id"], "workspace_id": workspace_id}, {"$set": updates})
    return existing, problems


# ─────────────────────────────────────────────────────────────────────────────
# READING (with stale tracking)
# ─────────────────────────────────────────────────────────────────────────────

def _stale(att: dict, asset: Optional[dict], piece: dict) -> tuple[bool, Optional[str]]:
    kind = att.get("asset_type")
    if kind == "upload":
        return False, None
    if asset is None:
        return True, "asset_changed"

    changed = False
    if kind == "image":
        slide = next((s for s in asset.get("slides") or [] if s.get("slide_number") == att.get("slide_number")), None)
        current = (slide or {}).get("media_id")
        changed = current != att.get("media_id")
    elif kind == "audio":
        changed = (asset.get("approved_master_media_id") or asset.get("media_id")) != att.get("media_id")
    elif kind == "video":
        clip = next((c for c in asset.get("video_clips") or [] if c.get("id") == att.get("clip_id")), None)
        changed = clip is None or clip.get("media_id") != att.get("media_id")
    version = att.get("asset_version")
    if kind in ("image", "audio") and version is not None and asset.get("version_count", 1) > version:
        changed = True
    if changed:
        return True, "asset_changed"

    # An image or recording that was generated from this post's text, while the
    # text has been edited since it was attached.
    if (
        kind in ("image", "audio")
        and asset.get("source_piece_id") == piece.get("piece_id")
        and piece.get("version_count", 1) > att.get("piece_version", 1)
    ):
        return True, "text_changed"
    return False, None


async def list_with_status(piece: dict, workspace_id: str) -> list[dict]:
    """The post's attachments, each with ``stale`` / ``stale_reason`` (one of
    "asset_changed", "text_changed", or None) and the file's url and kind."""
    atts = piece.get("attachments") or []
    if not atts:
        return []
    image_ids = {a["asset_id"] for a in atts if a.get("asset_type") == "image" and a.get("asset_id")}
    audio_ids = {a["asset_id"] for a in atts if a.get("asset_type") in ("audio", "video") and a.get("asset_id")}
    images = {d["id"]: d for d in await image_assets.find({"id": {"$in": list(image_ids)}, "workspace_id": workspace_id}).to_list(length=None)} if image_ids else {}
    audios = {d["id"]: d for d in await audio_assets.find({"id": {"$in": list(audio_ids)}, "workspace_id": workspace_id}).to_list(length=None)} if audio_ids else {}
    media_ids = [a["media_id"] for a in atts if a.get("media_id")]
    medias = {d["id"]: d for d in await media_assets.find({"id": {"$in": media_ids}, "workspace_id": workspace_id}).to_list(length=None)}

    out: list[dict] = []
    for att in atts:
        if att.get("asset_type") == "image":
            asset = images.get(att.get("asset_id"))
        elif att.get("asset_type") in ("audio", "video"):
            asset = audios.get(att.get("asset_id"))
        else:
            asset = None
        stale, reason = _stale(att, asset, piece)
        media = medias.get(att.get("media_id")) or {}
        out.append({
            **att, "stale": stale, "stale_reason": reason,
            "media_url": media.get("url"), "media_kind": media.get("kind"),
        })
    return out


# ─────────────────────────────────────────────────────────────────────────────
# PATCH /content/pieces/{id}/media and campaign media
# ─────────────────────────────────────────────────────────────────────────────

async def _describe_media(workspace_id: str, media_id: str) -> tuple[str, dict]:
    """Which attachment a media id corresponds to: a video clip, a recording, a
    picture slide, or (none of those) a plain upload."""
    doc = await audio_assets.find_one({"workspace_id": workspace_id, "video_clips.media_id": media_id})
    if doc:
        clip = next(c for c in doc["video_clips"] if c.get("media_id") == media_id)
        return "video", {"asset_id": doc["id"], "clip_id": clip["id"]}
    doc = await audio_assets.find_one({
        "workspace_id": workspace_id,
        "$or": [{"media_id": media_id}, {"approved_master_media_id": media_id}],
    })
    if doc:
        return "audio", {"asset_id": doc["id"]}
    doc = await image_assets.find_one({"workspace_id": workspace_id, "slides.media_id": media_id})
    if doc:
        slide = next(s for s in doc["slides"] if s.get("media_id") == media_id)
        return "image", {"asset_id": doc["id"], "slide_number": slide.get("slide_number")}
    return "upload", {"media_id": media_id}


async def replace_all_with_media(piece: dict, workspace_id: str, user_id: str, media_id: Optional[str]) -> None:
    """After PATCH .../media has set ``piece.media``: make the attachment list
    match it (empty, or one attachment for that file). A media id that belongs to
    no image, recording or video is recorded as an "upload". Never raises, so the
    PATCH response is unaffected by it."""
    try:
        old = piece.get("attachments") or []
        new: list[dict] = []
        now = datetime.now(timezone.utc)
        if media_id:
            kind, args = await _describe_media(workspace_id, media_id)
            resolved = await resolve_asset(workspace_id, kind, **args)
            new = [{
                "id": uuid4().hex, "asset_type": kind, "asset_id": resolved["asset_id"],
                "asset_version": resolved["asset_version"],
                # exactly the file the caller chose, even if the asset's current file differs
                "media_id": media_id, "slide_number": resolved["slide_number"], "clip_id": resolved["clip_id"],
                "piece_version": piece.get("version_count", 1), "attached_at": now, "attached_by": user_id,
                "refreshed_at": None,
            }]
        await content_pieces.update_one(
            {"piece_id": piece["piece_id"], "workspace_id": workspace_id},
            {"$set": {"attachments": new}},
        )
        for att in old:
            await _drop_backlink_if_unused(att, new, piece["piece_id"], workspace_id)
        for att in new:
            await _add_backlink(att["asset_type"], att["asset_id"], piece["piece_id"], workspace_id, now)
    except Exception as exc:  # noqa: BLE001
        logger.error("Recording the attachment for piece %s failed: %s", piece.get("piece_id"), exc)


async def attach_generated_image(piece_id: str, workspace_id: str, user_id: str, image_asset_id: str, *, replace_ids: Optional[list[str]] = None) -> None:
    """Campaign media: put the picture that was just made onto its post. Drops the
    post's attachments to the images it replaces. Never raises: media is an extra
    on top of the text."""
    try:
        piece = await content_pieces.find_one({"piece_id": piece_id, "workspace_id": workspace_id, "deleted": {"$ne": True}})
        if not piece or _locked(piece):
            return
        for att in list(piece.get("attachments") or []):
            if att.get("asset_type") == "image" and att.get("asset_id") in (replace_ids or []):
                await detach(piece, workspace_id, att["id"])
                piece = await content_pieces.find_one({"piece_id": piece_id, "workspace_id": workspace_id}) or piece
        resolved = await resolve_asset(workspace_id, "image", asset_id=image_asset_id)
        await attach(piece, workspace_id, user_id, resolved)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Attaching the generated picture to piece %s failed: %s", piece_id, exc)
