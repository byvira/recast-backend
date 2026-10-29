"""The public status page's data, and where members' screens report a crash. Neither needs a sign-in.

The status answer carries no provider names, no workspace data and no internal detail: part names, states, daily colours and
incident words written for the public. It is cached for half a minute.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from app.core.middleware import limiter
from app.health import errors, status

router = APIRouter()
client_errors_router = APIRouter()


@router.get("")
@limiter.limit("120/minute")
async def get_status(request: Request, response: Response) -> dict[str, Any]:
    """Every part's state now, its last 90 days, incidents happening now, planned maintenance, and the last two weeks."""
    response.headers["Cache-Control"] = "public, max-age=30"
    return await status.summary()


@router.get("/incidents")
@limiter.limit("60/minute")
async def get_incident_history(request: Request, page: int = Query(1, ge=1, le=500)) -> dict[str, Any]:
    return await status.history(page)


@router.get("/incidents/{incident_id}")
@limiter.limit("60/minute")
async def get_incident(request: Request, incident_id: str) -> dict[str, Any]:
    found = await status.incident(incident_id[:64])
    if not found:
        raise HTTPException(status_code=404, detail="That incident was not found.")
    return found


class Subscribe(BaseModel):
    email: str = Field(..., max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@router.post("/subscribe")
@limiter.limit("5/minute")
async def subscribe(request: Request, body: Subscribe) -> dict[str, Any]:
    """Ask for an email when something is wrong and when it is fixed. A link to confirm is sent first."""
    await status.subscribe(str(body.email))
    return {"ok": True, "message": "Check your email and confirm, and you will hear from us when something is wrong."}


class SubscriptionAction(BaseModel):
    action: Literal["confirm", "unsubscribe"]
    token: str = Field(..., min_length=10, max_length=80)


@router.post("/subscription")
@limiter.limit("20/minute")
async def subscription(request: Request, body: SubscriptionAction) -> dict[str, Any]:
    """The links in status emails: confirm a subscription, or stop the emails."""
    done = await (status.confirm(body.token) if body.action == "confirm" else status.unsubscribe(body.token))
    if not done:
        raise HTTPException(status_code=404, detail="That link has already been used or is no longer valid.")
    return {"ok": True}


class ClientError(BaseModel):
    message: str = Field("", max_length=2000)
    kind: str = Field("Error", max_length=80)
    stack: str = Field("", max_length=8000)
    page: str = Field("", max_length=300)
    component: Optional[str] = Field(None, max_length=120)
    release: Optional[str] = Field(None, max_length=40)
    digest: Optional[str] = Field(None, max_length=80)
    #: the reference the crash screen shows the member, so a support ticket that quotes it is linked to this error
    ref: Optional[str] = Field(None, pattern=r"^CR-[A-Z0-9]{8}$")


#: browser noise that is not a fault in Ekavo (extensions, a resize loop the browser reports itself)
_NOISE = ("ResizeObserver loop", "Script error.", "chrome-extension://", "moz-extension://", "safari-extension://")


@client_errors_router.post("", status_code=204)
@limiter.limit("30/minute")
async def report_client_error(request: Request, body: ClientError) -> Response:
    """A screen that crashed in a member's browser. Recorded in the Ops error log, grouped like every other error."""
    text = f"{body.message} {body.stack}"
    if not any(n in text for n in _NOISE) and (body.message or body.stack):
        where = body.page.split("?")[0]
        if body.component:
            where = f"{where} > {body.component}"
        errors.capture(
            source="browser", kind=body.kind or "Error", message=body.message, where=where, stack=body.stack,
            workspace_id=request.headers.get("x-workspace-id"), rel=body.release,
            extra={k: v for k, v in {"digest": body.digest, "ref": body.ref}.items() if v} or None,
        )
    return Response(status_code=204)
