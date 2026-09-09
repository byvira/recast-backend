"""
OAuth token storage — encrypt, store, retrieve, refresh.

All tokens encrypted with Fernet symmetric encryption before MongoDB storage.
Never store plaintext tokens. Never log token values.

Token lifecycle:
  User connects platform  → exchange_token() → encrypt → save to MongoDB
  Each publish call       → decrypt → use → never cache in memory
  Token expiring          → refresh worker → re-encrypt → update MongoDB
  User disconnects        → revoke on platform → delete from MongoDB
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings
from app.db.mongo import users

logger = logging.getLogger(__name__)

# Fernet instance — created once at module load
_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        if not settings.FERNET_SECRET_KEY:
            raise RuntimeError(
                "FERNET_SECRET_KEY not set. "
                "Generate with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )
        _fernet = Fernet(settings.FERNET_SECRET_KEY.encode())
    return _fernet


# ─────────────────────────────────────────────────────────────────────────────
# ENCRYPT / DECRYPT
# ─────────────────────────────────────────────────────────────────────────────

def encrypt_token(token: str) -> str:
    """Encrypt a token string. Returns encrypted bytes as string."""
    return _get_fernet().encrypt(token.encode()).decode()


def decrypt_token(encrypted: str) -> str:
    """Decrypt an encrypted token. Raises InvalidToken if tampered."""
    return _get_fernet().decrypt(encrypted.encode()).decode()


# ─────────────────────────────────────────────────────────────────────────────
# SAVE / GET / DELETE
# ─────────────────────────────────────────────────────────────────────────────

async def save_token(
    user_id: str,
    platform: str,
    access_token: str,
    refresh_token: Optional[str],
    expires_at: Optional[datetime],
    platform_user_id: str,
    username: str,
) -> None:
    """
    Save or update OAuth tokens for a user + platform.
    Tokens encrypted before storage.
    Upserts — safe to call on reconnect.
    """
    now = datetime.now(timezone.utc)

    account = {
        "platform": platform,
        "access_token": encrypt_token(access_token),
        "refresh_token": encrypt_token(refresh_token) if refresh_token else None,
        "expires_at": expires_at,
        "platform_user_id": platform_user_id,
        "username": username,
        "is_active": True,
        "connected_at": now,
        "last_refreshed_at": now,
    }

    # Remove existing entry for this platform, then add new one
    await users.update_one(
        {"id": user_id},
        {"$pull": {"social_accounts": {"platform": platform}}},
    )
    await users.update_one(
        {"id": user_id},
        {"$push": {"social_accounts": account}},
    )

    logger.info("Token saved for user %s platform %s", user_id, platform)


async def get_token(user_id: str, platform: str) -> Optional[dict]:
    """
    Retrieve and decrypt tokens for a user + platform.
    Returns None if not connected.
    Returns dict with: access_token, refresh_token, expires_at,
    platform_user_id, username, is_active.
    """
    user = await users.find_one({"id": user_id})
    if not user:
        return None

    accounts = user.get("social_accounts", [])
    account = next(
        (a for a in accounts if a["platform"] == platform),
        None,
    )
    if not account or not account.get("is_active"):
        return None

    try:
        return {
            "access_token": decrypt_token(account["access_token"]),
            "refresh_token": (
                decrypt_token(account["refresh_token"])
                if account.get("refresh_token")
                else None
            ),
            "expires_at": account.get("expires_at"),
            "platform_user_id": account.get("platform_user_id"),
            "username": account.get("username"),
            "is_active": account.get("is_active", True),
        }
    except InvalidToken:
        logger.error(
            "Token decryption failed for user %s platform %s — "
            "token may be corrupted",
            user_id, platform,
        )
        return None


async def delete_token(user_id: str, platform: str) -> None:
    """Remove a platform connection for a user."""
    await users.update_one(
        {"id": user_id},
        {"$pull": {"social_accounts": {"platform": platform}}},
    )
    logger.info("Token deleted for user %s platform %s", user_id, platform)


async def get_all_tokens(user_id: str) -> list[dict]:
    """
    List all connected platforms for a user.
    Returns list without decrypted tokens — safe for API responses.
    """
    user = await users.find_one({"id": user_id})
    if not user:
        return []

    accounts = user.get("social_accounts", [])
    return [
        {
            "platform": a["platform"],
            "username": a.get("username"),
            "platform_user_id": a.get("platform_user_id"),
            "is_active": a.get("is_active", True),
            "connected_at": a.get("connected_at"),
            "expires_at": a.get("expires_at"),
        }
        for a in accounts
        if a.get("is_active")
    ]


async def is_token_expiring_soon(
    user_id: str,
    platform: str,
    within_days: int = 7,
) -> bool:
    """Check if a token expires within the given number of days."""
    token = await get_token(user_id, platform)
    if not token or not token.get("expires_at"):
        return False

    expires_at = token["expires_at"]
    if isinstance(expires_at, str):
        expires_at = datetime.fromisoformat(expires_at)

    threshold = datetime.now(timezone.utc) + timedelta(days=within_days)
    return expires_at < threshold