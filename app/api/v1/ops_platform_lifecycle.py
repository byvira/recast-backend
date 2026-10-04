"""Ops Dashboard: the platform catalog, one platform's detail, and the controls on it (stage, rollout, live test,
registry facts, test send).

Everything here is for Recast staff. Reading needs platform staff (or a master admin). Changing a stage, widening or
narrowing the rollout and the other owner actions need a master admin; recording a live test and verifying the
registry facts are open to staff. Writes carry the `version` of the record they were based on and answer 409 when it
has moved, so two people cannot overwrite each other.
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.v1.platforms import _serialize
from app.core.auth import require_platform_staff
from app.core.middleware import limiter
from app.db.mongo import content_pieces, platform_configs, workspace_connections, workspaces
from app.models.platform_ops import FactsVerified, LiveTestRecord, PurgeCredentials, RolloutChange, StageChange, TestSend
from app.pipelines.platform_ops.events import record_platform_event
from app.pipelines.platform_ops.holds import count_impact
from app.pipelines.platform_ops.lifecycle import TransitionRefused, change_stage
from app.pipelines.platform_ops.readiness import BLOCKING, build_readiness
from app.pipelines.platform_ops.store import VersionConflict, get_ops, get_ops_many, save_ops
from app.pipelines.publish.generic.adapter import ConfigPublisherAdapter, is_adapter_pattern
from app.pipelines.publish.base import PublishRequest
from app.pipelines.publish.platform_config_store import PLATFORM_WIDE, get_effective_config, get_platform_config
from app.pipelines.publish.spine import MULTI_PICTURE_LIMITS, platform_key
from app.platforms.base import PlatformDefinition, get_platform, import_all, list_platforms

router = APIRouter()
logger = logging.getLogger(__name__)

# The Meta app is still in tester mode and Google's verification is pending, so a platform behind either can only be
# used by accounts those providers have approved, whatever Ops sets as the rollout.
EXTERNAL_GATES = {
    "instagram": "Meta app in tester mode",
    "facebook": "Meta app in tester mode",
    "threads": "Meta app in tester mode",
    "youtube": "Google verification pending",
    "google": "Google verification pending",
}


async def _rollout_workspaces(ops: dict) -> list[dict[str, str]]:
    """The workspaces a "selected" rollout names, with their names, so the screen can show more than ids."""
    ids = list((ops.get("rollout") or {}).get("workspace_ids") or [])
    if not ids:
        return []
    found = {w["id"]: w.get("name", "") for w in await workspaces.find({"id": {"$in": ids}}, {"id": 1, "name": 1}).to_list(length=len(ids))}
    return [{"id": i, "name": found.get(i, "")} for i in ids]


def _definition(key: str) -> PlatformDefinition:
    import_all()
    definition = get_platform(key)
    if definition is None:
        raise HTTPException(status_code=404, detail=f"Unknown platform: {key}")
    return definition


def _require_owner(user: dict) -> None:
    if not user.get("is_master_admin"):
        raise HTTPException(status_code=403, detail={"code": "owner_only", "message": "Only the Ops owner can do this."})


def _workspace_of(user: dict) -> str:
    return user.get("default_workspace_id") or ""


def _conflict() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"code": "version_conflict", "message": "Someone else changed this platform. Reload and try again."},
    )


async def _connection_summary() -> dict[str, dict[str, int]]:
    """Per platform key: how many workspaces are connected, how they are doing, and how many tokens end within 7 days."""
    soon = datetime.now(timezone.utc) + timedelta(days=7)
    rows = await workspace_connections.find(
        {"is_active": True}, {"platform": 1, "health": 1, "expires_at": 1}
    ).to_list(length=20000)
    out: dict[str, dict[str, int]] = {}
    for row in rows:
        entry = out.setdefault(row["platform"], {"connected": 0, "healthy": 0, "degraded": 0, "broken": 0, "expiring": 0})
        entry["connected"] += 1
        state = (row.get("health") or {}).get("state", "healthy")
        entry[state if state in ("healthy", "degraded", "broken") else "healthy"] += 1
        expires = row.get("expires_at")
        if isinstance(expires, datetime):
            expires = expires if expires.tzinfo else expires.replace(tzinfo=timezone.utc)
            if expires <= soon:
                entry["expiring"] += 1
    return out


async def _posts_last_14_days() -> dict[str, int]:
    since = datetime.now(timezone.utc) - timedelta(days=14)
    rows = await content_pieces.aggregate([
        {"$match": {"publish_status": "published", "published_at": {"$gte": since}}},
        {"$group": {"_id": {"$ifNull": ["$publish_target", "$platform"]}, "n": {"$sum": 1}}},
    ]).to_list(length=500)
    counts: dict[str, int] = {}
    for row in rows:
        key = platform_key(row["_id"])
        counts[key] = counts.get(key, 0) + row["n"]
    return counts


def _health_label(summary: dict[str, int]) -> str:
    if summary.get("broken"):
        return "broken"
    if summary.get("degraded"):
        return "degraded"
    return "healthy" if summary.get("connected") else "none"


@router.get("")
@limiter.limit("30/minute")
async def catalog(request: Request, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    """Every registered platform with its stage, rollout, health, usage and what is blocking it from going live."""
    import_all()
    definitions = list_platforms()
    ops_by_key = await get_ops_many(definitions)
    connections = await _connection_summary()
    posts = await _posts_last_14_days()
    rows = []
    for definition in definitions:
        ops = ops_by_key[definition.key]
        config = await get_platform_config(PLATFORM_WIDE, definition.key)
        summary = connections.get(definition.key, {"connected": 0, "healthy": 0, "degraded": 0, "broken": 0, "expiring": 0})
        readiness = build_readiness(definition, ops, config)
        rows.append({
            **_serialize(definition),
            "ops_stage": ops["ops_stage"],
            "rollout": ops["rollout"],
            "derived": ops["derived"],
            "version": ops["version"],
            "connections": summary,
            "health": _health_label(summary),
            "posts_14d": posts.get(definition.key, 0),
            "can_go_live": readiness["can_go_live"],
            "blockers": readiness["blockers"],
            "ready_checks": [
                sum(1 for c in readiness["checks"] if c["key"] in BLOCKING.get(definition.integration_pattern, ()) and c["state"] == "pass"),
                len(BLOCKING.get(definition.integration_pattern, ())),
            ],
            "waiting_for_developer": readiness["waiting_for_developer"],
            "external_gate": EXTERNAL_GATES.get(definition.key),
        })
    return {"platforms": rows, "total": len(rows)}


@router.get("/workspaces/search")
@limiter.limit("60/minute")
async def search_workspaces(
    request: Request,
    q: str = Query("", max_length=80),
    user: dict = Depends(require_platform_staff),
) -> dict[str, Any]:
    """Workspaces whose name contains `q` (or whose id is exactly `q`), for choosing who a rollout includes."""
    term = q.strip()
    if not term:
        return {"workspaces": []}
    docs = await workspaces.find(
        {"$or": [{"name": {"$regex": re.escape(term), "$options": "i"}}, {"id": term}]}, {"id": 1, "name": 1}
    ).limit(10).to_list(length=10)
    return {"workspaces": [{"id": d["id"], "name": d.get("name", "")} for d in docs]}


@router.get("/{key}")
@limiter.limit("60/minute")
async def detail(request: Request, key: str, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    definition = _definition(key)
    ops = await get_ops(definition)
    config = await get_platform_config(PLATFORM_WIDE, definition.key)
    connections = (await _connection_summary()).get(
        definition.key, {"connected": 0, "healthy": 0, "degraded": 0, "broken": 0, "expiring": 0}
    )
    return {
        **_serialize(definition),
        "max_chars": definition.max_chars,
        "pictures_per_post": MULTI_PICTURE_LIMITS.get(definition.key, 1),
        "has_validator": bool(definition.validator_fn),
        "has_publisher_class": bool(definition.publisher_cls),
        "ops": ops,
        "rollout_workspaces": await _rollout_workspaces(ops),
        "readiness": build_readiness(definition, ops, config),
        "config": config,
        "connections": connections,
        "health": _health_label(connections),
        "external_gate": EXTERNAL_GATES.get(definition.key),
    }


@router.get("/{key}/impact")
@limiter.limit("30/minute")
async def impact(request: Request, key: str, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    """What pausing or retiring this platform would touch, for the confirmation screen."""
    return await count_impact(_definition(key))


@router.post("/{key}/stage")
@limiter.limit("20/minute")
async def set_stage(request: Request, key: str, body: StageChange, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    definition = _definition(key)
    ops = await get_ops(definition)
    if ops["version"] != body.version:
        raise _conflict()
    config = await get_platform_config(PLATFORM_WIDE, definition.key)
    try:
        saved = await change_stage(
            definition, ops, config, to=body.to, actor=user, workspace_id=_workspace_of(user), reason=body.reason.strip(),
            member_message=body.member_message.strip(), confirm_name=body.confirm_name,
        )
    except VersionConflict:
        raise _conflict()
    except TransitionRefused as refused:
        status = 403 if refused.code == "owner_only" else 400
        raise HTTPException(
            status_code=status,
            detail={"code": refused.code, "message": refused.message, "blockers": refused.blockers},
        )
    return {"ops": saved, "readiness": build_readiness(definition, saved, config)}


@router.put("/{key}/rollout")
@limiter.limit("20/minute")
async def set_rollout(request: Request, key: str, body: RolloutChange, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    _require_owner(user)
    definition = _definition(key)
    ops = await get_ops(definition)
    if ops["version"] != body.version:
        raise _conflict()

    ids = sorted(set(body.workspace_ids)) if body.scope == "selected" else []
    if body.scope == "selected":
        if not ids:
            raise HTTPException(status_code=400, detail={"code": "no_workspaces", "message": "Pick at least one workspace."})
        known = {w["id"] for w in await workspaces.find({"id": {"$in": ids}}, {"id": 1}).to_list(length=len(ids))}
        missing = [i for i in ids if i not in known]
        if missing:
            raise HTTPException(status_code=400, detail={"code": "unknown_workspace", "message": "One of those workspaces does not exist."})

    rollout = {"scope": body.scope, "workspace_ids": ids}
    try:
        saved = await save_ops(
            definition, {"rollout": rollout}, expected_version=body.version, actor_id=user["id"],
        )
    except VersionConflict:
        raise _conflict()
    await record_platform_event(
        event="platform.rollout_changed", definition=definition, actor_user_id=user["id"], actor_role="owner",
        workspace_id=_workspace_of(user),
        description=f"{definition.label} rollout set to {body.scope.replace('_', ' ')}"
                    + (f" ({len(ids)} workspaces)." if ids else "."),
        before=ops["rollout"], after=rollout,
    )
    return {"ops": saved}


@router.post("/{key}/purge-credentials")
@limiter.limit("5/hour")
async def purge_credentials(request: Request, key: str, body: PurgeCredentials, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    """Permanently removes what is stored to sign in to a retired platform: connection tokens and saved settings secrets.
    Retiring keeps them on purpose, so this is a separate, owner-only step that asks for the platform's name."""
    _require_owner(user)
    definition = _definition(key)
    ops = await get_ops(definition)
    if ops["ops_stage"] != "retired":
        raise HTTPException(status_code=400, detail={"code": "not_retired", "message": "Only a retired platform's credentials can be purged."})
    if body.confirm_name.strip().lower() != definition.label.lower():
        raise HTTPException(status_code=400, detail={"code": "confirm_name", "message": f"Type {definition.label} to confirm."})

    now = datetime.now(timezone.utc)
    connections = await workspace_connections.update_many(
        {"platform": key},
        {"$set": {"is_active": False, "credentials_purged_at": now}, "$unset": {"access_token": "", "refresh_token": ""}},
    )
    settings = await platform_configs.update_many({"platform": key}, {"$set": {"secrets": {}, "enabled": False, "updated_at": now}})
    await record_platform_event(
        event="platform.credentials_purged", definition=definition, actor_user_id=user["id"], actor_role="owner",
        workspace_id=_workspace_of(user),
        description=f"Stored credentials for {definition.label} were purged ({connections.modified_count} connections, {settings.modified_count} settings).",
        reason=body.reason.strip(),
    )
    return {"connections": connections.modified_count, "settings": settings.modified_count}


