"""
Publish endpoints — publish now, status.

Scheduling and cancelling a scheduled publish live on app.api.v1.content
(/pieces/{id}/schedule, /pieces/{id}/cancel-schedule) — see the note above
where this router's own schedule/cancel routes used to be.

Workspace-scoped: content pieces and platform tokens are resolved within the
caller's active workspace. Publishing requires the ``publish_content``
permission; reading status requires membership.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from pymongo import ReturnDocument

from app.core.config import settings
from app.core.middleware import limiter
from app.core.notifications import send_templated_email
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.db.mongo import brand_profiles, content_pieces, post_metric_checkpoints, users
from app.models.media import MediaAsset
from app.pipelines.publish.base import PublishRequest
from app.pipelines.publish.registry import adapter_for, get_publisher
from app.pipelines.publish.spine import availability_block, check_gate, extra_media_note, media_for_publish, planned_media_note, platform_key, record_override
from app.pipelines.publish.health import mark_healthy
from app.workers.token_refresh import recover_connection
from app.pipelines.publish.token_store import get_token
from app.pipelines.publish.supervisor.alerts import alert_fatal, save_incident
from app.pipelines.publish.supervisor.classifier import RETRYABLE_CODES, classify_error, ErrorType, failure_code
from app.pipelines.publish.supervisor.retry import get_retry_delay
from app.pipelines.publish.supervisor.fixer import fix_content

router = APIRouter()
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# REQUEST MODELS
# ─────────────────────────────────────────────────────────────────────────────

class PublishNowRequest(BaseModel):
    piece_id: str
    # No separate `platform` field: a piece is always exactly one platform
    # (piece["platform"]), so a caller-supplied platform could previously
    # disagree with the piece's real platform and publish content generated
    # for one network onto a completely different one's API. Derived from
    # the piece server-side instead — see publish_now().
    # YouTube-only — the (possibly user-edited) draft from POST
    # /publish/youtube/prepare. Ignored for every other platform. None means
    # "no review step happened" — YouTubePublisher generates real metadata
    # fresh in that case, never a blank/guessed default.
    youtube_metadata: Optional[dict] = None
    # A flagged or not-quality-passed post is refused unless the member
    # explicitly chooses to send it anyway. Who chose, and when, is kept.
    confirm_publish_anyway: bool = False


class YouTubePrepareRequest(BaseModel):
    piece_id: str


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

async def _get_verified_piece(piece_id: str, workspace_id: str) -> dict:
    """Fetch a piece within the workspace."""
    piece = await content_pieces.find_one(
        {"piece_id": piece_id, "workspace_id": workspace_id, "deleted": {"$ne": True}}
    )
    if not piece:
        raise HTTPException(status_code=404, detail="Piece not found.")
    return piece


async def _claim_piece_for_publishing(piece_id: str, workspace_id: str) -> tuple[dict, Optional[str]]:
    """Atomically transition a piece into "publishing", rejecting the call
    if it's already publishing or published. A piece waiting in "queued" is
    taken too: the scheduled worker claims the same way (queued -> publishing),
    so a Publish Now click and a due schedule can never both post.

    _get_verified_piece + a later plain update_one(..., "publishing") left a
    real race window: two near-simultaneous /publish/now calls (a genuine
    double-click before the frontend's disabled state re-renders, two tabs,
    or a direct API call) could both pass the read, both build a publish
    request, and both call publisher.publish() — for YouTube specifically,
    two real video uploads from one user action. find_one_and_update's
    compare-and-swap is atomic at the database level, so only one caller can
    ever win the claim.

    Returns the piece plus the status it held before the claim, so a call
    that then can't go ahead (platform not connected, unexpected error) can
    put it back with _release_claim instead of leaving it stuck on
    "publishing", which the claim itself refuses to pick up again.
    """
    piece = await content_pieces.find_one_and_update(
        {
            "piece_id": piece_id,
            "workspace_id": workspace_id,
            "deleted": {"$ne": True},
            "publish_status": {"$nin": ["publishing", "published"]},
        },
        {"$set": {
            "publish_status": "publishing",
            # Stamped so a post stuck here can be told apart from one in flight
            # (the scheduled worker's reaper reads it).
            "publishing_started_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }},
        return_document=ReturnDocument.BEFORE,
    )
    if piece is not None:
        previous = piece.get("publish_status")
        return {**piece, "publish_status": "publishing"}, previous

    # Either the piece doesn't exist, or it's already publishing/published —
    # distinguish so a real 404 doesn't get reported as "already publishing."
    existing = await content_pieces.find_one(
        {"piece_id": piece_id, "workspace_id": workspace_id, "deleted": {"$ne": True}}
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Piece not found.")
    raise HTTPException(
        status_code=409,
        detail="This piece is already being published or has already been published.",
    )


async def _release_claim(piece_id: str, workspace_id: str, previous: Optional[str]) -> None:
    """Undo _claim_piece_for_publishing for a call that never reached the
    platform. Only touches a piece still on "publishing", so it can't
    overwrite a real outcome written in the meantime."""
    flt = {"piece_id": piece_id, "workspace_id": workspace_id, "publish_status": "publishing"}
    if previous:
        await content_pieces.update_one(
            flt, {"$set": {"publish_status": previous, "updated_at": datetime.now(timezone.utc)}}
        )
    else:
        await content_pieces.update_one(
            flt,
            {"$unset": {"publish_status": ""}, "$set": {"updated_at": datetime.now(timezone.utc)}},
        )


async def _update_piece_status(
    piece_id: str,
    workspace_id: str,
    status: str,
    platform_post_id: Optional[str] = None,
    platform_post_url: Optional[str] = None,
    error_message: Optional[str] = None,
    increment_attempts: bool = False,
    media_dropped_reason: Optional[str] = None,
    published_content: Optional[str] = None,
    published_version: Optional[int] = None,
) -> None:
    """Update publish status fields on a piece."""
    updates: dict = {
        "publish_status": status,
        "updated_at": datetime.now(timezone.utc),
    }
    if platform_post_id:
        updates["platform_post_id"] = platform_post_id
    if platform_post_url:
        updates["platform_post_url"] = platform_post_url
    if error_message:
        updates["last_error"] = error_message
    # Row 9: the same outcome the Activity Log already shows, also written
    # onto the piece itself so Drafts/Library/Calendar can render a real
    # "media dropped" badge without joining against Activity Log rows.
    if status == "published":
        updates["media_dropped_reason"] = media_dropped_reason or None
    if status == "published":
        # The real go-live time — metric checkpoints (1h/24h/72h/7d) are
        # measured from this, not from updated_at.
        updates["published_at"] = updates["updated_at"]
        # What actually went out (the platform fixer may have trimmed it) and which version of the post that was, so a later
        # edit never rewrites history and analytics stays tied to the words that were live.
        if published_content is not None:
            updates["published_content"] = published_content
        if published_version is not None:
            updates["published_version"] = published_version

    flt = {"piece_id": piece_id, "workspace_id": workspace_id}
    if increment_attempts:
        await content_pieces.update_one(
            flt, {"$inc": {"publish_attempts": 1}, "$set": updates}
        )
        return

    await content_pieces.update_one(flt, {"$set": updates})


# ─────────────────────────────────────────────────────────────────────────────
# PUBLISH NOW
# ─────────────────────────────────────────────────────────────────────────────

async def _record_publish_failure(ws: str, user_id: str, piece_id: str, platform: str, reason: str) -> None:
    """Activity Log row for a publish that didn't go out (Passive lane)."""
    from app.shared.activity import record_system
    from app.shared.activity.projector import platform_name
    await record_system(
        workspace_id=ws,
        key=f"publish:{piece_id}",
        actor_name="",
        actor_user_id=user_id,
        category="post_published",
        title=f"Publishing to {platform_name(platform)} failed",
        description=reason or "The platform rejected the post.",
        status="failed",
        channel=platform,
        target_id=piece_id,
        target_type="Draft Post",
        href="/dashboard/drafts",
    )


