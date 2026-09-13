"""Admin notification delivery for supervisor flags and insights.

Always writes an ``admin_notifications`` row (the in-app feed — the reliable
channel). Email is best-effort and ONLY attempted when ``RESEND_API_KEY`` is
configured; it reuses the existing Resend integration (no new provider). A
delivery failure never propagates.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import uuid4

from app.core.config import settings
from app.core.notifications import OPS_FROM, send_templated_email
from app.db.mongo import admin_notifications, users, workspace_flags, workspace_members, workspaces

logger = logging.getLogger(__name__)

_ADMIN_ROLES = ("owner", "admin")


async def resolve_admin_user_ids(workspace_id: str) -> list[str]:
    rows = await workspace_members.find(
        {"workspace_id": workspace_id, "status": "active", "role": {"$in": list(_ADMIN_ROLES)}},
        {"user_id": 1},
    ).to_list(length=200)
    return [r["user_id"] for r in rows if r.get("user_id")]


async def _admin_emails(workspace_id: str) -> list[str]:
    ids = await resolve_admin_user_ids(workspace_id)
    if not ids:
        return []
    rows = await users.find({"id": {"$in": ids}}, {"email": 1}).to_list(length=200)
    return [r["email"] for r in rows if r.get("email")]


def _email_enabled() -> bool:
    return bool(settings.RESEND_API_KEY)


async def _send_email(
    to: list[str], *, workspace_name: str, summary: str, severity: str, langsmith_run_url: str
) -> None:
    if not to:
        return
    variables = {
        "WORKSPACE_NAME": workspace_name,
        "SUMMARY": summary,
        "SEVERITY": severity,
    }
    if langsmith_run_url:
        variables["LANGSMITH_RUN_URL"] = langsmith_run_url
    for email in to:
        await send_templated_email(
            "supervisor-critical-flag", email, variables, from_override=OPS_FROM,
        )


async def deliver(
    workspace_id: str,
    *,
    kind: str,          # "flag" | "insight"
    source_id: str,
    title: str,
    body_persona: str,
    severity: str = "warning",
    email: bool | None = None,   # None → auto (email on critical if configured)
) -> str:
    """Create the in-app notification and, if warranted + configured, an email."""
    notif_id = str(uuid4())
    await admin_notifications.insert_one({
        "_id": notif_id,
        "workspace_id": workspace_id,
        "audience": "admins",
        "title": title,
        "body_persona": body_persona,
        "source": {"kind": kind, "id": source_id},
        "severity": severity,
        "read_by": [],
        "created_at": datetime.now(timezone.utc),
    })

    want_email = email if email is not None else (severity == "critical")
    emailed = False
    if want_email and _email_enabled():
        ws = await workspaces.find_one({"id": workspace_id}, {"name": 1})
        langsmith_run_url = ""
        if kind == "flag":
            flag_doc = await workspace_flags.find_one({"_id": source_id}, {"langsmith_run_url": 1})
            langsmith_run_url = (flag_doc or {}).get("langsmith_run_url") or ""
        await _send_email(
            await _admin_emails(workspace_id),
            workspace_name=(ws or {}).get("name", "your workspace"),
            summary=body_persona,
            severity=severity,
            langsmith_run_url=langsmith_run_url,
        )
        emailed = True

    logger.info(
        "admin notification: ws=%s kind=%s src=%s severity=%s emailed=%s",
        workspace_id, kind, source_id, severity, emailed,
    )
    return notif_id
