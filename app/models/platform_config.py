"""Admin-entered config for config-driven platforms (webhook / manual-handoff /
rss_pull integration patterns) — the Ops Dashboard's data layer.

Distinct from WorkspaceConnection (app/models/workspace.py): that's a
per-member OAuth connection made through a platform's own consent screen.
PlatformConfig is entered once by a workspace owner/admin for a platform that
has no OAuth flow at all — a bot token, a webhook URL, a compose-link
template. Same Fernet-at-rest convention as token_store.py, same
workspace_id + platform scoping as workspace_connections.
"""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class PlatformConfig(BaseModel):
    id: str
    workspace_id: str
    platform: str                              # must be a registered, config_driven platform key
    label: Optional[str] = None                # display override; falls back to the registry label
    enabled: bool = True

    # Non-secret settings — safe to return from the API as-is. e.g. a Telegram
    # config's {"chat_id": "@my_channel"}, or a manual-handoff platform's
    # {"compose_url_template": "https://reddit.com/submit?title={title}&text={content}"}.
    fields: dict[str, Any] = Field(default_factory=dict)

    # Which secret keys have been set (e.g. {"webhook_url": True, "bot_token": True}) —
    # booleans only. The actual encrypted values never leave app.pipelines.publish
    # .platform_config_store; this model (and every API response built from it)
    # never carries plaintext or ciphertext secret values.
    secrets_configured: dict[str, bool] = Field(default_factory=dict)

    created_by: str = ""                        # user_id (audit)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class PlatformConfigWrite(BaseModel):
    """Request body for creating/updating a config. `secrets` here is
    plaintext input only — encrypted immediately in platform_config_store and
    never echoed back (see save_platform_config's docstring)."""

    label: Optional[str] = None
    enabled: bool = True
    fields: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)
