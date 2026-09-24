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

from app.core.config import settings
from app.core.middleware import limiter
from app.core.notifications import send_templated_email
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.db.mongo import content_pieces, users
from app.pipelines.publish.base import PublishRequest
from app.pipelines.publish.registry import get_publisher
from app.pipelines.publish.health import mark_healthy
from app.workers.token_refresh import recover_connection
from app.pipelines.publish.token_store import get_token
from app.pipelines.publish.supervisor.alerts import alert_fatal, save_incident
from app.pipelines.publish.supervisor.classifier import classify_error, ErrorType
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


async def _update_piece_status(
    piece_id: str,
    workspace_id: str,
    status: str,
    platform_post_id: Optional[str] = None,
    platform_post_url: Optional[str] = None,
    error_message: Optional[str] = None,
    increment_attempts: bool = False,
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
    if status == "published":
        # The real go-live time — metric checkpoints (1h/24h/72h/7d) are
        # measured from this, not from updated_at.
        updates["published_at"] = updates["updated_at"]

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
    ws = ctx.workspace_id
    piece = await _get_verified_piece(body.piece_id, ws)
    # Derived from the piece, not caller input — a piece is always exactly
    # one platform, and the publish subsystem's own vocabulary (token_store,
    # the PUBLISHERS registry, the scheduled_posts worker) is the lowercase
    # slug ("linkedin"), not the display-cased content Platform value
    # ("LinkedIn") that piece["platform"] actually holds.
    display_platform = piece["platform"]
    platform = display_platform.lower()

    # Check platform token exists for this workspace
    token_data = await get_token(ws, platform)
    if not token_data:
        raise HTTPException(
            status_code=400,
            detail=f"{display_platform} is not connected. "
                   f"Connect at /api/v1/oauth/{platform}/connect",
        )

    # Get publisher
    try:
        publisher = get_publisher(platform)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Platform '{display_platform}' not supported.",
        )

    # Build publish request
    pub_request = PublishRequest(
        piece_id=body.piece_id,
        workspace_id=ws,
        user_id=ctx.user_id,
        brand_id=piece["brand_id"],
        platform=platform,
        content=piece["content"],
    )
    pub_request.platform_user_id = token_data.get("platform_user_id", "")

    await _update_piece_status(body.piece_id, ws, "publishing")

    content    = piece["content"]
    attempt    = 0
    max_attempts = 3
    auth_recovery_tried = False

    while attempt < max_attempts:
        pub_request.content = content
        result = await publisher.publish(pub_request, token_data["access_token"])

        if result.success:
            await _update_piece_status(
                body.piece_id, ws,
                "published",
                platform_post_id=result.platform_post_id,
                platform_post_url=result.platform_post_url,
                increment_attempts=True,
            )
            from app.shared.governance_events import emit_content_published
            emit_content_published(
                ws, pipeline_type=piece.get("pipeline_type", "text"),
                actor_user_id=ctx.user_id, actor_role=ctx.role,
                content_id=body.piece_id, target=platform,
                external_url=result.platform_post_url or "",
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
            raise HTTPException(
                status_code=401,
                detail=f"{display_platform} token expired or revoked. "
                       f"Reconnect at /api/v1/oauth/{platform}/connect",
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
            "publish_scheduled_at": retry_at.isoformat(),
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
