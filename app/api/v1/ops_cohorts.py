"""
Workspace cohorts — CRUD only. Scaffolding for Odette's Brand Consistency
Radar: cohorts can be created and members assigned, but no brand-token
adherence scoring exists yet (what the radar's per-axis percentages would
actually measure) — that analysis is separate, later work. Owner-gated, same
pattern as /api/v1/ops/platforms.
"""

import logging
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, require_ops_admin
from app.db.mongo import workspace_cohorts
from app.models.cohort import WorkspaceCohort, WorkspaceCohortWrite

router = APIRouter()
logger = logging.getLogger(__name__)

_OWNER = require_ops_admin("manage_workspace_settings")


@router.get("")
@limiter.limit("30/minute")
async def list_cohorts(request: Request, ctx: WorkspaceContext = Depends(_OWNER)) -> dict:
    rows = await workspace_cohorts.find({"workspace_id": ctx.workspace_id}).to_list(200)
    return {"cohorts": [WorkspaceCohort(**r) for r in rows], "total": len(rows)}


@router.post("")
@limiter.limit("20/minute")
async def create_cohort(
    request: Request, body: WorkspaceCohortWrite, ctx: WorkspaceContext = Depends(_OWNER)
) -> WorkspaceCohort:
    now = datetime.now(timezone.utc)
    doc = {
        "id": str(uuid4()),
        "workspace_id": ctx.workspace_id,
        "name": body.name,
        "member_user_ids": body.member_user_ids,
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
    res = await workspace_cohorts.update_one(
        {"id": cohort_id, "workspace_id": ctx.workspace_id},
        {"$set": {"name": body.name, "member_user_ids": body.member_user_ids, "updated_at": now}},
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
