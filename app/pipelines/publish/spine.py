"""
Shared pieces of the publish lifecycle, used by Publish Now, Schedule, the
approve endpoints and the scheduled-posts worker so they cannot drift apart:

  platform_key()          one way to turn a piece's platform text into the
                          registry key the publish system uses
  parse_schedule_time()   client time -> real UTC datetime (or a plain error)
  to_utc_datetime()       any stored schedule value (datetime or old ISO
                          string) -> UTC datetime
  iso_utc()               datetime -> ISO text ending in Z, for API output
  check_gate()            approval / rejection / needs-review gate
  schedule_blocker()      the checks a piece must pass before it is queued
  promote_approved_intent()  a piece generated with a planned time is queued
                          only once somebody approves it
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import HTTPException

from app.core.config import settings
from app.db.mongo import content_pieces
from app.pipelines.publish.registry import adapter_for, get_publisher
from app.pipelines.publish.token_store import get_token
from app.pipelines.publish.validators import validate_for_platform
from app.platforms.base import import_all, resolve_platform_by_display_value

logger = logging.getLogger(__name__)

# How far in the past a client time may be before it is refused. Covers a
# slow click or a clock a few minutes off, not a date from yesterday.
SCHEDULE_PAST_GRACE = timedelta(minutes=5)


# ─────────────────────────────────────────────────────────────────────────────
# PLATFORM IDENTITY
# ─────────────────────────────────────────────────────────────────────────────

def platform_key(value: Optional[str]) -> str:
    """The registry key ("twitter") for a piece's platform text ("Twitter/X",
    "Twitter/X Thread", "LinkedIn"). Falls back to the old behaviour (lowercase
    the text) when the registry does not know it, so nothing that worked with
    a plain lowercase key stops working."""
    if not value:
        return ""
    try:
        import_all()
        definition = resolve_platform_by_display_value(value)
    except Exception:  # noqa: BLE001
        definition = None
    return definition.key if definition else value.lower()


# ─────────────────────────────────────────────────────────────────────────────
# TIMES
# ─────────────────────────────────────────────────────────────────────────────

def to_utc_datetime(value: Any) -> Optional[datetime]:
    """A stored schedule value as a timezone-aware UTC datetime. Accepts the
    real datetimes written now (MongoDB hands them back without a zone, they
    are UTC) and the ISO strings older rows carry. None if it is neither."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    return None


def iso_utc(value: Any) -> Any:
    """ISO text ending in Z for a datetime or old ISO string; anything else is
    returned unchanged. Keeps the API's shape (a string) now that times are
    stored as real datetimes."""
    parsed = to_utc_datetime(value)
    if parsed is None:
        return value
    return parsed.isoformat().replace("+00:00", "Z")


def normalize_piece_dates(piece: dict) -> dict:
    """In place: the schedule fields of a piece read from the database, as ISO
    text ending in Z (see iso_utc). Returns the same dict."""
    for field in ("publish_scheduled_at", "intended_publish_at"):
        if piece.get(field):
            piece[field] = iso_utc(piece[field])
    return piece


def parse_schedule_time(raw: str) -> datetime:
    """Client time (ISO, with or without an offset) as a UTC datetime.
    Raises HTTPException 422 with a plain message when it is unreadable or
    more than SCHEDULE_PAST_GRACE in the past."""
    parsed = to_utc_datetime(raw)
    if parsed is None:
        raise HTTPException(status_code=422, detail="That date and time isn't valid. Pick it again.")
    if parsed < datetime.now(timezone.utc) - SCHEDULE_PAST_GRACE:
        raise HTTPException(status_code=422, detail="That time has already passed. Pick a time in the future.")
    return parsed


# ─────────────────────────────────────────────────────────────────────────────
# GATE
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class GateBlock:
    code: str       # NOT_APPROVED | REJECTED | NEEDS_REVIEW | PLATFORM_PAUSED | PLATFORM_RETIRED | PLATFORM_NOT_AVAILABLE
    message: str

    def http(self) -> HTTPException:
        return HTTPException(status_code=409, detail={"code": self.code, "message": self.message})


async def availability_block(platform_text: str, workspace_id: str) -> Optional[GateBlock]:
    """A block when Ops has paused or retired the platform, or has not opened it to this workspace. A platform
    the code cannot publish to at all is left to the usual "not supported yet" checks, so those messages stay as they were."""
    from app.pipelines.platform_ops.availability import platform_availability

    slug = platform_key(platform_text)
    result = await platform_availability(slug, workspace_id)
    from app.platforms.base import get_platform

    definition = get_platform(slug)
    name = definition.label if definition else platform_text
    if result.value == "paused":
        return GateBlock("PLATFORM_PAUSED", f"{name} is paused by Recast for now. Nothing was cancelled. Try again once it is back.")
    if result.value == "retired":
        return GateBlock("PLATFORM_RETIRED", f"{name} is no longer available. You can still copy your post.")
    if result.value == "hidden" and result.reason not in ("no_code", "unknown_platform"):
        return GateBlock("PLATFORM_NOT_AVAILABLE", f"{name} isn't available for this workspace yet.")
    return None


