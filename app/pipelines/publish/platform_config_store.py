"""
Ops Dashboard config storage — admin-entered settings for config-driven
platforms (webhook / manual-handoff / rss_pull), collection
``platform_configs``, keyed by ``(workspace_id, platform)``.

Same Fernet-at-rest convention as app.pipelines.publish.token_store (reuses
its encrypt_token/decrypt_token — one Fernet key, one encryption convention
for every secret in this codebase). The difference from token_store: secrets
here are **write-only** past the initial save — get_platform_config() never
returns decrypted values, only booleans ("is this secret set"), per
docs/PLATFORM_REGISTRY_PLAN.md Stage 2's rule ("never re-returned after save,
UI shows '•••• configured'"). Only a publisher actually sending content
(WebhookPublisher) calls get_platform_config_secrets() to decrypt.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from app.db.mongo import platform_configs
from app.pipelines.publish.token_store import decrypt_token, encrypt_token

logger = logging.getLogger(__name__)


def _to_public(doc: dict) -> dict:
    """Strip encrypted secret values, keep everything else."""
    return {
        "id": doc["id"],
        "workspace_id": doc["workspace_id"],
        "platform": doc["platform"],
        "label": doc.get("label"),
        "enabled": doc.get("enabled", True),
        "fields": doc.get("fields", {}),
        "secrets_configured": {k: True for k in doc.get("secrets", {})},
        "created_by": doc.get("created_by", ""),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
    }


async def save_platform_config(
    workspace_id: str,
    platform: str,
    label: Optional[str],
    enabled: bool,
    fields: dict[str, Any],
    secrets: dict[str, str],
    created_by: str = "",
) -> dict:
    """
    Create or update a platform config. Upserts — safe to call on edit.

    `secrets` is plaintext input, encrypted immediately before storage. Only
    keys present in `secrets` are touched — omitting a previously-set secret
    key leaves its stored (encrypted) value untouched, so an admin can edit
    the label or a non-secret field without having to re-type a bot token.
    Pass an empty string as a secret's value to explicitly clear it.
    """
    now = datetime.now(timezone.utc)
    existing = await platform_configs.find_one({"workspace_id": workspace_id, "platform": platform})

    existing_secrets: dict[str, str] = (existing or {}).get("secrets", {})
    updated_secrets = dict(existing_secrets)
    for key, value in secrets.items():
        if value == "":
            updated_secrets.pop(key, None)
        else:
            updated_secrets[key] = encrypt_token(value)

    await platform_configs.update_one(
        {"workspace_id": workspace_id, "platform": platform},
        {
            "$set": {
                "workspace_id": workspace_id,
                "platform": platform,
                "label": label,
                "enabled": enabled,
                "fields": fields,
                "secrets": updated_secrets,
                "updated_at": now,
            },
            "$setOnInsert": {
                "id": str(uuid4()),
                "created_by": created_by,
                "created_at": now,
            },
        },
        upsert=True,
    )
    logger.info("Platform config saved — workspace=%s platform=%s", workspace_id, platform)

    doc = await platform_configs.find_one({"workspace_id": workspace_id, "platform": platform})
    return _to_public(doc)


async def get_platform_config(workspace_id: str, platform: str) -> Optional[dict]:
    """Public shape only — secrets_configured booleans, never values."""
    doc = await platform_configs.find_one({"workspace_id": workspace_id, "platform": platform})
    return _to_public(doc) if doc else None


async def list_platform_configs(workspace_id: str) -> list[dict]:
    docs = await platform_configs.find({"workspace_id": workspace_id}).to_list(length=200)
    return [_to_public(d) for d in docs]


async def delete_platform_config(workspace_id: str, platform: str) -> None:
    await platform_configs.delete_one({"workspace_id": workspace_id, "platform": platform})
    logger.info("Platform config deleted — workspace=%s platform=%s", workspace_id, platform)


async def get_platform_config_secrets(workspace_id: str, platform: str) -> dict[str, str]:
    """Decrypted secret values — for a publisher's own use only (WebhookPublisher).
    Never call this from an API route handler; nothing that reaches an HTTP
    response should hold a decrypted secret."""
    doc = await platform_configs.find_one({"workspace_id": workspace_id, "platform": platform})
    if not doc:
        return {}
    return {k: decrypt_token(v) for k, v in doc.get("secrets", {}).items()}
