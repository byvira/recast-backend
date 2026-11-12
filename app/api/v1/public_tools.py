"""The public API: every tool, called with a workspace key (`Authorization: Bearer ek_live_...`), never a login.

A call passes the gatekeeper (see app.tools.gatekeeper) and is done by code that already exists (see app.tools.executor), so it gets the
same checks, credits, plan limits and approvals as the app. Every answer is JSON. A refusal is `{"error": {"code", "message"}, "request_id"}`
and its code never changes. A repeated request with the same `Idempotency-Key` is answered from the first and charged once."""

from __future__ import annotations

import re
from typing import Any, Optional
from uuid import uuid4

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.core.middleware import limiter
from app.tools import credentials, executor, openapi
from app.tools.contract import Scope, ToolSpec
from app.tools.gatekeeper import Call, Gatekeeper, Refusal, visible_tools
from app.tools.registry import get_registry
from app.tools.services import LiveServices

router = APIRouter()

UNAUTHORIZED = "A valid API key is needed. Send it as Authorization: Bearer <key>."
_KEY = re.compile(r"^[A-Za-z0-9._:\-]{1,120}$")


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header.lower().startswith("bearer ") else ""


def _limit_key(request: Request) -> str:
    """One rate limit per key; a request with no key shares one bucket per address, so guessing is slow."""
    token = _bearer(request)
    return f"key:{token[:24]}" if token else f"ip:{request.client.host if request.client else 'unknown'}"


def _rid(request: Request) -> str:
    return request.headers.get("x-request-id") or uuid4().hex


def _error(request: Request, status: int, code: str, message: str, extra: Optional[dict[str, Any]] = None) -> JSONResponse:
    from app.core import failures

    extra = {**failures.for_refusal(code), **(extra or {})}
    headers: dict[str, str] = {"WWW-Authenticate": "Bearer"} if status == 401 else {}
    if extra.get("retry_after_seconds") is not None:
        headers["Retry-After"] = str(extra["retry_after_seconds"])
    return _error_response(request, status, code, message, extra, headers or None)


def _error_response(request: Request, status: int, code: str, message: str, extra: dict[str, Any], headers: Optional[dict[str, str]]) -> JSONResponse:
    if code == "auth_invalid":
        import asyncio

        from app.tools import ops

        # a call with a key that does not exist is counted, by a short hash of the caller's address, for the abuse watch
        asyncio.get_running_loop().create_task(ops.record_auth_failure(_limit_key(request)))
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message, **extra}, "request_id": _rid(request)}, headers=headers)


async def _credential_id(request: Request) -> Optional[str]:
    return await credentials.credential_for_secret(_bearer(request))


def _entry(tool: ToolSpec) -> dict[str, Any]:
    return {"name": tool.name, "summary": tool.summary, "risk": tool.risk.value, "scope": tool.scope.value, "mode": tool.mode.value,
            "cost": tool.cost_key or tool.cost_note or "free", "approval": tool.approval, "available": executor.is_wired(tool)}


class CallBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    arguments: dict[str, Any] = Field(default_factory=dict)
    dry_run: bool = False
    confirmed_credits: Optional[int] = Field(None, ge=0)
    approval_token: Optional[str] = Field(None, max_length=200)


async def _visible(request: Request) -> tuple[Optional[Any], list[ToolSpec]]:
    credential_id = await _credential_id(request)
    if not credential_id:
        return None, []
    services = LiveServices()
    credential = await services.credential(credential_id)
    if credential is None or credential.revoked:
        return None, []
    permissions = await services.member_permissions(credential.workspace_id, credential.user_id)
    if permissions is None:
        return None, []
    return credential, visible_tools(get_registry(), credential, permissions)


@router.get("/openapi.json")
@limiter.limit("30/minute", key_func=_limit_key)
async def openapi_document(request: Request) -> JSONResponse:
    """The whole API as an OpenAPI document, built from the tool list. It describes every tool; what a key may use is in GET /tools."""
    return JSONResponse(openapi.build(get_registry().all()))


@router.get("/approvals/{request_id}")
@limiter.limit("240/minute", key_func=_limit_key)
async def get_approval(request: Request, request_id: str) -> JSONResponse:
    """Where a call that needed a person's yes stands: waiting, declined, or approved with the one-time token to repeat the call with."""
    from app.tools import approvals

    credential_id = await _credential_id(request)
    credential = await LiveServices().credential(credential_id) if credential_id else None
    if credential is None or credential.revoked:
        return _error(request, 401, "auth_invalid", UNAUTHORIZED)
    found = await approvals.poll(credential, request_id)
    if found is None:
        return _error(request, 404, "approval_unknown", "That approval request was not found for this key.")
    return JSONResponse({"request": found, "request_id": _rid(request)})


