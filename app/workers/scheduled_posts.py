"""
Scheduled posts worker.
Runs every minute — finds queued posts and fires publish pipeline.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.core.notifications import send_templated_email
from app.core.scheduler_lock import distributed_job_lock
from app.db.mongo import content_pieces, users, workspaces
from app.models.media import MediaAsset
from app.pipelines.publish.base import PublishRequest
from app.pipelines.publish.registry import adapter_for, get_publisher
from app.pipelines.publish.spine import check_gate, extra_media_note, iso_utc, media_for_publish, planned_media_note, platform_key
from app.pipelines.publish.supervisor.alerts import alert_fatal
from app.pipelines.publish.supervisor.classifier import classify_error, ErrorType
from app.pipelines.publish.supervisor.retry import get_retry_delay, should_retry
from app.pipelines.publish.token_store import get_token
from app.pipelines.publish.health import mark_healthy
from pymongo import ReturnDocument
from app.workers.token_refresh import recover_connection
from app.shared.activity import record_system
from app.shared.activity.projector import platform_name

logger = logging.getLogger(__name__)


async def _notify_publish_failure(
    *, piece_id: str, platform: str, user_id: str, brand_id: str,
    workspace_id: str, error_message: str, scheduled_at: str = "",
) -> None:
    """Alert ops and email the content owner about a scheduled-publish failure.

    Mirrors the FATAL alert path the synchronous /publish/now endpoint already
    uses (app.api.v1.publish) — this worker previously fired no alert of any
    kind. Never raises; a notification failure must not crash the scheduler.
    """
    await record_system(
        workspace_id=workspace_id,
        key=f"publish:{piece_id}",
        actor_name="Publishing scheduler",
        category="post_published",
        title=f"Scheduled post to {platform_name(platform)} failed",
        description=error_message,
        status="failed",
        channel=platform,
        target_id=piece_id,
        target_type="Scheduled Post",
        href="/dashboard/calendar",
        metadata={"scheduledFor": scheduled_at} if scheduled_at else None,
    )
    try:
        await alert_fatal(
            piece_id=piece_id,
            platform=platform,
            user_id=user_id,
            brand_id=brand_id,
            error_code=None,
            error_message=error_message,
            workspace_id=workspace_id,
        )
    except Exception as e:
        logger.error("alert_fatal failed for piece %s: %s", piece_id, e)

    try:
        owner = await users.find_one({"id": user_id}, {"email": 1})
        ws = await workspaces.find_one({"id": workspace_id}, {"name": 1}) if workspace_id else None
        if owner and owner.get("email"):
            await send_templated_email(
                "scheduled-post-failed",
                owner["email"],
                {
                    "PLATFORM": platform,
                    "WORKSPACE_NAME": (ws or {}).get("name", "your workspace"),
                    "SCHEDULED_AT": scheduled_at,
                    "ERROR_MESSAGE": error_message,
                    "PIECE_ID": piece_id,
                    "DASHBOARD_LINK": f"{settings.FRONTEND_URL}/dashboard",
                },
            )
    except Exception as e:
        logger.error("publish-failure email failed for piece %s: %s", piece_id, e)


# A post that has been "publishing" longer than this was interrupted (a restart,
# a crash). It is marked failed, never retried by itself: the platform may
# already have posted it.
PUBLISHING_STALE_AFTER = timedelta(minutes=15)
INTERRUPTED_MESSAGE = "Publishing was interrupted. Check the platform before trying again."


def _due_filter(now: datetime) -> dict:
    """Queued posts whose time has come. publish_scheduled_at is a real datetime
    on new rows and an ISO string on older ones; a comparison only matches its
    own type, so both are asked."""
    return {
        "publish_status": "queued",
        "deleted":        {"$ne": True},
        # A post held because its platform is paused or retired is never claimed.
        "hold":           {"$exists": False},
        "$or": [
            {"publish_scheduled_at": {"$lte": now}},
            {"publish_scheduled_at": {"$lte": now.isoformat()}},
        ],
    }


async def reap_stuck_publishing() -> int:
    """Mark posts stuck in "publishing" as failed. Returns how many."""
    now = datetime.now(timezone.utc)
    cutoff = now - PUBLISHING_STALE_AFTER
    stuck = await content_pieces.find({
        "publish_status": "publishing",
        "$or": [
            {"publishing_started_at": {"$lt": cutoff}},
            # Rows from before the start time was stamped: last touched long ago.
            {"publishing_started_at": {"$exists": False}, "updated_at": {"$lt": cutoff}},
        ],
    }, {"piece_id": 1, "workspace_id": 1, "platform": 1, "publish_target": 1}).to_list(length=100)
    reaped = 0
    for piece in stuck:
        result = await content_pieces.update_one(
            {"piece_id": piece["piece_id"], "publish_status": "publishing"},
            {"$set": {"publish_status": "failed", "last_error": INTERRUPTED_MESSAGE, "updated_at": now}},
        )
        if not result.modified_count:
            continue
        reaped += 1
        platform = platform_key(piece.get("publish_target") or piece.get("platform"))
        try:
            await record_system(
                workspace_id=piece.get("workspace_id", ""),
                key=f"publish:{piece['piece_id']}",
                actor_name="Publishing scheduler",
                category="post_published",
                title=f"Post to {platform_name(platform)} was interrupted",
                description=INTERRUPTED_MESSAGE,
                status="failed",
                channel=platform,
                target_id=piece["piece_id"],
                target_type="Scheduled Post",
                href="/dashboard/calendar",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("reaper activity row failed for %s: %s", piece["piece_id"], exc)
    if reaped:
        logger.warning("Marked %d stuck publishing posts as failed", reaped)
    return reaped


async def _claim_due_piece(piece_id: str, now: datetime) -> dict | None:
    """Atomically move one due piece queued -> publishing. Whoever wins the swap
    publishes it; Publish Now claims the same way, so the two cannot both post."""
    return await content_pieces.find_one_and_update(
        {**_due_filter(now), "piece_id": piece_id},
        {"$set": {
            "publish_status": "publishing",
            "publishing_started_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }},
        return_document=ReturnDocument.AFTER,
    )


async def _return_unapproved(piece: dict, message: str) -> None:
    """A due piece that may not go out (not approved, rejected, needs review):
    back to pending with a plain note, so it is not picked up again every minute."""
    await content_pieces.update_one(
        {"piece_id": piece["piece_id"], "publish_status": "publishing"},
        {"$set": {
            "publish_status": "pending",
            "publish_scheduled_at": "",
            "schedule_note": f"Not published. {message}",
            "updated_at": datetime.now(timezone.utc),
        }},
    )
    platform = platform_key(piece.get("publish_target") or piece.get("platform"))
    try:
        await record_system(
            workspace_id=piece.get("workspace_id", ""),
            key=f"publish:{piece['piece_id']}",
            actor_name="Publishing scheduler",
            category="post_published",
            title=f"Scheduled post to {platform_name(platform)} was held back",
            description=message,
            status="warning",
            channel=platform,
            target_id=piece["piece_id"],
            target_type="Scheduled Post",
            href="/dashboard/drafts",
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("held-back activity row failed for %s: %s", piece["piece_id"], exc)


@distributed_job_lock("process_scheduled_posts", ttl_seconds=55)
async def process_scheduled_posts() -> None:
    """
    Find all posts scheduled for now or earlier and publish them.
    Called every minute by the scheduler.

    Each due post is claimed one at a time with an atomic queued -> publishing
    swap, so a second instance, a lock that failed open, or a Publish Now click
    cannot post the same piece twice.
    """
    await reap_stuck_publishing()

    now = datetime.now(timezone.utc)
    due = await content_pieces.find(_due_filter(now), {"piece_id": 1}).to_list(length=50)

    if not due:
        return

    logger.info("Found %d scheduled posts due for publishing", len(due))

    from app.agents.content_guard.agent import check_piece_before_send
    from app.agents.content_guard.config import ensure_fresh

    await ensure_fresh()  # the gate below reads the current content safety settings
    for candidate in due:
        piece_id = candidate["piece_id"]
        piece = await _claim_due_piece(piece_id, now)
        if piece is None:
            continue  # somebody else took it (or it was cancelled) first
        try:
            # Only approved posts go out; a flagged one only if a person already
            # chose "publish anyway" when scheduling it.
            await check_piece_before_send(piece, piece.get("workspace_id") or "")
            block = check_gate(piece, honour_recorded_override=True)
            if block:
                await _return_unapproved(piece, block.message)
                continue
            await _publish_scheduled_piece(piece)
        except Exception as e:
            logger.error("Failed to publish scheduled piece %s: %s", piece_id, e)
            # Never leave it on "publishing". Not retried: the platform may have posted.
            await content_pieces.update_one(
                {"piece_id": piece_id, "publish_status": "publishing"},
                {"$set": {
                    "publish_status": "failed",
                    "last_error": "Publishing hit an unexpected problem. Check the platform before trying again.",
                    "updated_at": datetime.now(timezone.utc),
                }},
            )


async def _fail_before_publish(piece: dict, platform: str, user_id: str, workspace_id: str, message: str) -> None:
    """Mark a claimed piece failed with a plain reason, and tell its owner. Used
    when nothing could be sent (no connection, no publisher, a crash)."""
    await content_pieces.update_one(
        {"piece_id": piece["piece_id"]},
        {"$set": {
            "publish_status": "failed",
            "last_error": message,
            "updated_at": datetime.now(timezone.utc),
        }},
    )
    await _notify_publish_failure(
        piece_id=piece["piece_id"], platform=platform, user_id=user_id,
        brand_id=piece.get("brand_id", ""), workspace_id=workspace_id,
        error_message=message,
        scheduled_at=iso_utc(piece.get("publish_scheduled_at")) or "",
    )


async def _hold_for_manual_post(piece: dict, platform: str, workspace_id: str, result) -> None:
    """A manual handoff cannot be posted by us. At the scheduled time the post goes back to waiting, with the
    compose link and a plain note, and the member sees it in the Activity Log. It is only published when they
    confirm with "I posted this myself". It is never marked published here."""
    name = platform_name(platform)
    note = f"Time to post this on {name} yourself. Open the link, post it, then tap I posted this myself."
    await content_pieces.update_one(
        {"piece_id": piece["piece_id"], "publish_status": "publishing"},
        {"$set": {
            "publish_status": "pending",
            "publish_scheduled_at": "",
            "schedule_note": note,
            "manual_post_due": True,
            "manual_action_url": result.manual_action_url,
            "manual_instructions": result.manual_instructions,
            "updated_at": datetime.now(timezone.utc),
        }},
    )
    try:
        await record_system(
            workspace_id=workspace_id,
            key=f"publish:{piece['piece_id']}",
            actor_name="Publishing scheduler",
            category="post_published",
            title=f"Time to post on {name} yourself",
            description=note,
            status="warning",
            channel=platform,
            target_id=piece["piece_id"],
            target_type="Scheduled Post",
            href="/dashboard/drafts",
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("manual-post activity row failed for %s: %s", piece["piece_id"], exc)


async def _publish_scheduled_piece(piece: dict) -> None:
    """Publish one scheduled piece."""
    piece_id     = piece["piece_id"]
    # publish_target may be missing (or an older display-style value): derive it.
    platform     = platform_key(piece.get("publish_target") or piece.get("platform"))
    user_id      = piece["user_id"]
    workspace_id = piece.get("workspace_id", "")

    if not workspace_id:
        logger.warning(
            "Piece %s has no workspace_id — skipping (pre-cutover data)", piece_id
        )
        await content_pieces.update_one(
            {"piece_id": piece_id},
            {"$set": {
                "publish_status": "failed",
                "last_error": "Missing workspace_id",
                "updated_at": datetime.now(timezone.utc),
            }},
        )
        await _notify_publish_failure(
            piece_id=piece_id, platform=platform, user_id=user_id,
            brand_id=piece.get("brand_id", ""), workspace_id="",
            error_message="Missing workspace_id",
            scheduled_at=iso_utc(piece.get("publish_scheduled_at")) or "",
        )
        return

    # The platform may have been paused or retired after this post was claimed. Put it back in the queue, held, and
    # do not send it.
    from app.pipelines.platform_ops.availability import platform_availability
    availability = await platform_availability(platform, workspace_id)
    if availability.value in ("paused", "retired"):
        reason = "platform_paused" if availability.value == "paused" else "platform_retired"
        await content_pieces.update_one(
            {"piece_id": piece_id, "publish_status": "publishing"},
            {"$set": {
                "publish_status": "queued",
                "hold": {"reason": reason, "platform_key": platform, "held_at": datetime.now(timezone.utc)},
                "schedule_note": "On hold while this platform is paused.",
                "updated_at": datetime.now(timezone.utc),
            }},
        )
        return

    # Get token — scoped to the piece's workspace
    token_data = await get_token(workspace_id, platform)

    # Get publisher. A webhook or manual-handoff platform with saved settings resolves to the thin adapter (no token).
    try:
        publisher = get_publisher(platform)
    except ValueError:
        publisher = await adapter_for(platform, workspace_id)
    if publisher is not None and not getattr(publisher, "uses_oauth_token", True):
        token_data = {}
    elif not token_data:
        logger.warning(
            "No token for workspace %s platform %s — piece %s skipped",
            workspace_id, platform, piece_id,
        )
        await _fail_before_publish(
            piece, platform, user_id, workspace_id,
            f"{platform_name(platform)} isn't connected, so this post couldn't go out. Reconnect it in Settings.",
        )
        return
    if publisher is None:
        logger.error("No publisher for platform %s", platform)
        await _fail_before_publish(
            piece, platform, user_id, workspace_id,
            f"Publishing to {platform_name(platform)} isn't supported yet.",
        )
        return

    # Build request — media populated for real, same fix as the publish-now
    # path (app/api/v1/publish.py); this was the other of the two real
    # construction sites that always left it empty.
    pub_request = PublishRequest(
        piece_id=piece_id,
        workspace_id=workspace_id,
        user_id=user_id,
        brand_id=piece["brand_id"],
        platform=platform,
        # The text to send: a fixed version made by an earlier failed attempt (see the FIXABLE branch below), else the post as written.
        content=piece.get("publish_content_override") or piece["content"],
        media=[MediaAsset(**m) for m in await media_for_publish(piece, workspace_id, platform)],
        # The title, description and tags the member reviewed when scheduling a
        # YouTube upload. Without them the publisher makes its own.
        youtube_metadata=piece.get("publish_youtube_metadata"),
    )
    pub_request.platform_user_id = token_data.get("platform_user_id", "")

    # Mark as publishing (the loop already claimed it; this also records the
    # derived target and refreshes the start time the reaper reads)
    await content_pieces.update_one(
        {"piece_id": piece_id},
        {"$set": {
            "publish_status": "publishing",
            "publish_target": platform,
            "publishing_started_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }},
    )

    try:
        result = await publisher.publish(pub_request, token_data.get("access_token", ""))
    except Exception as exc:  # noqa: BLE001
        # An exception here must not leave the piece "publishing", and must not
        # be retried by itself: the platform may already have the post.
        logger.error("Publisher raised for scheduled piece %s on %s: %s", piece_id, platform, exc)
        await _fail_before_publish(
            piece, platform, user_id, workspace_id,
            "Publishing hit an unexpected problem. Check the platform before trying again.",
        )
        return

    if result.manual_action_url:
        await _hold_for_manual_post(piece, platform, workspace_id, result)
        return

    if result.success:
        await content_pieces.update_one(
            {"piece_id": piece_id},
            {"$set": {
                "publish_status":    "published",
                "platform_post_id":  result.platform_post_id,
                "platform_post_url": result.platform_post_url,
                "updated_at":        datetime.now(timezone.utc),
                "published_at":      datetime.now(timezone.utc),
                # Row 9: same outcome the Activity Log already shows (see
                # emit_content_published below), also on the piece itself so
                # Drafts/Library/Calendar can render a real badge.
                "media_dropped_reason": result.media_dropped_reason or extra_media_note(piece, platform) or planned_media_note(piece) or None,
                # What went out and which version of the post it was (see _update_piece_status in api/v1/publish.py).
                "published_content": pub_request.content,
                "published_version": piece.get("version_count"),
            }},
        )
        try:
            from app.shared.governance_events import emit_content_published
            emit_content_published(
                workspace_id, pipeline_type=piece.get("pipeline_type", "text"),
                actor_user_id=user_id, actor_role="",
                content_id=piece_id, target=platform,
                external_url=result.platform_post_url or "",
                via="scheduled",
                media_dropped_reason=result.media_dropped_reason or "",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("emit content.published failed for %s: %s", piece_id, exc)
        await mark_healthy(workspace_id, platform, via="a successful publish")
        if piece.get("publish_attempts"):
            # Close out the retry chain row with the recovery.
            await record_system(
                workspace_id=workspace_id,
                key=f"publish:{piece_id}",
                actor_name="Publishing scheduler",
                category="post_published",
                title=f"Recovered: scheduled post to {platform_name(platform)} published",
                description=f"Went live after {piece['publish_attempts']} automatic "
                            f"{'retry' if piece['publish_attempts'] == 1 else 'retries'}.",
                channel=platform,
                target_id=piece_id,
                target_type="Live Post",
                href=result.platform_post_url or None,
                metadata={"retries": piece["publish_attempts"]},
            )
        logger.info("Scheduled piece %s published to %s", piece_id, platform)
    else:
        error_type = classify_error(result.error_code or 500, result.error_message or "")
        attempt = piece.get("publish_attempts", 0)

        # Self-heal an AUTH rejection once per piece: renew the token and
        # requeue for the very next tick. If renewal fails, recover_connection
        # has recorded it (and escalates on the second failure in a row).
        if error_type == ErrorType.AUTH and not piece.get("auth_recovery_tried"):
            recovered = await recover_connection(workspace_id, platform)
            await content_pieces.update_one(
                {"piece_id": piece_id},
                {"$set": {
                    "auth_recovery_tried": True,
                    **({
                        "publish_status": "queued",
                        "publish_scheduled_at": datetime.now(timezone.utc),
                        "updated_at": datetime.now(timezone.utc),
                    } if recovered else {}),
                }},
            )
            if recovered:
                await record_system(
                    workspace_id=workspace_id,
                    key=f"publish:{piece_id}",
                    actor_name="Publishing scheduler",
                    category="post_published",
                    title=f"Renewed {platform_name(platform)} access — retrying scheduled post",
                    description="The platform rejected the old access token; Recast renewed it "
                                "automatically and queued the post again.",
                    status="warning",
                    channel=platform,
                    target_id=piece_id,
                    target_type="Scheduled Post",
                    href="/dashboard/calendar",
                )
                return

        # AUTH errors can't be retried without the user reconnecting the
        # platform — same as /publish/now's own AUTH branch — so those (and
        # anything past MAX_RETRIES) fail immediately below. Everything else
        # (TRANSIENT/FIXABLE/FATAL-but-retryable per should_retry) gets
        # requeued instead of permanently failing on the first error — this
        # worker previously had no retry or requeue logic at all, unlike
        # /publish/now's synchronous retry loop.
        # The platform said the text itself is the problem (too long, a banned hashtag). Publish Now fixes it and sends again;
        # this worker used to queue the same unchanged text and fail on the second try. When the fixer can repair it, the
        # repaired text is kept for the next attempt (the post itself is not rewritten); when it cannot, retrying is pointless.
        if error_type == ErrorType.FIXABLE:
            from app.pipelines.publish.supervisor.fixer import fix_content

            fixed, fixed_text = fix_content(platform, pub_request.content, result.error_message or "")
            if fixed:
                await content_pieces.update_one(
                    {"piece_id": piece_id},
                    {"$set": {
                        "publish_status": "queued",
                        "publish_scheduled_at": datetime.now(timezone.utc) + timedelta(seconds=15),
                        "publish_content_override": fixed_text,
                        "last_error": result.error_message,
                        "updated_at": datetime.now(timezone.utc),
                    }, "$inc": {"publish_attempts": 1}},
                )
                return

        if error_type != ErrorType.AUTH and error_type != ErrorType.FIXABLE and should_retry(error_type, attempt):
            delay = get_retry_delay(error_type, attempt) or 60
            next_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
            await content_pieces.update_one(
                {"piece_id": piece_id},
                {
                    "$set": {
                        "publish_status": "queued",
                        "publish_scheduled_at": next_at,
                        "last_error": result.error_message,
                        "updated_at": datetime.now(timezone.utc),
                    },
                    "$inc": {"publish_attempts": 1},
                },
            )
            logger.info(
                "Scheduled piece %s failed on %s (attempt %d, %s) — requeued for retry at %s",
                piece_id, platform, attempt + 1, error_type.value, next_at.isoformat(),
            )
            await record_system(
                workspace_id=workspace_id,
                key=f"publish:{piece_id}",
                actor_name="Publishing scheduler",
                category="post_published",
                title=f"Retrying scheduled post to {platform_name(platform)}",
                description=f"{result.error_message or 'Platform error'} — retrying automatically "
                            f"in {max(delay // 60, 1)} min.",
                status="warning",
                channel=platform,
                target_id=piece_id,
                target_type="Scheduled Post",
                href="/dashboard/calendar",
                metadata={"retryAttempt": attempt + 1, "errorType": error_type.value.lower()},
            )
            return

        await content_pieces.update_one(
            {"piece_id": piece_id},
            {"$set": {
                "publish_status": "failed",
                "last_error":     result.error_message,
                "updated_at":     datetime.now(timezone.utc),
            }},
        )
        await _notify_publish_failure(
            piece_id=piece_id, platform=platform, user_id=user_id,
            brand_id=piece["brand_id"], workspace_id=workspace_id,
            error_message=result.error_message or "Unknown error",
            scheduled_at=iso_utc(piece.get("publish_scheduled_at")) or "",
        )
        logger.error(
            "Scheduled piece %s failed on %s: %s",
            piece_id, platform, result.error_message,
        )