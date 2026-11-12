"""Browser sign-in for the command line and assistants: the two public calls a tool makes (`start`, `token`), no key needed.

The person's side (checking the code, saying yes or no, listing and revoking sign-ins) is in `tool_sessions.py`. See `app/tools/sessions.py`."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import settings
from app.core.middleware import limiter
from app.tools import sessions

router = APIRouter()


def _ip(request: Request) -> str:
    return f"ip:{request.client.host if request.client else 'unknown'}"


class StartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client: str = Field("cli", max_length=8, description="cli or mcp")
    label: str = Field("", max_length=60, description="What the person will see, for example the name of the computer or the assistant.")


class TokenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_code: str = Field(min_length=10, max_length=200)


def _failure(exc: sessions.SessionError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message}})


@router.post("/device/start", status_code=201)
@limiter.limit("10/minute", key_func=_ip)
async def start(request: Request, body: StartBody) -> Any:
    """Ask for a sign-in code. Show the `user_code` and `verification_url` to the person and poll `/device/token` with the `device_code`."""
    try:
        return await sessions.start(body.client, body.label, address=_ip(request), app_url=settings.FRONTEND_URL)
    except sessions.SessionError as exc:
        return _failure(exc)


@router.post("/device/token")
@limiter.limit("60/minute", key_func=_ip)
async def token(request: Request, body: TokenBody) -> Any:
    """Ask whether the person has answered: `pending` (keep asking every `interval` seconds), `slow_down`, or `approved` with the access token, once."""
    try:
        return await sessions.poll(body.device_code)
    except sessions.SessionError as exc:
        return _failure(exc)


@router.post("/device/revoke")
@limiter.limit("20/minute", key_func=_ip)
async def revoke_self(request: Request) -> Any:
    """A command line or assistant signs itself out: the session it is calling with is revoked at once."""
    from app.tools import credentials

    credential_id = await credentials.credential_for_secret(request.headers.get("authorization", "")[7:].strip())
    if not credential_id or not await sessions.revoke_own(credential_id):
        return JSONResponse(status_code=401, content={"error": {"code": "auth_invalid", "message": "A valid sign-in is needed. Send it as Authorization: Bearer <token>."}}, headers={"WWW-Authenticate": "Bearer"})
    return {"ok": True}
