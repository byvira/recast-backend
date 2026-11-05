"""
Workspace groups (cohorts): named groups of the workspace's people, such as "Marketing". The owner makes them and chooses who is
in each. Only people who are active members of this workspace can be put in one, each person is listed once, and a name is used
once. Owner-gated, same pattern as /api/v1/ops/platforms.
"""

import logging
import re
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, require_ops_admin
from app.db.mongo import workspace_cohorts, workspace_members
from app.models.cohort import WorkspaceCohort, WorkspaceCohortWrite

router = APIRouter()
logger = logging.getLogger(__name__)

_OWNER = require_ops_admin("manage_workspace_settings")
MAX_GROUPS = 50
MAX_PEOPLE = 200


async def _checked(ctx: WorkspaceContext, body: WorkspaceCohortWrite, *, own_id: str | None = None) -> tuple[str, list[str]]:
    """The group's name and member list made safe to save, or a plain refusal."""
    name = " ".join(body.name.split())[:60]
    if not name:
        raise HTTPException(status_code=400, detail="Give the group a name.")
    people = list(dict.fromkeys(body.member_user_ids))[:MAX_PEOPLE]
    if people:
        found = {m["user_id"] async for m in workspace_members.find(
            {"workspace_id": ctx.workspace_id, "user_id": {"$in": people}, "status": "active"}, {"user_id": 1})}
        if len(found) != len(people):
            raise HTTPException(status_code=400, detail="Some of those people are not in this workspace.")
    clash = {"workspace_id": ctx.workspace_id, "name": {"$regex": f"^{re.escape(name)}$", "$options": "i"}}
    if own_id:
        clash["id"] = {"$ne": own_id}
    if await workspace_cohorts.find_one(clash, {"_id": 1}):
        raise HTTPException(status_code=409, detail="A group with that name already exists.")
    return name, people


@router.get("")
@limiter.limit("30/minute")
async def list_cohorts(request: Request, ctx: WorkspaceContext = Depends(_OWNER)) -> dict:
    rows = await workspace_cohorts.find({"workspace_id": ctx.workspace_id}).to_list(200)
    return {"cohorts": [WorkspaceCohort(**r) for r in rows], "total": len(rows)}


CONSISTENCY_DAYS = 30


@router.get("/consistency")
@limiter.limit("20/minute")
async def consistency(request: Request, ctx: WorkspaceContext = Depends(_OWNER)) -> dict:
    """How each group keeps to the brand over the last 30 days, from what its members' posts went through. A group with nobody in it
    is listed with no posts. Read only."""
    from datetime import timedelta

    from app.db.mongo import content_pieces
    from app.shared.group_consistency import score_group

    groups = await workspace_cohorts.find({"workspace_id": ctx.workspace_id}).to_list(MAX_GROUPS)
    since = datetime.now(timezone.utc) - timedelta(days=CONSISTENCY_DAYS)
    out = []
    for g in groups:
        people = g.get("member_user_ids") or []
        posts = await content_pieces.find(
            {"workspace_id": ctx.workspace_id, "user_id": {"$in": people}, "deleted": {"$ne": True}, "created_at": {"$gte": since}},
            {"quality_passed": 1, "flagged_for_review": 1, "approval_status": 1},
        ).to_list(5000) if people else []
        out.append({"id": g["id"], "name": g["name"], "people": len(people), **score_group(posts)})
    return {"days": CONSISTENCY_DAYS, "groups": out}


@router.post("")
@limiter.limit("20/minute")
async def create_cohort(
    request: Request, body: WorkspaceCohortWrite, ctx: WorkspaceContext = Depends(_OWNER)
) -> WorkspaceCohort:
    now = datetime.now(timezone.utc)
    if await workspace_cohorts.count_documents({"workspace_id": ctx.workspace_id}) >= MAX_GROUPS:
        raise HTTPException(status_code=400, detail=f"A workspace can have {MAX_GROUPS} groups. Remove one to add another.")
    name, people = await _checked(ctx, body)
    doc = {
        "id": str(uuid4()),
        "workspace_id": ctx.workspace_id,
        "name": name,
        "member_user_ids": people,
        "created_by": ctx.user_id,
        "created_at": now,
        "updated_at": now,
    }
    await workspace_cohorts.insert_one(doc)
    return WorkspaceCohort(**doc)


@router.put("/{cohort_id}")
@limiter.limit("20/minute")
async def update_cohort(
    request: Request,
    cohort_id: str,
    body: WorkspaceCohortWrite,
    ctx: WorkspaceContext = Depends(_OWNER),
) -> WorkspaceCohort:
    now = datetime.now(timezone.utc)
    name, people = await _checked(ctx, body, own_id=cohort_id)
    res = await workspace_cohorts.update_one(
        {"id": cohort_id, "workspace_id": ctx.workspace_id},
        {"$set": {"name": name, "member_user_ids": people, "updated_at": now}},
    )
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Cohort not found.")
    doc = await workspace_cohorts.find_one({"id": cohort_id, "workspace_id": ctx.workspace_id})
    return WorkspaceCohort(**doc)


@router.delete("/{cohort_id}")
@limiter.limit("20/minute")
async def delete_cohort(request: Request, cohort_id: str, ctx: WorkspaceContext = Depends(_OWNER)) -> dict:
    await workspace_cohorts.delete_one({"id": cohort_id, "workspace_id": ctx.workspace_id})
    return {"deleted": True, "id": cohort_id}
