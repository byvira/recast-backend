"""Ops: chat apps. A switch for each, usage, and the abuse watch.

Reading needs view on the Platform area, any change needs edit (see `area_guard`). Each change is written to the staff audit with who, what, before, after and the reason."""

from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from app.channels import ops
from app.core.auth import get_current_user
from app.core.middleware import limiter
from app.core.staff import audit

router = APIRouter()


class ChannelSwitch(BaseModel):
    channel: Literal["whatsapp", "telegram"]
    off: bool
    message: Optional[str] = Field(None, max_length=200)
    reason: str = Field("", max_length=300)


@router.get("")
@limiter.limit("60/minute")
async def overview(request: Request, hours: int = Query(24, ge=1, le=168), user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return await ops.overview(hours)


@router.put("/switch")
@limiter.limit("30/minute")
async def switch(request: Request, body: ChannelSwitch, user: dict = Depends(get_current_user)) -> dict[str, Any]:
    changed = await ops.set_switch(body.channel, body.off, body.message)
    await audit(user, "channels.switch", body.channel, before=changed["before"], after=changed["after"], reason=body.reason)
    return await ops.overview()


class ReminderLimits(BaseModel):
    whatsapp_per_day: Optional[float] = Field(None, ge=0, le=500)
    telegram_per_day: Optional[float] = Field(None, ge=0, le=500)
    whatsapp_per_month: Optional[float] = Field(None, ge=0, le=20000)
    template_rupees: Optional[float] = Field(None, ge=0, le=10)
    reason: str = Field("", max_length=300)


@router.put("/reminders")
@limiter.limit("20/minute")
async def change_reminders(request: Request, body: ReminderLimits, user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """The caps on reminders (a WhatsApp template message after 24 hours is charged by Meta, so its daily and monthly caps start at zero) and the estimated cost of one."""
    from app.channels import reminders

    before = await reminders.limits()
    after = await reminders.set_limits(body.model_dump(exclude={"reason"}, exclude_none=True))
    await audit(user, "channels.reminders", "reminder caps", before=before, after=after, reason=body.reason)
    return {"limits": after, "spend": await reminders.spend()}