def review_reason(piece: dict) -> Optional[str]:
    """Why a piece needs a second look before it goes out, or None."""
    if piece.get("flagged_for_review"):
        return "This post was flagged for review."
    if piece.get("quality_passed") is False:
        return "This post didn't pass the quality check."
    if any((m or {}).get("qa_flagged") for m in (piece.get("media") or [])):
        return "The attached picture was flagged for review."
    return None


def check_gate(
    piece: dict, *, confirm_anyway: bool = False, honour_recorded_override: bool = False,
) -> Optional[GateBlock]:
    """None when the piece may go out, otherwise why not.

    Rejected is always blocked (it has to be approved again). Not approved is
    blocked. Flagged / not quality-passed / flagged media is blocked unless the
    caller confirms "publish anyway", or (worker only) a person already did when
    scheduling. PUBLISH_REQUIRE_APPROVAL=False lifts all of it."""
    if not settings.PUBLISH_REQUIRE_APPROVAL:
        return None
    status = piece.get("approval_status") or "pending"
    if status == "rejected":
        return GateBlock("REJECTED", "This post was rejected. Approve it again before it can go out.")
    if status != "approved":
        return GateBlock("NOT_APPROVED", "Approve this post before it can go out.")
    reason = review_reason(piece)
    if reason:
        confirmed = confirm_anyway or (honour_recorded_override and bool(piece.get("publish_override_at")))
        if not confirmed:
            return GateBlock("NEEDS_REVIEW", f"{reason} Check it, then choose Publish anyway to send it.")
    return None


# Platforms whose publisher posts several pictures at once, and how many it takes. Every other platform posts the first
# attached picture only (their multi-picture posts work differently and are not built).
MULTI_PICTURE_LIMITS = {"bluesky": 4, "instagram": 10}


async def media_for_publish(piece: dict, workspace_id: str, platform: str) -> list[dict]:
    """The pictures and files to send with a post: what is on the post, plus the other attached pictures when the platform
    posts several at once. A post with one attachment (or a platform that takes one) gets exactly its own media, as before."""
    base = list(piece.get("media") or [])
    limit = MULTI_PICTURE_LIMITS.get(platform_key(platform))
    first_kind = base[0].get("kind") if base else None
    first_kind = getattr(first_kind, "value", first_kind)
    if not limit or not base or first_kind != "image":
        return base
    primary_id = base[0].get("id")
    extra_ids = [a["media_id"] for a in (piece.get("attachments") or []) if a.get("media_id") and a["media_id"] != primary_id]
    if not extra_ids:
        return base
    from app.db.mongo import media_assets

    docs = await media_assets.find({"id": {"$in": extra_ids}, "workspace_id": workspace_id, "kind": "image"}, {"_id": 0}).to_list(length=limit)
    by_id = {d["id"]: d for d in docs}
    ordered = [by_id[i] for i in extra_ids if i in by_id]
    return (base + ordered)[:limit]


def planned_media_note(piece: dict) -> Optional[str]:
    """A campaign planned a picture for this post, it could not be made, and the post has none: say so when it goes out without
    one (media_status is written by the campaign run; nothing used to read it at publish)."""
    if (piece.get("media_status") or {}).get("image") == "failed" and not piece.get("media"):
        return "The picture planned for this post could not be made, so it went out without one."
    return None


def extra_media_note(piece: dict, platform: str = "") -> Optional[str]:
    """A plain note when a post has more pictures attached than were sent. Posting several at once is built for the platforms in
    MULTI_PICTURE_LIMITS; everywhere else only the first goes out, and the member is told, not left to find out."""
    count = len([a for a in (piece.get("attachments") or []) if a.get("media_id")])
    if count <= 1:
        return None
    limit = MULTI_PICTURE_LIMITS.get(platform_key(platform)) if platform else None
    if limit:
        if count > limit:
            return f"Only the first {limit} of {count} attached pictures were posted. This platform takes {limit} at a time."
        return None
    return f"Only the first of {count} attached pictures was posted. This platform posts one picture at a time."


