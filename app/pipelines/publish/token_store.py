"""
OAuth token storage — encrypt, store, retrieve, refresh.

Tokens are scoped per **workspace** (collection ``workspace_connections``,
keyed by ``(workspace_id, platform)``) — they are a shared workspace asset,
not a per-user one. All tokens are encrypted with Fernet before MongoDB
storage. Never store plaintext tokens. Never log token values.

Token lifecycle:
  Member connects platform → exchange_token() → encrypt → upsert connection
  Each publish call        → decrypt → use → never cache in memory
  Token expiring           → refresh worker → re-encrypt → update connection
  Member disconnects       → revoke on platform → delete connection
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings
from app.db.mongo import workspace_connections

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
    workspace_id: str,
    platform: str,
    access_token: str,
    refresh_token: Optional[str],
    expires_at: Optional[datetime],
    platform_user_id: str,
    username: str,
    connected_by: str = "",
) -> None:
    """
    Save or update OAuth tokens for a workspace + platform.
    Tokens encrypted before storage. Upserts — safe to call on reconnect.
    """
    now = datetime.now(timezone.utc)

    await workspace_connections.update_one(
        {"workspace_id": workspace_id, "platform": platform},
        {
            "$set": {
                "workspace_id": workspace_id,
                "platform": platform,
                "access_token": encrypt_token(access_token),
                "refresh_token": encrypt_token(refresh_token) if refresh_token else None,
                "expires_at": expires_at,
                "platform_user_id": platform_user_id,
                "username": username,
                "is_active": True,
                "last_refreshed_at": now,
            },
            "$setOnInsert": {
                "id": str(uuid4()),
                "connected_by": connected_by,
                "connected_at": now,
            },
        },
        upsert=True,
    )

    logger.info("Token saved for workspace %s platform %s", workspace_id, platform)


async def get_token(workspace_id: str, platform: str) -> Optional[dict]:
    """
    Retrieve and decrypt tokens for a workspace + platform.
    Returns None if not connected. Returns dict with: access_token,
    refresh_token, expires_at, platform_user_id, username, is_active.
    """
    account = await workspace_connections.find_one(
        {"workspace_id": workspace_id, "platform": platform}
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
            "Token decryption failed for workspace %s platform %s — "
            "token may be corrupted",
            workspace_id, platform,
        )
        return None


async def delete_token(workspace_id: str, platform: str) -> None:
    """Remove a platform connection for a workspace."""
    await workspace_connections.delete_one(
        {"workspace_id": workspace_id, "platform": platform}
    )
    logger.info("Token deleted for workspace %s platform %s", workspace_id, platform)


async def get_all_tokens(workspace_id: str) -> list[dict]:
    """
    List all connected platforms for a workspace.
    Returns list without decrypted tokens — safe for API responses.
    """
    accounts = await workspace_connections.find(
        {"workspace_id": workspace_id, "is_active": True}
    ).to_list(length=100)

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
    ]


async def is_token_expiring_soon(
    workspace_id: str,
    platform: str,
    within_days: int = 7,
) -> bool:
    """Check if a token expires within the given number of days."""
    token = await get_token(workspace_id, platform)
    if not token or not token.get("expires_at"):
        return False

    expires_at = token["expires_at"]
    if isinstance(expires_at, str):
        expires_at = datetime.fromisoformat(expires_at)

    threshold = datetime.now(timezone.utc) + timedelta(days=within_days)
    return expires_at < threshold