@router.get("/tools")
@limiter.limit("120/minute", key_func=_limit_key)
async def list_tools(
    request: Request, group: Optional[str] = Query(None, max_length=40), q: Optional[str] = Query(None, max_length=80),
) -> JSONResponse:
    """The tools this key may use, as the person it acts for is allowed today."""
    credential, tools = await _visible(request)
    if credential is None:
        return _error(request, 401, "auth_invalid", UNAUTHORIZED)
    if group:
        tools = [t for t in tools if t.name.split(".")[0] == group]
    if q:
        needle = q.lower()
        tools = [t for t in tools if needle in t.name or needle in t.summary.lower()]
    return JSONResponse({"tools": [_entry(t) for t in tools], "count": len(tools), "request_id": _rid(request)})


@router.get("/tools/{name}")
@limiter.limit("240/minute", key_func=_limit_key)
async def get_tool(request: Request, name: str) -> JSONResponse:
    """One tool with its typed input and output. A tool this key may not use is answered like one that does not exist."""
    credential, tools = await _visible(request)
    if credential is None:
        return _error(request, 401, "auth_invalid", UNAUTHORIZED)
    tool = next((t for t in tools if t.name == name), None)
    if tool is None:
        return _error(request, 404, "tool_unknown", f"There is no tool called {name!r} for this key.")
    return JSONResponse({**tool.catalog_entry(), "available": executor.is_wired(tool), "output": tool.output_schema(), "request_id": _rid(request)})


async def execute(
    credential_id: str, name: str, arguments: dict[str, Any], *, idempotency_key: Optional[str] = None, confirmed_credits: Optional[int] = None,
    approval_token: Optional[str] = None, dry_run: bool = False,
) -> tuple[int, dict[str, Any]]:
    """Run one tool through the gatekeeper and the code that does the work. Returns (HTTP status, body): the body is the answer, or `{"error": ...}` with
    a code that never changes. Used by the REST API and the MCP endpoint, so both give the same answer, the same refusals and the same records."""
    from app.core import failures

    call = Call(credential_id=credential_id, tool=name, arguments=arguments, idempotency_key=idempotency_key, confirmed_credits=confirmed_credits,
                approval_token=approval_token, dry_run=dry_run)
    gate = Gatekeeper(get_registry(), LiveServices())
    try:
        admission, outcome = await executor.run_call(gate, call)
    except Refusal as refusal:
        return refusal.status, {"error": {"code": refusal.code, "message": refusal.message, **{**failures.for_refusal(refusal.code), **(refusal.extra or {})}}}
    except executor.ToolFailure as failure:
        return failure.status, {"error": {"code": failure.code, "message": failure.message, **{k: v for k, v in failure.body().items() if k not in ("code", "message")}}}
    except Exception as exc:  # noqa: BLE001 - anything unexpected is a plain "something went wrong", never a stack trace
        import logging

        from app.health import errors

        logging.getLogger(__name__).exception("Public tools API error | tool=%s", name)
        errors.capture_exception(exc, source="server", where=f"tool call {name}", workspace_id=None)
        f = failures.internal_error()
        return 500, {"error": {"code": f.code, "message": f.message, "retryable": f.retryable, "retry_after_seconds": f.retry_after_seconds, "next": f.next}}

    estimate = {"credits": admission.estimate.credits, "exact": admission.estimate.exact}
    if admission.estimate.note and admission.estimate.credits:
        estimate["note"] = admission.estimate.note  # said only when there is a price to explain
    if admission.replay is not None:
        stored = admission.replay
        return stored.get("status", 200), {"ok": True, "tool": name, "data": stored.get("data"), "change": stored.get("change"), "replayed": True, "estimate": estimate}
    if admission.dry_run:
        return 200, {"ok": True, "tool": name, "dry_run": True, "estimate": estimate, "arguments": admission.arguments.model_dump(mode="json", exclude={"confirm"})}
    return outcome.status, {"ok": True, "tool": name, "data": outcome.data, "run": outcome.run, "change": outcome.change, "estimate": estimate, "warnings": list(outcome.warnings)}


@router.post("/tools/{name}/call")
@limiter.limit("300/minute", key_func=_limit_key)
async def call_tool(
    request: Request, name: str, body: CallBody,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> JSONResponse:
    """Run one tool. `dry_run` shows the estimate and does nothing. A call over 20 credits asks first: send back `confirmed_credits`."""
    credential_id = await _credential_id(request)
    if not credential_id:
        return _error(request, 401, "auth_invalid", UNAUTHORIZED)
    if idempotency_key is not None and not _KEY.match(idempotency_key):
        return _error(request, 422, "input_invalid", "The Idempotency-Key is up to 120 letters, numbers and . _ : -")
    status, content = await execute(
        credential_id, name, body.arguments, idempotency_key=idempotency_key, confirmed_credits=body.confirmed_credits,
        approval_token=body.approval_token, dry_run=body.dry_run,
    )
    content["request_id"] = _rid(request)
    if (content.get("error") or {}).get("code") == "auth_invalid":
        return _error_response(request, status, "auth_invalid", content["error"].get("message", UNAUTHORIZED), {k: v for k, v in content["error"].items() if k not in ("code", "message")}, {"WWW-Authenticate": "Bearer"})
    headers: dict[str, str] = {}
    wait = (content.get("error") or {}).get("retry_after_seconds")
    if wait is not None:
        headers["Retry-After"] = str(wait)
    return JSONResponse(status_code=status, content=content, headers=headers or None)
