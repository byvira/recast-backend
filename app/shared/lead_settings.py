"""Staff-editable settings for sign-up and the lead emails. Stored as one document; the config file supplies the first values."""

from typing import Any

from app.core.config import settings
from app.db.mongo import lead_settings

SIGNUP_MODES = ("auto", "open", "invite")
_ID = "main"


def _defaults() -> dict[str, Any]:
    inbox = (settings.CONTACT_INBOX_EMAIL or "").strip()
    return {
        "signup_mode": settings.SIGNUP_MODE if settings.SIGNUP_MODE in SIGNUP_MODES else "auto",
        "invite_expiry_days": settings.INVITE_EXPIRY_DAYS,
        "daily_invite_cap": settings.DAILY_INVITE_CAP,
        "digest_enabled": True,
        "digest_recipients": [inbox] if inbox else [],
    }


async def get_lead_settings() -> dict[str, Any]:
    stored = await lead_settings.find_one({"_id": _ID}, {"_id": 0}) or {}
    return {**_defaults(), **stored}


async def save_lead_settings(changes: dict[str, Any]) -> dict[str, Any]:
    await lead_settings.update_one({"_id": _ID}, {"$set": changes}, upsert=True)
    return await get_lead_settings()


def effective_signup_mode(value: str) -> str:
    """`auto` is invite only in production and open everywhere else, so development and tests never need a code."""
    if value == "auto":
        return "invite" if settings.ENVIRONMENT == "production" else "open"
    return value if value in ("open", "invite") else "invite"