@router.post("/{key}/auth-test")
@limiter.limit("20/minute")
async def record_live_test(request: Request, key: str, body: LiveTestRecord, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    """Records that a real post (or delivery, or link) was tried and worked. This is the one go-live check that
    only a person can confirm."""
    definition = _definition(key)
    if definition.integration_pattern in ("rss_pull", "generation_only"):
        raise HTTPException(status_code=400, detail={"code": "not_applicable", "message": "There is nothing to test live here."})
    ops = await get_ops(definition)
    if ops["version"] != body.version:
        raise _conflict()
    record = {
        "tested_at": datetime.now(timezone.utc),
        "tested_by": user.get("name") or "Staff",
        "tested_by_id": user["id"],
        "note": body.note.strip(),
    }
    try:
        saved = await save_ops(definition, {"auth_test": record}, expected_version=body.version, actor_id=user["id"])
    except VersionConflict:
        raise _conflict()
    await record_platform_event(
        event="platform.auth_test_recorded", definition=definition, actor_user_id=user["id"],
        actor_role="owner" if user.get("is_master_admin") else "staff", workspace_id=_workspace_of(user),
        description=f"A live test of {definition.label} was recorded." + (f" Note: {record['note']}" if record["note"] else ""),
    )
    return {"ops": saved}


@router.post("/{key}/facts-verified")
@limiter.limit("20/minute")
async def verify_facts(request: Request, key: str, body: FactsVerified, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    """Records that the registry's facts for this platform were checked against the platform's own documentation."""
    definition = _definition(key)
    if not body.source_url.lower().startswith("https://"):
        raise HTTPException(status_code=400, detail={"code": "bad_source", "message": "Give the https address of the page you checked."})
    ops = await get_ops(definition)
    if ops["version"] != body.version:
        raise _conflict()
    record = {
        "verified_at": datetime.now(timezone.utc),
        "verified_by": user.get("name") or "Staff",
        "verified_by_id": user["id"],
        "source_url": body.source_url.strip(),
    }
    try:
        saved = await save_ops(definition, {"facts_verified": record}, expected_version=body.version, actor_id=user["id"])
    except VersionConflict:
        raise _conflict()
    await record_platform_event(
        event="platform.facts_verified", definition=definition, actor_user_id=user["id"],
        actor_role="owner" if user.get("is_master_admin") else "staff", workspace_id=_workspace_of(user),
        description=f"The registry facts for {definition.label} were verified against {record['source_url']}.",
    )
    return {"ops": saved}


@router.post("/{key}/test")
@limiter.limit("10/minute")
async def test_send(request: Request, key: str, body: TestSend, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    """Sends one test message through the platform's saved settings (a webhook delivery, or a compose link built).
    It answers in plain language and does not record the live test: a person confirms that separately."""
    definition = _definition(key)
    if not is_adapter_pattern(definition):
        raise HTTPException(
            status_code=400,
            detail={"code": "not_testable", "message": "Only webhook and manual-handoff platforms can be tested from here."},
        )
    config = await get_effective_config(_workspace_of(user), definition.key)
    if config is None:
        raise HTTPException(status_code=400, detail={"code": "no_settings", "message": "Save and switch on the settings first."})

    adapter = ConfigPublisherAdapter(definition, config)
    text = (body.text or "This is a test message from Recast. You can ignore it.").strip()
    request_obj = PublishRequest(
        piece_id="ops-test", user_id=user["id"], brand_id="", platform=definition.key, content=text,
        workspace_id=_workspace_of(user),
    )
    result = await adapter.publish(request_obj)
    if result.manual_action_url:
        return {"ok": True, "kind": "link", "message": "The link was built. Open it to check it fills in the post.",
                "manual_action_url": result.manual_action_url}
    if result.success:
        return {"ok": True, "kind": "delivery", "message": "The webhook accepted the message."}
    return {"ok": False, "kind": "error", "message": result.error_message or "The test did not go through."}