def _failure_fields(error_type: ErrorType, result) -> dict:
    """What the screens need to offer the right action: the kind of failure and whether trying again can help."""
    code = failure_code(error_type, result.error_code or 500, result.error_message or "")
    return {"code": code, "retryable": code in RETRYABLE_CODES}


@router.post("/now")
@limiter.limit("10/minute")
async def publish_now(
    request: Request,
    body: PublishNowRequest,
    ctx: WorkspaceContext = Depends(require("publish_content")),
) -> dict:
    """
    Publish a piece immediately to a platform.
    Runs supervisor logic — auto-retry on transient errors,
    auto-fix on fixable errors, alert on fatal errors.
    """
    try:
        return await _publish_now(body, ctx)
    except HTTPException:
        raise
    except Exception as exc:
        # A crash between the claim and a real outcome would leave the piece
        # on "publishing" forever, and the claim refuses to pick it up again.
        # Only touches a piece still in flight, so a real outcome is kept.
        await content_pieces.update_one(
            {"piece_id": body.piece_id, "workspace_id": ctx.workspace_id, "publish_status": "publishing"},
            {"$set": {
                "publish_status": "failed",
                "last_error": "Publishing hit an unexpected problem. Check the platform first, then try again.",
                "updated_at": datetime.now(timezone.utc),
            }},
        )
        logger.exception("publish_now crashed for piece %s: %s", body.piece_id, exc)
        raise


