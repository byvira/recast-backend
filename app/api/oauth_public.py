"""OAuth sign-in for assistants that connect by address (Claude and others): the discovery documents and the four calls an assistant makes.

Mounted at the address root, because the standards fix where the discovery documents live. The person's side (checking what is asking, choosing a
workspace and saying yes) is in `tool_sessions.py`. See `app/tools/oauth.py` for how it works."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.core.middleware import limiter
from app.tools import oauth

router = APIRouter()

_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _ip(request: Request) -> str:
    return f"ip:{request.client.host if request.client else 'unknown'}"


def _error(exc: oauth.OAuthError) -> JSONResponse:
    headers = dict(_NO_STORE)
    if exc.status == 401:
        headers["WWW-Authenticate"] = "Basic"
    return JSONResponse(status_code=exc.status, content={"error": exc.error, "error_description": exc.description}, headers=headers)


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/{path:path}")
async def protected_resource(request: Request, path: str = "") -> Any:
    """Where the MCP server's sign-in is (RFC 9728)."""
    return JSONResponse(oauth.protected_resource_metadata(), headers={"Cache-Control": "public, max-age=300"})


@router.get("/.well-known/oauth-authorization-server")
@router.get("/.well-known/oauth-authorization-server/{path:path}")
async def authorization_server(request: Request, path: str = "") -> Any:
    """The sign-in's own addresses and what it supports (RFC 8414)."""
    return JSONResponse(oauth.authorization_server_metadata(), headers={"Cache-Control": "public, max-age=300"})


@router.post("/oauth/register", status_code=201)
@limiter.limit("10/minute", key_func=_ip)
async def register(request: Request) -> Any:
    """An assistant introduces itself and gets a client id (RFC 7591)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - not JSON
        body = None
    if not isinstance(body, dict):
        return _error(oauth.OAuthError("invalid_client_metadata", "Send a JSON object."))
    try:
        return JSONResponse(status_code=201, content=await oauth.register(body, address=_ip(request)), headers=_NO_STORE)
    except oauth.OAuthError as exc:
        return _error(exc)


@router.get("/oauth/authorize")
@limiter.limit("30/minute", key_func=_ip)
async def authorize(request: Request) -> Any:
    """The assistant sends the person here. The request is checked, then the person is sent to the app to say yes or no."""
    try:
        return RedirectResponse(await oauth.begin(dict(request.query_params)), status_code=302, headers=_NO_STORE)
    except oauth._Redirect as exc:
        return RedirectResponse(exc.url(), status_code=302, headers=_NO_STORE)
    except oauth.OAuthError as exc:
        # the client or its redirect address is wrong: nothing is sent anywhere, the person is told here
        return _error(exc)


@router.post("/oauth/token")
@limiter.limit("60/minute", key_func=_ip)
async def token(request: Request) -> Any:
    """Trade a code (with its PKCE proof) or a refresh token for new tokens. Form encoded, as the standard says."""
    form = {k: str(v) for k, v in (await request.form()).items()}
    kind = form.get("grant_type", "")
    try:
        if kind == "authorization_code":
            return JSONResponse(await oauth.exchange(form), headers=_NO_STORE)
        if kind == "refresh_token":
            return JSONResponse(await oauth.refresh(form), headers=_NO_STORE)
        raise oauth.OAuthError("unsupported_grant_type", "Use authorization_code or refresh_token.")
    except oauth.OAuthError as exc:
        return _error(exc)


@router.post("/oauth/revoke")
@limiter.limit("30/minute", key_func=_ip)
async def revoke(request: Request) -> Any:
    """An assistant signs itself out (RFC 7009). An unknown token is still a success."""
    form = {k: str(v) for k, v in (await request.form()).items()}
    await oauth.revoke(form.get("token", ""))
    return JSONResponse({}, headers=_NO_STORE)
