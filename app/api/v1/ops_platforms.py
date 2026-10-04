"""
Ops Dashboard: the settings for config-driven platforms (webhook, manual handoff, RSS directory).

Owner-only (``manage_workspace_settings``, same gate as ``/supervisor/run``). Settings saved here are platform-wide:
they are stored once and shared by every workspace, and a workspace's own row (for example its own webhook address)
is laid over them when a post is published. Code-driven platforms have nothing to configure here and are rejected
with a 400 that says so. Stage, rollout and the Ops overview are in ops_platform_lifecycle.py.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.middleware import limiter
from app.core.auth import require_platform_staff
from app.models.platform_config import PlatformConfigWrite
from app.pipelines.publish.platform_config_store import (
    PLATFORM_WIDE,
    delete_platform_config,
    get_platform_config,
    list_platform_configs,
    save_platform_config,
)
from app.platforms.base import get_platform, import_all

router = APIRouter()
logger = logging.getLogger(__name__)

async def _ops_owner(user: dict = Depends(require_platform_staff)) -> dict:
    """Settings saved here apply to every workspace, so changing them is for the Ops owner (a master admin), never for
    the owner of one workspace. Reading them needs platform staff."""
    if not user.get("is_master_admin"):
        raise HTTPException(status_code=403, detail="Only the Ops owner can change platform settings.")
    return user


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


async def _check_settings(platform: str, body: PlatformConfigWrite) -> None:
    """Refuse a webhook address or compose link that could reach inside our network or is not https, before it is
    stored. The same checks run again just before every send."""
    from app.pipelines.publish.generic.manual_handoff_publisher import template_problem
    from app.pipelines.publish.generic.safe_url import UnsafeUrl, assert_safe_url

    webhook_url = body.secrets.get("webhook_url")
    if webhook_url:
        try:
            await assert_safe_url(webhook_url)
        except UnsafeUrl as exc:
            raise HTTPException(status_code=400, detail=f"Webhook address: {exc}")
    template = body.fields.get("compose_url_template")
    if template:
        problem = template_problem(str(template))
        if problem:
            raise HTTPException(status_code=400, detail=f"Compose link: {problem}")


@router.get("/configs")
@limiter.limit("30/minute")
async def list_configs(request: Request, user: dict = Depends(require_platform_staff)) -> dict:
    import_all()
    configs = {c["platform"]: c for c in await list_platform_configs(PLATFORM_WIDE)}
    from app.platforms.base import list_platforms

    # Surface every config-driven platform, configured or not, so the Ops
    # Dashboard can show "not configured yet" rows alongside real ones.
    rows = []
    for definition in list_platforms():
        if definition.mode != "config_driven":
            continue
        row = configs.get(definition.key) or {
            "id": None,
            "workspace_id": PLATFORM_WIDE,
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


@router.get("/{platform}/config")
@limiter.limit("30/minute")
async def get_config(request: Request, platform: str, user: dict = Depends(require_platform_staff)) -> dict:
    _assert_config_driven(platform)
    config = await get_platform_config(PLATFORM_WIDE, platform)
    if config is None:
        raise HTTPException(status_code=404, detail=f"No config for platform: {platform}")
    return config


@router.put("/{platform}/config")
@router.put("/{platform}")
@limiter.limit("20/minute")
async def upsert_config(
    request: Request,
    platform: str,
    body: PlatformConfigWrite,
    user: dict = Depends(_ops_owner),
) -> dict:
    _assert_config_driven(platform)
    await _check_settings(platform, body)
    return await save_platform_config(
        workspace_id=PLATFORM_WIDE,
        platform=platform,
        label=body.label,
        enabled=body.enabled,
        fields=body.fields,
        secrets=body.secrets,
        created_by=user["id"],
    )


@router.delete("/{platform}/config")
@router.delete("/{platform}")
@limiter.limit("20/minute")
async def remove_config(request: Request, platform: str, user: dict = Depends(_ops_owner)) -> dict:
    _assert_config_driven(platform)
    await delete_platform_config(PLATFORM_WIDE, platform)
    return {"deleted": True, "platform": platform}