async def _publish_now(body: PublishNowRequest, ctx: WorkspaceContext) -> dict:
    ws = ctx.workspace_id
    # Atomic claim, not a plain read — see _claim_piece_for_publishing's own
    # docstring. This also marks the piece "publishing" immediately, so the
    # separate _update_piece_status(..., "publishing") call further down is
    # gone; the claim already did it as part of the same atomic operation.
    piece, previous_status = await _claim_piece_for_publishing(body.piece_id, ws)
    # Derived from the piece, not caller input — a piece is always exactly
    # one platform, and the publish subsystem's own vocabulary (token_store,
    # the PUBLISHERS registry, the scheduled_posts worker) is the lowercase
    # slug ("linkedin"), not the display-cased content Platform value
    # ("LinkedIn") that piece["platform"] actually holds.
    display_platform = piece["platform"]
    platform = platform_key(display_platform)

    unavailable = await availability_block(display_platform, ws)
    if unavailable:
        await _release_claim(body.piece_id, ws, previous_status)
        raise unavailable.http()

    if platform == "youtube" and body.youtube_metadata:
        from app.pipelines.publish.youtube.metadata import visibility_problem

        problem = visibility_problem(str(body.youtube_metadata.get("privacy_status", "private")))
        if problem:
            await _release_claim(body.piece_id, ws, previous_status)
            raise HTTPException(status_code=422, detail=problem)

    # Check platform token exists for this workspace
    token_data = await get_token(ws, platform)

    # Get publisher. A webhook or manual-handoff platform with saved settings resolves to the thin adapter, which
    # has no token; every real publisher resolves, and fails, exactly as before.
    try:
        publisher = get_publisher(platform)
    except ValueError:
        publisher = await adapter_for(platform, ws)
    if publisher is not None and not getattr(publisher, "uses_oauth_token", True):
        token_data = {}
    elif not token_data:
        await _release_claim(body.piece_id, ws, previous_status)
        raise HTTPException(
            status_code=400,
            detail=f"{display_platform} is not connected. "
                   "Connect it in Settings to publish.",
        )
    if publisher is None:
        await _release_claim(body.piece_id, ws, previous_status)
        raise HTTPException(
            status_code=400,
            detail=f"Platform '{display_platform}' not supported.",
        )

    # Server-side gate: only approved posts go out, and a flagged one needs an
    # explicit "publish anyway". The claim is put back, nothing was sent.
    from app.agents.content_guard.agent import check_piece_before_send

    await check_piece_before_send(piece, ws)
    block = check_gate(piece, confirm_anyway=body.confirm_publish_anyway)
    if block:
        await _release_claim(body.piece_id, ws, previous_status)
        raise block.http()
    if body.confirm_publish_anyway:
        await record_override(piece, ws, ctx.user_id)

    # Build publish request — media populated for real (was always empty,
    # PublishRequest.media_urls existed but neither construction site ever
    # passed it) from whatever the piece actually has attached.
    pub_request = PublishRequest(
        piece_id=body.piece_id,
        workspace_id=ws,
        user_id=ctx.user_id,
        brand_id=piece["brand_id"],
        platform=platform,
        content=piece["content"],
        media=[MediaAsset(**m) for m in await media_for_publish(piece, ws, platform)],
        youtube_metadata=body.youtube_metadata,
        options=piece.get("publish_options") or {},
    )
    pub_request.platform_user_id = token_data.get("platform_user_id", "")

    content    = piece["content"]
    attempt    = 0
    max_attempts = 3
    auth_recovery_tried = False

    while attempt < max_attempts:
        pub_request.content = content
        result = await publisher.publish(pub_request, token_data.get("access_token", ""))

        if result.manual_action_url:
            # A manual handoff never posts. The piece goes back to where it was and the member gets the link; it is
            # only published when they confirm with "I posted this myself".
            await _release_claim(body.piece_id, ws, previous_status)
            return {
                "success": False,
                "manual": True,
                "piece_id": body.piece_id,
                "platform": platform,
                "manual_action_url": result.manual_action_url,
                "instructions": result.manual_instructions,
                "message": "Ready to post. Open the link, post it, then tap I posted this myself.",
            }

        if result.success:
            await _update_piece_status(
                body.piece_id, ws,
                "published",
                platform_post_id=result.platform_post_id,
                platform_post_url=result.platform_post_url,
                increment_attempts=True,
                media_dropped_reason=result.media_dropped_reason or extra_media_note(piece, platform) or planned_media_note(piece),
                published_content=content,
                published_version=piece.get("version_count"),
            )
            from app.shared.governance_events import emit_content_published
            emit_content_published(
                ws, pipeline_type=piece.get("pipeline_type", "text"),
                actor_user_id=ctx.user_id, actor_role=ctx.role,
                content_id=body.piece_id, target=platform,
                external_url=result.platform_post_url or "",
                media_dropped_reason=result.media_dropped_reason or "",
            )
            await mark_healthy(ws, platform, via="a successful publish")
            return {
                "success":          True,
                "piece_id":         body.piece_id,
                "platform":         platform,
                "platform_post_id": result.platform_post_id,
                "platform_post_url": result.platform_post_url,
                "attempts":         attempt + 1,
            }

        error_type = classify_error(
            result.error_code or 500,
            result.error_message or "",
        )

        await save_incident(
            piece_id=body.piece_id,
            platform=platform,
            workspace_id=ws,
            user_id=ctx.user_id,
            brand_id=piece["brand_id"],
            error_type=error_type.value,
            error_code=result.error_code,
            error_message=result.error_message or "",
            retry_count=attempt,
            retry_at=None,
        )

        if error_type == ErrorType.AUTH and not auth_recovery_tried:
            # Self-heal once: renew the token and retry before bothering
            # anyone. recover_connection records the failure if it can't.
            auth_recovery_tried = True
            if await recover_connection(ws, platform):
                token_data = await get_token(ws, platform) or token_data
                continue

        if error_type == ErrorType.AUTH:
            await _update_piece_status(
                body.piece_id, ws, "failed",
                error_message=result.error_message,
                increment_attempts=True,
            )
            owner_id = ctx.workspace.get("owner_id")
            if owner_id:
                owner = await users.find_one({"id": owner_id}, {"email": 1})
                if owner and owner.get("email"):
                    await send_templated_email(
                        "platform-reconnect-needed",
                        owner["email"],
                        {
                            "PLATFORM": display_platform,
                            "WORKSPACE_NAME": ctx.workspace.get("name", "your workspace"),
                            "RECONNECT_URL": f"{settings.FRONTEND_URL}/dashboard/settings",
                        },
                    )
            await _record_publish_failure(
                ws, ctx.user_id, body.piece_id, platform,
                f"{display_platform} connection expired or was revoked — reconnect it in Settings.",
            )
            # NOT a 401: the frontend's axios interceptor reads any 401 as
            # "your login expired", silently refreshes the session and
            # replays this request — a second real publish attempt (second
            # incident, second reconnect email to the owner) — and the user
            # then saw a raw API path. This is the platform's connection
            # failing, not the user's session, so it gets its own status and
            # a structured, plain-language reason the UI can act on.
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "platform_reconnect_required",
                    "platform": platform,
                    "message": (
                        f"Your {display_platform} connection expired or was revoked. "
                        "Reconnect it in Settings to publish."
                    ),
                },
            )

        if error_type == ErrorType.FIXABLE:
            fixed, new_content = fix_content(
                platform, content, result.error_message or ""
            )
            if fixed:
                content = new_content
                attempt += 1
                continue
            else:
                # publish_status="failed" (not the old "flagged" — not a
                # real PublishStatus value, so compute_kanban_stage in
                # storage.py couldn't distinguish it from a normal
                # approved-and-waiting piece and silently showed it as
                # "staging" instead of surfacing the failure).
                await _update_piece_status(
                    body.piece_id, ws, "failed",
                    error_message=result.error_message,
                    increment_attempts=True,
                )
                await _record_publish_failure(ws, ctx.user_id, body.piece_id, platform, result.error_message or "")
                return {
                    "success":  False,
                    "piece_id": body.piece_id,
                    "platform": platform,
                    "status":   "failed",
                    "reason":   result.error_message,
                    **_failure_fields(error_type, result),
                }

        if error_type == ErrorType.FATAL:
            await alert_fatal(
                piece_id=body.piece_id,
                platform=platform,
                workspace_id=ws,
                user_id=ctx.user_id,
                brand_id=piece["brand_id"],
                error_code=result.error_code,
                error_message=result.error_message or "",
            )
            await _update_piece_status(
                body.piece_id, ws, "failed",
                error_message=result.error_message,
                increment_attempts=True,
            )
            await _record_publish_failure(ws, ctx.user_id, body.piece_id, platform, result.error_message or "")
            return {
                "success":  False,
                "piece_id": body.piece_id,
                "platform": platform,
                "status":   "failed",
                "reason":   result.error_message,
                **_failure_fields(error_type, result),
            }

        # QA-002: this used to sleep in-request (up to 10s, up to 3 attempts
        # — 20-30s+ of a hanging "Publish Now" click, with no client-side
        # timeout) before reaching the background handoff below. A TRANSIENT
        # error now goes straight there on the first attempt — the
        # background worker is exactly where a real wait-and-retry belongs,
        # not inside a synchronous HTTP request.
        break

    # Only a TRANSIENT error (rate limit / 5xx) reaches here — AUTH, FATAL and
    # unfixable FIXABLE errors all returned above. The scheduled worker keeps
    # retrying exactly these on its own backoff, so hand the post to it rather
    # than failing a post the platform may well accept in a few minutes. The
    # two paths now end the same way: permanent errors fail, temporary ones
    # keep being retried in the background.
    retry_at = datetime.now(timezone.utc) + timedelta(
        seconds=get_retry_delay(ErrorType.TRANSIENT, 0) or 60
    )
    await content_pieces.update_one(
        {"piece_id": body.piece_id, "workspace_id": ws},
        {"$set": {
            "publish_status":       "queued",
            "publish_scheduled_at": retry_at,
            # The worker publishes to publish_target and would otherwise fall
            # back to LinkedIn for a piece that never had one set.
            "publish_target":       platform,
            # A fresh background budget (MAX_RETRIES for TRANSIENT) — the
            # attempts spent while the user watched don't count against it.
            "publish_attempts":     0,
            "last_error":           result.error_message or "Platform temporarily unavailable",
            "updated_at":           datetime.now(timezone.utc),
        }},
    )
    from app.shared.activity import record_system
    from app.shared.activity.projector import platform_name
    await record_system(
        workspace_id=ws,
        key=f"publish:{body.piece_id}",
        actor_name="",
        actor_user_id=ctx.user_id,
        category="post_published",
        title=f"{platform_name(platform)} is busy — retrying in the background",
        description=(
            f"{result.error_message or 'The platform is temporarily unavailable.'} "
            f"Recast will keep retrying automatically, starting "
            f"{retry_at.strftime('%H:%M')} UTC."
        ),
        status="warning",
        channel=platform,
        target_id=body.piece_id,
        target_type="Scheduled Post",
        href="/dashboard/calendar",
        metadata={"retries": attempt},
    )
    return {
        "success":  False,
        "piece_id": body.piece_id,
        "platform": platform,
        "status":   "retry_scheduled",
        "retry_at": retry_at.isoformat(),
        "reason":   result.error_message or "Platform temporarily unavailable",
        "attempts": attempt,
        **_failure_fields(error_type, result),
    }


