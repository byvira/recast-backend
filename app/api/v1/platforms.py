"""
Platform registry API for signed-in members. Read-only.

Each platform carries what the code can do (status, formats, limits) and, for the caller's workspace, what they may
do with it right now: `availability` is "hidden", "connectable", "manual", "paused" or "retired" (see
app.pipelines.platform_ops.availability). Nothing Ops-only is returned here; the Ops screens use
/api/v1/ops/platforms.
"""

from app.core.config import settings
from app.pipelines.publish.spine import MULTI_PICTURE_LIMITS
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.auth import get_current_user
from app.core.workspace import WorkspaceContext, require
from app.db.mongo import platform_configs, platform_listings, workspace_members
from app.pipelines.platform_ops.events import record_platform_event
from app.pipelines.publish.generic.manual_handoff_publisher import template_problem
from app.pipelines.publish.platform_config_store import PLATFORM_WIDE
from app.pipelines.platform_ops.availability import availability_for_all, platform_availability
from app.platforms.base import PlatformDefinition, get_platform, import_all, list_platforms

router = APIRouter()


async def _caller_workspace(user: dict, requested: Optional[str]) -> Optional[str]:
    """The workspace to answer for: the one named in the request, else the user's default, and only if they belong
    to it. None when there is no such workspace (availability is then left out)."""
    workspace_id = requested or user.get("default_workspace_id")
    if not workspace_id:
        return None
    member = await workspace_members.find_one({"workspace_id": workspace_id, "user_id": user["id"]}, {"_id": 1})
    return workspace_id if member else None


def _serialize(p: PlatformDefinition) -> dict[str, Any]:
    """Public shape — omits nothing sensitive today (no secrets live on
    PlatformDefinition itself; those arrive with platform_configs in Stage 2,
    which will be write-only per docs/PLATFORM_REGISTRY_PLAN.md's rules)."""
    return {
        "key": p.key,
        "label": p.label,
        "category": p.category,
        "pipelines": sorted(p.pipelines),
        "modalities": p.modalities,
        "native_formats": p.native_formats,
        "shapes": p.shapes,
        "mode": p.mode,
        "integration_pattern": p.integration_pattern,
        "status": p.status,
        "audit_required": p.audit_required,
        "rate_limits": p.rate_limits,
        "max_chars": p.max_chars,
        "requires_media": p.requires_media,
        # How many pictures one post can carry on platforms that take several, so screens do not keep their own copy of the number.
        "max_images": MULTI_PICTURE_LIMITS.get(p.key),
        # False for YouTube until Google has passed the app's API audit: videos are private until then. Null for every other platform.
        "publish_unlocked": settings.YOUTUBE_API_AUDIT_PASSED if p.key == "youtube" else None,
        "policy_constraints": p.policy_constraints,
        "tone_profile": p.tone_profile,
        "access_notes": p.access_notes,
        "confidence": p.confidence,
        "has_official_post_api": p.has_official_post_api,
        "official_doc_url": p.official_doc_url,
        "verified_at": p.verified_at,
        "verification_note": p.verification_note,
        "connectable": p.publisher_cls is not None,
        "has_analytics": p.analytics_fetcher_cls is not None,
        # The content Platform value text generation uses for this platform
        # (app.models.text.Platform) — the frontend builds its platform maps
        # from these instead of hardcoding them. None when the platform has
        # no text generation rules.
        "text_platform": (p.text_enum_value or p.label) if p.has_text_prompt_rules else None,
        "text_thread_platform": p.thread_enum_value if p.has_text_prompt_rules else None,
    }


