"""Ops: Sources. The switch, usage, the sources that stopped themselves, and the abuse watch.

Reading needs view on the Platform area, any change needs edit (see `area_guard`). Each change is written to the staff audit with who, what, before, after and the reason."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from app.core.auth import get_current_user
from app.core.middleware import limiter
from app.core.staff import audit
from app.sources import ops

router = APIRouter()


class SourcesSwitch(BaseModel):
    off: bool
    message: Optional[str] = Field(None, max_length=200)
    reason: str = Field("", max_length=300)


@router.get("")
@limiter.limit("60/minute")
async def overview(request: Request, days: int = Query(7, ge=1, le=30), user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return await ops.overview(days)


@router.put("/switch")
@limiter.limit("30/minute")
async def switch(request: Request, body: SourcesSwitch, user: dict = Depends(get_current_user)) -> dict[str, Any]:
    changed = await ops.set_switch(body.off, body.message)
    await audit(user, "sources.switch", "sources", before=changed["before"], after=changed["after"], reason=body.reason)
    return await ops.switch()