# Scheduling now lives on app.api.v1.content's /pieces/{id}/schedule —
# it used to be duplicated here with a different, worker-incompatible
# publish_status value ("scheduled" instead of "queued"), so pieces
# scheduled through this endpoint silently never got picked up by
# app.workers.scheduled_posts. Nothing in the frontend called this route,
# so removing it (rather than fixing a second implementation of the same
# thing) is the real fix — one schedule path, not two.


# Cancelling now lives on app.api.v1.content's /pieces/{id}/cancel-schedule
# for the same reason scheduling does — one implementation, not two.


# ─────────────────────────────────────────────────────────────────────────────
# YOUTUBE — REVIEW STEP
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/suggest-times")
@limiter.limit("30/minute")
async def suggest_times(
    request: Request,
    piece_id: str,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    """Up to three good times to publish this post, in the member's time zone. Uses this workspace's own results on the platform when
    there are enough, otherwise common times for the platform, and says which. Posts already planned for the platform are checked so a
    clash is shown. A suggestion is never a promise of results."""
    from app.pipelines.publish.timing import rank_slots

    piece = await content_pieces.find_one({"piece_id": piece_id, "workspace_id": ctx.workspace_id, "deleted": {"$ne": True}}, {"platform": 1})
    if not piece:
        raise HTTPException(status_code=404, detail="Piece not found.")
    slug = platform_key(piece["platform"])
    since = datetime.now(timezone.utc) - timedelta(days=90)
    rows = await post_metric_checkpoints.find(
        {"workspace_id": ctx.workspace_id, "checkpoint": "24h", "platform": slug, "published_at": {"$gte": since}},
        {"published_at": 1, "metrics.engagement_rate": 1},
    ).sort("published_at", -1).limit(300).to_list(length=300)
    samples = [{"published_at": r.get("published_at"), "engagement": (r.get("metrics") or {}).get("engagement_rate") or 0.0} for r in rows]
    planned = [
        p.get("publish_scheduled_at")
        for p in await content_pieces.find(
            {"workspace_id": ctx.workspace_id, "publish_status": "queued", "publish_target": slug, "piece_id": {"$ne": piece_id}, "deleted": {"$ne": True}},
            {"publish_scheduled_at": 1},
        ).to_list(length=500)
    ]
    return rank_slots(platform=slug, label=piece["platform"], samples=samples, planned=planned, tz_name=ctx.user.get("timezone"))


@router.post("/youtube/prepare")
@limiter.limit("20/minute")
async def prepare_youtube_publish(
    request: Request,
    body: YouTubePrepareRequest,
    ctx: WorkspaceContext = Depends(require("publish_content")),
) -> dict:
    """Real, brand-grounded YouTube title/description/tags/category draft
    for the user to review and edit before publishing — never a guess the
    user can't see or change. The actual /now call, when given this back
    (possibly edited) as youtube_metadata, uses it as-is."""
    from app.db.mongo import media_assets
    from app.pipelines.publish.youtube.links import collect_social_links
    from app.pipelines.publish.youtube.metadata import generate_youtube_metadata

    ws = ctx.workspace_id
    piece = await _get_verified_piece(body.piece_id, ws)

    brand = await brand_profiles.find_one({"id": piece["brand_id"], "workspace_id": ws})
    if not brand:
        raise HTTPException(status_code=404, detail="Brand profile not found.")

    # The piece embeds only a snapshot of its video from attach time; the
    # transcript and chapters live on the media_assets document.
    media = None
    attached = (piece.get("media") or [None])[0]
    if attached and attached.get("id"):
        media = await media_assets.find_one({"id": attached["id"], "workspace_id": ws})

    links = await collect_social_links(ws, brand)
    metadata = await generate_youtube_metadata(
        piece["content"], brand, media=media, social_links=links,
    )
    return {
        **metadata.model_dump(),
        # What the review step needs to show about the recording itself.
        "video": {
            "media_id": (media or {}).get("id"),
            "analysis_status": (media or {}).get("analysis_status", "none"),
            "analysis_error": (media or {}).get("analysis_error"),
            "chapter_count": len((media or {}).get("chapters") or []),
            "has_transcript": bool((media or {}).get("transcript")),
            "duration_s": (media or {}).get("duration_s"),
        },
        "social_links": [{"label": label, "url": url} for label, url in links],
    }


# ─────────────────────────────────────────────────────────────────────────────
# STATUS
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/{piece_id}/status")
@limiter.limit("60/minute")
async def get_publish_status(
    request: Request,
    piece_id: str,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    """Get the current publish status of a piece."""
    piece = await _get_verified_piece(piece_id, ctx.workspace_id)

    return {
        "piece_id":             piece_id,
        "publish_status":       piece.get("publish_status", "pending"),
        "publish_scheduled_at": piece.get("publish_scheduled_at"),
        "publish_target":       piece.get("publish_target"),
        "platform_post_id":     piece.get("platform_post_id"),
        "platform_post_url":    piece.get("platform_post_url"),
        "publish_attempts":     piece.get("publish_attempts", 0),
        "last_error":           piece.get("last_error"),
    }