async def _member_extras(definitions: list[PlatformDefinition], workspace_id: str) -> dict[str, dict[str, Any]]:
    """Two things a member screen needs that come from the settings Ops saved: whether a manual-handoff platform has a
    working compose link (so posting can hand back a link), and the steps for a podcast directory."""
    wanted = [d.key for d in definitions if d.integration_pattern in ("manual_handoff", "rss_pull", "token_webhook")]
    if not wanted:
        return {}
    rows = await platform_configs.find(
        {"platform": {"$in": wanted}, "workspace_id": {"$in": [PLATFORM_WIDE, workspace_id]}, "enabled": {"$ne": False}},
        {"platform": 1, "workspace_id": 1, "fields": 1, "secrets": 1},
    ).to_list(length=500)
    merged: dict[str, dict[str, Any]] = {}
    secret_keys: dict[str, set[str]] = {}
    own_secret_keys: dict[str, set[str]] = {}
    # The platform-wide row first, then the workspace's own row laid over it.
    for row in sorted(rows, key=lambda r: r["workspace_id"] != PLATFORM_WIDE, reverse=True):
        merged.setdefault(row["platform"], {}).update(row.get("fields") or {})
        keys = set((row.get("secrets") or {}).keys())
        secret_keys.setdefault(row["platform"], set()).update(keys)
        if row["workspace_id"] == workspace_id:
            own_secret_keys.setdefault(row["platform"], set()).update(keys)
    extras: dict[str, dict[str, Any]] = {}
    for definition in definitions:
        if definition.integration_pattern == "token_webhook":
            extras[definition.key] = {
                "webhook_ready": "webhook_url" in secret_keys.get(definition.key, set()),
                "webhook_own": "webhook_url" in own_secret_keys.get(definition.key, set()),
            }
            continue
        fields = merged.get(definition.key)
        if fields is None:
            continue
        if definition.integration_pattern == "manual_handoff":
            extras[definition.key] = {"manual_handoff_ready": template_problem(str(fields.get("compose_url_template") or "")) is None}
        elif fields.get("submission_url"):
            extras[definition.key] = {"directory": {
                "submission_url": fields.get("submission_url"),
                "member_steps": fields.get("member_steps", ""),
                "require_listing_url": bool(fields.get("require_listing_url", True)),
            }}
    return extras


@router.get("")
async def get_platforms(
    status: Optional[str] = Query(None, description="Filter by status: active | partial | planned"),
    pipeline: Optional[str] = Query(None, description="Filter by pipeline: text | image | video | audio"),
    modality: Optional[str] = Query(None, description="Only platforms that can really take this kind of content: text | image | video | audio"),
    user: dict = Depends(get_current_user),
    x_workspace_id: Optional[str] = Header(default=None, alias="X-Workspace-Id"),
) -> dict[str, Any]:
    import_all()
    # `visibility` is recorded for later but is not applied yet: every platform is still listed.
    platforms = list_platforms(status=status, pipeline=pipeline, modality=modality)  # type: ignore[arg-type]
    workspace_id = await _caller_workspace(user, x_workspace_id)
    availability = await availability_for_all(workspace_id, platforms) if workspace_id else {}
    extras = await _member_extras(platforms, workspace_id) if workspace_id else {}
    rows = []
    for p in platforms:
        row = _serialize(p)
        found = availability.get(p.key)
        row["availability"] = {"value": found.value, "reason": found.reason} if found else None
        row.update(extras.get(p.key, {}))
        rows.append(row)
    return {"platforms": rows, "total": len(rows)}


@router.get("/{key}")
async def get_platform_detail(
    key: str,
    user: dict = Depends(get_current_user),
    x_workspace_id: Optional[str] = Header(default=None, alias="X-Workspace-Id"),
) -> dict[str, Any]:
    import_all()
    definition = get_platform(key)
    if definition is None:
        raise HTTPException(status_code=404, detail=f"Unknown platform: {key}")
    row = _serialize(definition)
    workspace_id = await _caller_workspace(user, x_workspace_id)
    found = await platform_availability(definition.key, workspace_id) if workspace_id else None
    row["availability"] = {"value": found.value, "reason": found.reason} if found else None
    if workspace_id:
        row.update((await _member_extras([definition], workspace_id)).get(definition.key, {}))
    return row


class ListingBody(BaseModel):
    listing_url: str = Field(..., min_length=8, max_length=500)
    status: Literal["submitted", "live", "rejected"] = "submitted"


def _directory(key: str) -> PlatformDefinition:
    import_all()
    definition = get_platform(key)
    if definition is None:
        raise HTTPException(status_code=404, detail=f"Unknown platform: {key}")
    if definition.integration_pattern != "rss_pull":
        raise HTTPException(status_code=400, detail="Only podcast directories have a listing.")
    return definition


@router.get("/{key}/listing")
async def get_my_listing(key: str, ctx: WorkspaceContext = Depends(require("manage_connections"))) -> dict[str, Any]:
    """This workspace's listing for a podcast directory, or nulls when it has none yet."""
    _directory(key)
    doc = await platform_listings.find_one({"workspace_id": ctx.workspace_id, "platform_key": key})
    if not doc:
        return {"listing_url": None, "status": None, "submitted_at": None}
    return {"listing_url": doc.get("listing_url"), "status": doc.get("status"), "submitted_at": doc.get("submitted_at")}


