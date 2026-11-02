"""Device notifications: turning them on or off for a device, the devices a member has, which kinds they want, and a test.

Everything here is the signed-in member's own: a device is never shown to or changed by anyone else.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.auth import get_current_user
from app.core.middleware import limiter
from app.shared import push

router = APIRouter()


@router.get("/config")
@limiter.limit("60/minute")
async def config(request: Request, user: dict = Depends(get_current_user)) -> dict:
    """Whether device notifications are set up on the server, the key a browser needs to turn them on, and the kinds a member can
    choose."""
    return {
        "enabled": push.enabled(),
        "public_key": push.settings.VAPID_PUBLIC_KEY if push.enabled() else None,
        "groups": [{"key": k, "label": v} for k, v in push.GROUPS.items()],
    }


class Keys(BaseModel):
    p256dh: str = Field(..., min_length=10, max_length=200)
    auth: str = Field(..., min_length=10, max_length=100)


class Subscription(BaseModel):
    endpoint: str = Field(..., min_length=10, max_length=1000)
    keys: Keys


class DeviceIn(BaseModel):
    subscription: Subscription
    label: str = Field(default="This device", max_length=60)


@router.post("/devices")
@limiter.limit("20/minute")
async def add_device(request: Request, body: DeviceIn, user: dict = Depends(get_current_user)) -> dict:
    if not push.enabled():
        raise HTTPException(status_code=503, detail="Notifications aren't set up yet. Try again later.")
    try:
        return await push.save_device(user["id"], body.subscription.model_dump(), body.label)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class ForgetIn(BaseModel):
    endpoint: str = Field(..., min_length=10, max_length=1000)


@router.post("/devices/forget")
@limiter.limit("30/minute")
async def forget_this_device(request: Request, body: ForgetIn, user: dict = Depends(get_current_user)) -> dict:
    """This device stops getting notifications (turned off here, or signing out)."""
    return {"removed": await push.forget_device(user["id"], endpoint=body.endpoint)}


@router.get("/devices")
@limiter.limit("60/minute")
async def devices(request: Request, user: dict = Depends(get_current_user)) -> dict:
    return {"devices": await push.list_devices(user["id"])}


@router.delete("/devices/{device_id}")
@limiter.limit("30/minute")
async def remove_device(request: Request, device_id: str, user: dict = Depends(get_current_user)) -> dict:
    removed = await push.forget_device(user["id"], did=device_id[:64])
    if not removed:
        raise HTTPException(status_code=404, detail="That device isn't on your list.")
    return {"removed": removed}


@router.get("/prefs")
@limiter.limit("60/minute")
async def get_prefs(request: Request, user: dict = Depends(get_current_user)) -> dict:
    return {"prefs": await push.get_prefs(user["id"])}


class PrefsIn(BaseModel):
    prefs: dict[str, bool]


@router.put("/prefs")
@limiter.limit("30/minute")
async def put_prefs(request: Request, body: PrefsIn, user: dict = Depends(get_current_user)) -> dict:
    return {"prefs": await push.set_prefs(user["id"], body.prefs)}


@router.post("/test")
@limiter.limit("5/minute")
async def test(request: Request, user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """A notification to each of the member's devices, so they can see how Ekavo reaches them."""
    if not push.enabled():
        raise HTTPException(status_code=503, detail="Notifications aren't set up yet. Try again later.")
    first: Optional[str] = (user.get("full_name") or user.get("name") or "").split(" ")[0] or None
    sent = await push.send_to_user(user["id"], {
        "title": f"You're all set{', ' + first if first else ''}",
        "body": "This is how Ekavo will reach you on this device: approvals, finished work and anything that needs a look.",
        "icon": "/icons/notify/update.png", "badge": "/icons/notify/badge.png", "tag": "ekavo-test",
        "url": "/dashboard/settings?tab=notifications", "group": "updates",
    }, urgency="high")
    if not sent:
        raise HTTPException(status_code=404, detail="No device has notifications on yet. Turn them on for this device first.")
    return {"sent": sent}
