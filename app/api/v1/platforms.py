"""
Platform registry API — read-only for now (Stage 1). Stage 2 adds
/api/v1/ops/platforms CRUD for config-driven entries in platform_configs;
this endpoint will merge those in once they exist (see the TODO below).
"""

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.auth import get_current_user
from app.platforms.base import PlatformDefinition, get_platform, import_all, list_platforms

router = APIRouter()


def _serialize(p: PlatformDefinition) -> dict[str, Any]:
    """Public shape — omits nothing sensitive today (no secrets live on
    PlatformDefinition itself; those arrive with platform_configs in Stage 2,
    which will be write-only per docs/PLATFORM_REGISTRY_PLAN.md's rules)."""
    return {
        "key": p.key,
        "label": p.label,
        "category": p.category,
        "pipelines": sorted(p.pipelines),
        "native_formats": p.native_formats,
        "shapes": p.shapes,
        "mode": p.mode,
        "integration_pattern": p.integration_pattern,
        "status": p.status,
        "audit_required": p.audit_required,
        "rate_limits": p.rate_limits,
        "policy_constraints": p.policy_constraints,
        "tone_profile": p.tone_profile,
        "access_notes": p.access_notes,
        "confidence": p.confidence,
        "connectable": p.publisher_cls is not None,
        "has_analytics": p.analytics_fetcher_cls is not None,
    }


@router.get("")
async def get_platforms(
    status: Optional[str] = Query(None, description="Filter by status: active | partial | planned"),
    pipeline: Optional[str] = Query(None, description="Filter by pipeline: text | image | video | audio"),
    _user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    import_all()
    platforms = list_platforms(status=status, pipeline=pipeline)  # type: ignore[arg-type]
    return {
        "platforms": [_serialize(p) for p in platforms],
        "total": len(platforms),
    }


@router.get("/{key}")
async def get_platform_detail(
    key: str,
    _user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    import_all()
    definition = get_platform(key)
    if definition is None:
        raise HTTPException(status_code=404, detail=f"Unknown platform: {key}")
    return _serialize(definition)
