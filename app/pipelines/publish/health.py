"""Connection health + self-healing escalation policy.

Each ``workspace_connections`` row carries a ``health`` sub-document:

    {"state": "healthy" | "degraded" | "broken",
     "failures": int,            # consecutive failed recoveries
     "reason": str, "checked_at": dt, "last_ok_at": dt, "escalated": bool}

Policy (the part that keeps noise down as more runs unattended):

* A failed automatic recovery (token renewal) is logged to the Activity Log
  and retried about an hour later — nobody is emailed.
* Only when recovery fails ``ESCALATE_AFTER`` times in a row does it escalate:
  one owner email, one ops alert, and an Odette ``connection_broken`` flag in
  the admins' Active lane. Escalation happens once per outage.
* Any success (renewal, publish, reconnect) returns the connection to
  healthy, records the recovery, and resolves Odette's flag.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from app.core.config import settings
from app.core.notifications import send_templated_email
from app.db.mongo import users, workspace_connections, workspace_flags, workspaces
from app.shared.activity import project_odette_flag, record_system

logger = logging.getLogger(__name__)

ESCALATE_AFTER = 2
FLAG_TYPE = "connection_broken"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _label(platform: str) -> str:
    return (platform or "").capitalize()


async def _connection(workspace_id: str, platform: str) -> Optional[dict]:
    return await workspace_connections.find_one({"workspace_id": workspace_id, "platform": platform})


async def mark_healthy(workspace_id: str, platform: str, *, via: str) -> None:
    """A renewal, publish or reconnect just worked. ``via`` says which, for
    the recovery row. Never raises."""
    try:
        conn = await _connection(workspace_id, platform)
        if not conn:
            return
        previous = (conn.get("health") or {}).get("state", "healthy")
        if previous == "healthy":
            return   # the common case — no write on every successful publish
        now = _now()
        await workspace_connections.update_one(
            {"_id": conn["_id"]},
            {"$set": {"health": {
                "state": "healthy", "failures": 0, "reason": "",
                "checked_at": now, "last_ok_at": now, "escalated": False,
            }}},
        )
        await note_recovered(workspace_id, platform, previous_state=previous, via=via)
    except Exception as exc:  # noqa: BLE001
        logger.error("mark_healthy failed for %s/%s: %s", workspace_id, platform, exc)


async def note_recovered(workspace_id: str, platform: str, *, previous_state: str, via: str) -> None:
    """Record a recovery and close Odette's flag. Called by ``mark_healthy``
    and by ``token_store.save_token`` (renewal / reconnect), which resets the
    health document itself. No-op unless the connection was unhealthy."""
    if previous_state == "healthy":
        return
    try:
        now = _now()
        await record_system(
            workspace_id=workspace_id,
            key=f"health:{workspace_id}:{platform}:{now.isoformat()}",
            actor_name="Connection monitor",
            category="account_connected",
            title=f"{_label(platform)} connection recovered",
            description=f"Back to normal after {via}.",
            channel=platform,
            href="/dashboard/settings",
        )
        await _resolve_flag(workspace_id, platform)
    except Exception as exc:  # noqa: BLE001
        logger.error("note_recovered failed for %s/%s: %s", workspace_id, platform, exc)


async def record_failure(
    workspace_id: str,
    platform: str,
    *,
    reason: str,
    broken: bool = False,
) -> int:
    """One failed automatic recovery. ``broken`` = the platform refused the
    credentials outright (vs. a renewal that may still succeed). Returns the
    consecutive-failure count. Never raises."""
    try:
        conn = await _connection(workspace_id, platform)
        if not conn:
            return 0
        health = conn.get("health") or {}
        failures = int(health.get("failures") or 0) + 1
        expires_at = conn.get("expires_at")
        if isinstance(expires_at, datetime) and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        expired = isinstance(expires_at, datetime) and expires_at <= _now()
        state = "broken" if (broken or expired) else "degraded"
        now = _now()
        await workspace_connections.update_one(
            {"_id": conn["_id"]},
            {"$set": {
                "health.state": state,
                "health.failures": failures,
                "health.reason": reason[:500],
                "health.checked_at": now,
            }},
        )

        if health.get("escalated"):
            # Already escalated this outage — keep counting, stay quiet.
            return failures

        escalate = failures >= ESCALATE_AFTER
        await record_system(
            workspace_id=workspace_id,
            key=f"health:{workspace_id}:{platform}:{now.isoformat()}",
            actor_name="Connection monitor",
            category="account_connected",
            title=(
                f"{_label(platform)} connection needs reconnecting"
                if escalate else f"{_label(platform)} connection renewal failed — retrying"
            ),
            description=(
                f"Automatic recovery failed {failures} times in a row. Reconnect it in Settings "
                f"— scheduled posts to {_label(platform)} will fail until then."
                if escalate else
                "Recast will try again automatically in about an hour. Nothing to do yet."
            ),
            status="failed" if escalate else "warning",
            channel=platform,
            href="/dashboard/settings",
            metadata={"retryAttempt": failures},
        )
        if escalate:
            await _escalate(workspace_id, platform, reason, failures, conn)
            await workspace_connections.update_one(
                {"_id": conn["_id"]}, {"$set": {"health.escalated": True}}
            )
        return failures
    except Exception as exc:  # noqa: BLE001
        logger.error("record_failure failed for %s/%s: %s", workspace_id, platform, exc)
        return 0


async def _escalate(workspace_id: str, platform: str, reason: str, failures: int, conn: dict) -> None:
    from app.agents.supervisor.personas import odette_flag_summary
    from app.agents.supervisor.ticks import _resolve_workspace_language
    from app.pipelines.publish.supervisor.alerts import alert_token_refresh_failure

    ws = await workspaces.find_one({"id": workspace_id}) or {}

    # Odette flag → the admins' Active lane (one open flag per platform).
    if not await workspace_flags.find_one({
        "workspace_id": workspace_id, "flag_type": FLAG_TYPE,
        "status": "open", "detail.platform": platform,
    }):
        detail = {"platform": _label(platform), "failures": failures,
                  "account": conn.get("username") or platform}
        flag = {
            "_id": str(uuid4()),
            "workspace_id": workspace_id,
            "flag_type": FLAG_TYPE,
            "detection": "rule",
            "severity": "critical",
            "summary_persona": await odette_flag_summary(
                FLAG_TYPE, detail, language=await _resolve_workspace_language(ws),
            ),
            "detail": {**detail, "platform": platform},
            "metric": {"name": "failed_recoveries", "value": float(failures), "limit": float(ESCALATE_AFTER)},
            "langsmith_run_url": None,
            "status": "open",
            "notified": {"in_app": True, "email": True, "at": _now()},
            "created_at": _now(),
            "resolved_at": None,
        }
        await workspace_flags.insert_one(flag)
        await project_odette_flag(flag)

    await alert_token_refresh_failure(workspace_id=workspace_id, platform=platform, error_message=reason)

    owner_id = ws.get("owner_id")
    owner = await users.find_one({"id": owner_id}, {"email": 1}) if owner_id else None
    if owner and owner.get("email"):
        await send_templated_email(
            "platform-reconnect-needed",
            owner["email"],
            {
                "PLATFORM": _label(platform),
                "WORKSPACE_NAME": ws.get("name", "your workspace"),
                "RECONNECT_URL": f"{settings.FRONTEND_URL}/dashboard/settings",
            },
        )


async def _resolve_flag(workspace_id: str, platform: str) -> None:
    async for flag in workspace_flags.find({
        "workspace_id": workspace_id, "flag_type": FLAG_TYPE,
        "status": "open", "detail.platform": platform,
    }):
        await workspace_flags.update_one(
            {"_id": flag["_id"]},
            {"$set": {"status": "resolved", "resolved_at": _now(), "resolved_by": "system"}},
        )
        flag.update(status="resolved", resolved_at=_now(), resolved_by="system")
        await project_odette_flag(flag)
