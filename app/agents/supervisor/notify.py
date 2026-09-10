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
from app.db.mongo import admin_notifications, users, workspace_members

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


async def _send_email(to: list[str], subject: str, body: str) -> None:
    if not to:
        return
    if settings.ENVIRONMENT != "production":
        logger.info("[DEV] supervisor email to %s: %s", to, subject)
        return
    try:
        import resend  # type: ignore[import-untyped]

        resend.api_key = settings.RESEND_API_KEY
        resend.Emails.send({
            "from": settings.EMAIL_FROM,
            "to": to,
            "subject": subject,
            "html": f'<div style="font-family:sans-serif;max-width:560px;margin:auto">'
                    f'<p style="white-space:pre-wrap">{body}</p></div>',
        })
    except Exception as exc:  # noqa: BLE001
        logger.error("supervisor email delivery failed: %s", exc)


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
        await _send_email(
            await _admin_emails(workspace_id),
            subject=f"[{severity.upper()}] {title}",
            body=f"{body_persona}\n\n— Odette, your workspace supervisor",
        )
        emailed = True

    logger.info(
        "admin notification: ws=%s kind=%s src=%s severity=%s emailed=%s",
        workspace_id, kind, source_id, severity, emailed,
    )
    return notif_id
