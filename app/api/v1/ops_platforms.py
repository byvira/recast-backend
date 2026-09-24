"""
Ops Dashboard — platform config CRUD. Owner-only (``manage_workspace_settings``,
same gate as ``/supervisor/run``), covers config-driven platforms only
(webhook / manual-handoff / rss_pull integration patterns) — code-driven
platforms (the 5 real ones, plus everything still needing custom OAuth code)
have nothing to configure here and are rejected with a 400 explaining why.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, require_ops_admin
from app.models.platform_config import PlatformConfigWrite
from app.pipelines.publish.platform_config_store import (
    delete_platform_config,
    get_platform_config,
    list_platform_configs,
    save_platform_config,
)
from app.platforms.base import get_platform, import_all

router = APIRouter()
logger = logging.getLogger(__name__)

_OWNER = require_ops_admin("manage_workspace_settings")


def _assert_config_driven(platform: str) -> None:
    import_all()
    definition = get_platform(platform)
    if definition is None:
        raise HTTPException(status_code=404, detail=f"Unknown platform: {platform}")
    if definition.mode != "config_driven":
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{platform}' is code_driven ({definition.integration_pattern}) — "
                "it needs a real publisher class, not an Ops Dashboard config. "
                "Connect it from the OAuth connections flow instead."
            ),
        )


@router.get("")
@limiter.limit("30/minute")
async def list_configs(request: Request, ctx: WorkspaceContext = Depends(_OWNER)) -> dict:
    import_all()
    configs = {c["platform"]: c for c in await list_platform_configs(ctx.workspace_id)}
    from app.platforms.base import list_platforms

    # Surface every config-driven platform, configured or not, so the Ops
    # Dashboard can show "not configured yet" rows alongside real ones.
    rows = []
    for definition in list_platforms():
        if definition.mode != "config_driven":
            continue
        row = configs.get(definition.key) or {
            "id": None,
            "workspace_id": ctx.workspace_id,
            "platform": definition.key,
            "label": None,
            "enabled": False,
            "fields": {},
            "secrets_configured": {},
            "created_by": "",
            "created_at": None,
            "updated_at": None,
        }
        row["platform_label"] = definition.label
        row["integration_pattern"] = definition.integration_pattern
        row["status"] = definition.status
        row["configured"] = row["id"] is not None
        rows.append(row)

    return {"platforms": rows, "total": len(rows)}


@router.get("/{platform}")
@limiter.limit("30/minute")
async def get_config(request: Request, platform: str, ctx: WorkspaceContext = Depends(_OWNER)) -> dict:
    _assert_config_driven(platform)
    config = await get_platform_config(ctx.workspace_id, platform)
    if config is None:
        raise HTTPException(status_code=404, detail=f"No config for platform: {platform}")
    return config


@router.put("/{platform}")
@limiter.limit("20/minute")
async def upsert_config(
    request: Request,
    platform: str,
    body: PlatformConfigWrite,
    ctx: WorkspaceContext = Depends(_OWNER),
) -> dict:
    _assert_config_driven(platform)
    return await save_platform_config(
        workspace_id=ctx.workspace_id,
        platform=platform,
        label=body.label,
        enabled=body.enabled,
        fields=body.fields,
        secrets=body.secrets,
        created_by=ctx.user_id,
    )


@router.delete("/{platform}")
@limiter.limit("20/minute")
async def remove_config(request: Request, platform: str, ctx: WorkspaceContext = Depends(_OWNER)) -> dict:
    _assert_config_driven(platform)
    await delete_platform_config(ctx.workspace_id, platform)
    return {"deleted": True, "platform": platform}