@router.put("/{key}/listing")
async def save_my_listing(key: str, body: ListingBody, ctx: WorkspaceContext = Depends(require("manage_connections"))) -> dict[str, Any]:
    """Records the link a member got from a podcast directory and where it stands. Nothing checks it: the status is
    whatever the member says."""
    definition = _directory(key)
    if not body.listing_url.strip().lower().startswith("https://"):
        raise HTTPException(status_code=400, detail="Paste the full link, starting with https://")
    now = datetime.now(timezone.utc)
    await platform_listings.update_one(
        {"workspace_id": ctx.workspace_id, "platform_key": key},
        {
            "$set": {"listing_url": body.listing_url.strip(), "status": body.status, "updated_at": now},
            "$setOnInsert": {"submitted_at": now, "submitted_by": ctx.user_id, "note": ""},
        },
        upsert=True,
    )
    await record_platform_event(
        event="listing.updated", definition=definition, actor_user_id=ctx.user_id, actor_role=ctx.role,
        workspace_id=ctx.workspace_id, description=f"The {definition.label} listing was set to {body.status}.",
        subject_workspace_id=ctx.workspace_id,
    )
    return {"saved": True, "status": body.status}


class WebhookConnectionBody(BaseModel):
    webhook_url: str = Field(..., min_length=8, max_length=1000)
    label: Optional[str] = Field(None, max_length=100)


def _webhook_platform(key: str) -> PlatformDefinition:
    import_all()
    definition = get_platform(key)
    if definition is None:
        raise HTTPException(status_code=404, detail=f"Unknown platform: {key}")
    if definition.integration_pattern != "token_webhook":
        raise HTTPException(status_code=400, detail="Only webhook platforms take a webhook address.")
    return definition


@router.get("/{key}/connection")
async def get_my_webhook(key: str, ctx: WorkspaceContext = Depends(require("manage_connections"))) -> dict[str, Any]:
    """Whether this workspace has its own webhook address saved for the platform. The address itself is never returned."""
    _webhook_platform(key)
    row = await platform_configs.find_one({"workspace_id": ctx.workspace_id, "platform": key}, {"label": 1, "secrets": 1})
    own = bool(row and "webhook_url" in (row.get("secrets") or {}))
    return {"webhook_set": own, "label": (row or {}).get("label")}


@router.put("/{key}/connection")
async def save_my_webhook(key: str, body: WebhookConnectionBody, ctx: WorkspaceContext = Depends(require("manage_connections"))) -> dict[str, Any]:
    """Saves this workspace's own webhook address for a platform that posts by webhook. It must be https and reach a public
    address. It is stored encrypted and never shown again."""
    from app.pipelines.publish.generic.safe_url import UnsafeUrl, assert_safe_url
    from app.pipelines.publish.platform_config_store import save_platform_config

    definition = _webhook_platform(key)
    found = await platform_availability(definition.key, ctx.workspace_id)
    if found.value != "connectable":
        raise HTTPException(status_code=409, detail={"code": "PLATFORM_NOT_AVAILABLE", "message": f"{definition.label} isn't available for this workspace right now."})
    try:
        await assert_safe_url(body.webhook_url.strip())
    except UnsafeUrl as exc:
        raise HTTPException(status_code=400, detail=f"Webhook address: {exc}")

    # A workspace's own row only carries its address and label; the template and limits stay platform-wide.
    await save_platform_config(
        ctx.workspace_id, definition.key, (body.label or "").strip() or None, True, {}, {"webhook_url": body.webhook_url.strip()},
        created_by=ctx.user_id,
    )
    await record_platform_event(
        event="connection.webhook_saved", definition=definition, actor_user_id=ctx.user_id, actor_role=ctx.role,
        workspace_id=ctx.workspace_id, description=f"A webhook address for {definition.label} was saved.",
        subject_workspace_id=ctx.workspace_id,
    )
    return {"saved": True}


@router.delete("/{key}/connection")
async def remove_my_webhook(key: str, ctx: WorkspaceContext = Depends(require("manage_connections"))) -> dict[str, Any]:
    """Removes this workspace's own webhook address. The platform-wide one, if Ops set it, then applies again."""
    from app.pipelines.publish.platform_config_store import delete_platform_config

    definition = _webhook_platform(key)
    await delete_platform_config(ctx.workspace_id, definition.key)
    await record_platform_event(
        event="connection.webhook_removed", definition=definition, actor_user_id=ctx.user_id, actor_role=ctx.role,
        workspace_id=ctx.workspace_id, description=f"The webhook address for {definition.label} was removed.",
        subject_workspace_id=ctx.workspace_id,
    )
    return {"removed": True}