async def record_override(piece: dict, workspace_id: str, user_id: str) -> None:
    """A person chose "publish anyway" for a piece that needed review: keep who
    and when on the piece, and a row in the Activity Log."""
    if not review_reason(piece):
        return
    now = datetime.now(timezone.utc)
    await content_pieces.update_one(
        {"piece_id": piece["piece_id"], "workspace_id": workspace_id},
        {"$set": {"publish_override_by": user_id, "publish_override_at": now}},
    )
    try:
        from app.shared.activity import record_system
        from app.shared.activity.projector import platform_name
        await record_system(
            workspace_id=workspace_id,
            key=f"publish-override:{piece['piece_id']}",
            actor_name="",
            actor_user_id=user_id,
            category="post_published",
            title=f"{platform_name(platform_key(piece.get('platform')))} post sent despite a review flag",
            description=f"{review_reason(piece)} A team member chose to publish anyway.",
            status="warning",
            channel=platform_key(piece.get("platform")),
            target_id=piece["piece_id"],
            target_type="Draft Post",
            href="/dashboard/drafts",
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("publish override log failed for %s: %s", piece.get("piece_id"), exc)


# ─────────────────────────────────────────────────────────────────────────────
# SCHEDULING CHECKS
# ─────────────────────────────────────────────────────────────────────────────

async def schedule_blocker(piece: dict, workspace_id: str) -> Optional[tuple[int, str]]:
    """(status code, plain message) for the first reason this piece cannot be
    queued for later, or None. Platforms that refuse a post with no media would
    fail at the scheduled time when nobody is watching, so they are refused
    now."""
    platform = piece["platform"]
    slug = platform_key(platform)

    unavailable = await availability_block(platform, workspace_id)
    if unavailable:
        return 409, unavailable.message

    # A webhook or manual-handoff platform with saved settings has no sign in, so "connected" means its settings
    # are complete. Every real publisher is checked exactly as before.
    adapter = None
    real_publisher = True
    try:
        get_publisher(slug)
    except ValueError:
        real_publisher = False
        adapter = await adapter_for(slug, workspace_id)
    if adapter is not None:
        if not adapter.is_manual and not (adapter.config.get("secrets") or {}).get("webhook_url"):
            return 400, f"{platform} has no webhook address set yet. Ask the person who runs Recast to finish setting it up."
    else:
        if not await get_token(workspace_id, slug):
            return 400, f"{platform} is not connected. Connect it in Settings before scheduling."
        if not real_publisher:
            return 400, f"Publishing to {platform} isn't supported yet."
    is_valid, issues = validate_for_platform(slug, piece["content"])
    if not is_valid:
        return 400, f"Content validation failed: {'; '.join(issues)}"

    kinds = {str(m.get("kind")) for m in (piece.get("media") or [])}
    if slug == "instagram" and not kinds:
        return 400, "Instagram posts need an image or video. Add one first."
    if slug == "youtube" and "video" not in kinds:
        return 400, "YouTube posts need a video. Add one first."
    return None


async def promote_approved_intent(piece: dict, workspace_id: str) -> Optional[str]:
    """Called right after a piece is approved. A piece generated with a planned
    time only recorded the intent; this is where it becomes a real queued post.

    Returns "queued" when it was queued, "note" when it stayed pending with a
    plain note on the piece (time passed, platform not connected, needs review),
    or None when the piece had no planned time or is not waiting."""
    intended = to_utc_datetime(piece.get("intended_publish_at"))
    if intended is None or piece.get("publish_status") not in (None, "pending"):
        return None

    async def _note(text: str) -> str:
        await content_pieces.update_one(
            {"piece_id": piece["piece_id"], "workspace_id": workspace_id},
            {"$set": {"schedule_note": text, "updated_at": datetime.now(timezone.utc)}},
        )
        return "note"

    if intended <= datetime.now(timezone.utc):
        return await _note("The planned time had already passed when this was approved. Pick a new time to schedule it.")

    blocker = await schedule_blocker(piece, workspace_id)
    if blocker:
        return await _note(f"Not scheduled yet. {blocker[1]}")
    gate = check_gate({**piece, "approval_status": "approved"})
    if gate:
        return await _note(f"Not scheduled yet. {gate.message}")

    await content_pieces.update_one(
        {"piece_id": piece["piece_id"], "workspace_id": workspace_id, "publish_status": {"$in": [None, "pending"]}},
        {
            "$set": {
                "publish_status": "queued",
                "publish_scheduled_at": intended,
                "publish_target": piece.get("publish_target") or platform_key(piece.get("platform")),
                "updated_at": datetime.now(timezone.utc),
            },
            "$unset": {"schedule_note": ""},
        },
    )
    return "queued"
