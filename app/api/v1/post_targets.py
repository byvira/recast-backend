"""Where a connected account's posts go: the connected account itself (the default) or, where the platform allows it, a Company Page.

Posts go to the connected account unless the member chooses otherwise here. LinkedIn Company Pages are built end to end but stay off
until LinkedIn approves the app for them (switched on by Ops per platform, see destinations_support.py): before then the choice is listed as not available yet, and
choosing it is refused with that reason. Facebook Pages keep their own picker (PUT /oauth/facebook/page)."""
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.pipelines.publish.destinations_support import destination_enabled
from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, require
from app.db.mongo import workspace_connections

router = APIRouter()

_WAITING = "Company Pages open up once LinkedIn approves Ekavo for them. Posts go to your profile until then."


def _profile_option(connection: dict, selected: bool) -> dict:
    return {
        "kind": "profile", "id": connection.get("platform_user_id") or "", "name": connection.get("username") or "Your profile",
        "selected": selected, "available": True, "reason": None,
    }


async def _linkedin_options(connection: dict) -> list[dict]:
    target = connection.get("post_target") or {}
    open_for_pages = await destination_enabled("linkedin", "organization")
    on_org = target.get("kind") == "organization" and open_for_pages
    options = [_profile_option(connection, selected=not on_org)]
    if not open_for_pages:
        options.append({"kind": "organization", "id": "", "name": "A Company Page", "selected": False, "available": False, "reason": _WAITING})
        return options

    from app.pipelines.publish.linkedin.oauth import list_admin_organizations
    from app.pipelines.publish.token_store import decrypt_token

    token = decrypt_token(connection["access_token"]) if connection.get("access_token") else ""
    organizations = await list_admin_organizations(token) if token else []
    if not organizations:
        options.append({
            "kind": "organization", "id": "", "name": "A Company Page", "selected": False, "available": False,
            "reason": "No Company Page was found on this LinkedIn sign-in. Connect LinkedIn again and allow Company Pages.",
        })
    for organization in organizations:
        options.append({
            "kind": "organization", "id": organization["id"], "name": organization["name"],
            "selected": on_org and str(target.get("id")) == organization["id"], "available": True, "reason": None,
        })
    return options


@router.get("/{platform}")
@limiter.limit("60/minute")
async def list_targets(
    request: Request, platform: Literal["linkedin"], ctx: WorkspaceContext = Depends(require("manage_connections")),
) -> dict:
    """The choices for where this account's posts go, and which one is in use."""
    connection = await workspace_connections.find_one({"workspace_id": ctx.workspace_id, "platform": platform, "is_active": True})
    if not connection:
        raise HTTPException(status_code=404, detail="Connect this account first.")
    return {"platform": platform, "options": await _linkedin_options(connection)}


class SetTargetRequest(BaseModel):
    kind: Literal["profile", "organization"]
    id: Optional[str] = Field(default=None, max_length=64)


@router.put("/{platform}")
@limiter.limit("20/minute")
async def set_target(
    request: Request, platform: Literal["linkedin"], body: SetTargetRequest, ctx: WorkspaceContext = Depends(require("manage_connections")),
) -> dict:
    """Chooses where this account's posts go. "profile" is the default and always allowed."""
    connection = await workspace_connections.find_one({"workspace_id": ctx.workspace_id, "platform": platform, "is_active": True})
    if not connection:
        raise HTTPException(status_code=404, detail="Connect this account first.")

    if body.kind == "profile":
        await workspace_connections.update_one({"_id": connection["_id"]}, {"$unset": {"post_target": ""}})
        return {"platform": platform, "options": await _linkedin_options({k: v for k, v in connection.items() if k != "post_target"})}

    if not await destination_enabled("linkedin", "organization"):
        raise HTTPException(status_code=409, detail=_WAITING)
    options = await _linkedin_options(connection)
    chosen = next((o for o in options if o["kind"] == "organization" and o["available"] and o["id"] == (body.id or "")), None)
    if chosen is None:
        raise HTTPException(status_code=404, detail="That Company Page isn't one this LinkedIn sign-in manages.")
    target = {"kind": "organization", "id": chosen["id"], "name": chosen["name"], "urn": f"urn:li:organization:{chosen['id']}"}
    await workspace_connections.update_one({"_id": connection["_id"]}, {"$set": {"post_target": target}})
    return {"platform": platform, "options": await _linkedin_options({**connection, "post_target": target})}
