"""
Token refresh worker — proactive renewal with self-healing retries.

Runs every hour. A connection expiring within 7 days is renewed at most once
a day while things are fine; after a failed renewal it's retried about an
hour later instead of waiting a day. Escalation (owner email, ops alert,
Odette flag) is decided by ``app.pipelines.publish.health`` — only after
recovery fails twice in a row, never on the first failure.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from app.core.scheduler_lock import distributed_job_lock
from app.db.mongo import workspace_connections
from app.pipelines.publish import health
from app.pipelines.publish.registry import get_publisher
from app.pipelines.publish.token_store import decrypt_token, save_token
from app.shared.activity import record_system

logger = logging.getLogger(__name__)

RENEW_WITHIN = timedelta(days=7)
#: Healthy connections: one renewal attempt per day at most.
HEALTHY_RETRY_AFTER = timedelta(hours=20)
#: After a failure: try again roughly an hour later (the job runs hourly).
FAILED_RETRY_AFTER = timedelta(minutes=55)
#: After escalation: keep trying quietly, less often.
ESCALATED_RETRY_AFTER = timedelta(hours=6)


def _as_utc(value) -> Optional[datetime]:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


def _is_due(account: dict, now: datetime) -> bool:
    expires_at = _as_utc(account.get("expires_at"))
    if not expires_at or expires_at > now + RENEW_WITHIN:
        return False
    h = account.get("health") or {}
    if h.get("escalated"):
        wait = ESCALATED_RETRY_AFTER
    elif int(h.get("failures") or 0) > 0:
        wait = FAILED_RETRY_AFTER
    else:
        wait = HEALTHY_RETRY_AFTER
    attempts = [t for t in (_as_utc(account.get("last_refreshed_at")), _as_utc(h.get("checked_at"))) if t]
    return not attempts or now - max(attempts) >= wait


async def refresh_connection(account: dict) -> tuple[bool, str]:
    """Renew one connection's access token. Returns (ok, error). Also used by
    the publish paths to recover from an expired token on the spot."""
    platform = account["platform"]
    try:
        publisher = get_publisher(platform)
    except ValueError:
        return False, f"No publisher for {platform}"
    encrypted_refresh = account.get("refresh_token")
    if not encrypted_refresh:
        return False, "No refresh token on file — this platform needs a manual reconnect."
    try:
        refresh_token_value = decrypt_token(encrypted_refresh)
        new_token = await publisher.refresh_token(refresh_token_value)
        await save_token(
            workspace_id=account["workspace_id"],
            platform=platform,
            access_token=new_token["access_token"],
            refresh_token=refresh_token_value,
            expires_at=new_token["expires_at"],
            platform_user_id=account.get("platform_user_id", ""),
            username=account.get("username", ""),
            connected_by=account.get("connected_by", ""),
            recovered_via="automatic renewal",
        )
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, str(e)


async def recover_connection(workspace_id: str, platform: str) -> bool:
    """Try to self-heal a connection a platform just rejected. Records the
    failure (and escalates per policy) if renewal doesn't work."""
    account = await workspace_connections.find_one(
        {"workspace_id": workspace_id, "platform": platform, "is_active": True}
    )
    if not account:
        return False
    ok, error = await refresh_connection(account)
    if not ok:
        await health.record_failure(workspace_id, platform, reason=error, broken=True)
    return ok


@distributed_job_lock("refresh_expiring_tokens", ttl_seconds=3600)
async def refresh_expiring_tokens() -> None:
    """Hourly: renew every due connection (see ``_is_due``)."""
    now = datetime.now(timezone.utc)
    connections = await workspace_connections.find(
        {"is_active": True, "expires_at": {"$ne": None}}
    ).to_list(length=5000)

    refreshed = failed = 0
    for account in connections:
        if not _is_due(account, now):
            continue
        platform, workspace_id = account["platform"], account["workspace_id"]
        ok, error = await refresh_connection(account)
        if ok:
            refreshed += 1
            logger.info("Refreshed token for workspace %s platform %s", workspace_id, platform)
            await record_system(
                workspace_id=workspace_id,
                key=f"token_refresh:{workspace_id}:{platform}:{now.date().isoformat()}",
                actor_name="Connection monitor",
                category="account_connected",
                title=f"{platform.capitalize()} connection renewed automatically",
                description=f"Access for {account.get('username') or platform} was about to expire and was "
                            f"renewed before any post could fail.",
                channel=platform,
                href="/dashboard/settings",
            )
        else:
            failed += 1
            logger.error("Token refresh failed for workspace %s platform %s: %s",
                         workspace_id, platform, error)
            await health.record_failure(workspace_id, platform, reason=error)

    if refreshed or failed:
        logger.info("Token refresh complete — refreshed: %d, failed: %d", refreshed, failed)
