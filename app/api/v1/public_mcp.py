"""The MCP server: the same tools for assistants such as Claude and Cursor, over MCP's streamable HTTP (one JSON-RPC message in a POST, one answer).

It signs in the same way as the REST API: `Authorization: Bearer <key or session token>`. A person connects an assistant through the browser
sign-in (see `app/tools/sessions.py`), which gives the assistant a session with only what the person allowed. A call is done by `execute`, the
same function the REST API uses, so it passes the same gatekeeper, costs the same credits, needs the same approvals and is recorded in the same
ledger (tagged "mcp").

Supported: initialize, ping, tools/list, tools/call. Estimates come back as a tool result: send `_meta.dry_run: true`. Idempotency, a confirmed
price and an approval token go in `_meta` too (`idempotency_key`, `confirmed_credits`, `approval_token`)."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from app.api.v1.public_tools import UNAUTHORIZED, _KEY, _credential_id, _limit_key, _visible, execute
from app.core.middleware import limiter
from app.tools import executor
from app.tools.contract import Risk, ToolSpec

router = APIRouter()

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER = {"name": "ekavo", "title": "Ekavo", "version": "1.0.0"}
INSTRUCTIONS = (
    "Tools for managing one Ekavo workspace as the person who connected you, with their role. Reads are free. A call that makes something may cost credits: "
    "send _meta.dry_run true first to see the price. Publishing, approving and deleting need the person's own yes, so they come back as 'needs approval' "
    "with what to ask them. Every error has a stable code, whether trying again can help, and what to do next."
)

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602


def _result(id_: Any, result: dict[str, Any]) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": result})


def _rpc_error(id_: Any, code: int, message: str, status: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status, content={"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}})


def _unauthorized() -> JSONResponse:
    return JSONResponse(status_code=401, content={"error": {"code": "auth_invalid", "message": UNAUTHORIZED}}, headers={"WWW-Authenticate": "Bearer"})


def tool_view(tool: ToolSpec) -> dict[str, Any]:
    """One tool as MCP describes it. The hints let an assistant ask the person before anything that changes or removes something."""
    cost = tool.cost_key or tool.cost_note
    note = f" Costs credits: {cost}." if tool.cost_key else (f" {tool.cost_note}" if tool.cost_note else "")
    schema = dict(tool.input_schema() or {"type": "object", "properties": {}})
    schema.setdefault("type", "object")
    return {
        "name": tool.name, "title": tool.summary.rstrip(".")[:80], "description": f"{tool.summary}{note}"[:1000], "inputSchema": schema,
        "annotations": {"readOnlyHint": tool.risk is Risk.READ, "destructiveHint": tool.risk is Risk.DESTRUCTIVE, "idempotentHint": tool.risk is Risk.READ, "openWorldHint": False},
    }


def _text(status: int, body: dict[str, Any]) -> str:
    """A short sentence an assistant can read out, then the facts."""
    import json

    if "error" in body:
        e = body["error"]
        line = f"{e.get('message', 'That did not work.')} ({e.get('code', 'error')})"
        if e.get("retryable") and e.get("retry_after_seconds") is not None:
            line += f" You can try again in about {e['retry_after_seconds']} seconds."
        return f"{line} {e.get('next', '')}".strip()
    if body.get("dry_run"):
        est = body.get("estimate") or {}
        return f"Nothing was done. It would cost {est.get('credits', 0)} credits{' or more' if est.get('exact') is False else ''}. " + json.dumps(body.get("arguments") or {}, ensure_ascii=False)
    return json.dumps(body.get("data"), ensure_ascii=False, default=str)[:60000] if body.get("data") is not None else "Done."


async def _call(credential_id: str, params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    meta = params.get("_meta") or {}
    key = meta.get("idempotency_key")
    if not isinstance(name, str) or not isinstance(arguments, dict) or (key is not None and (not isinstance(key, str) or not _KEY.match(key))):
        raise ValueError("name must be a tool name, arguments an object and _meta.idempotency_key up to 120 letters, numbers and . _ : -")
    confirmed = meta.get("confirmed_credits")
    status, body = await execute(
        credential_id, name, arguments, idempotency_key=key, confirmed_credits=confirmed if isinstance(confirmed, int) and confirmed >= 0 else None,
        approval_token=meta.get("approval_token") if isinstance(meta.get("approval_token"), str) else None, dry_run=bool(meta.get("dry_run")),
    )
    failed = "error" in body
    return {"content": [{"type": "text", "text": _text(status, body)}], "structuredContent": body, "isError": failed}


@router.post("/mcp")
@limiter.limit("300/minute", key_func=_limit_key)
async def mcp(request: Request) -> Response:
    credential_id = await _credential_id(request)
    if not credential_id:
        return _unauthorized()
    try:
        message = await request.json()
    except Exception:  # noqa: BLE001
        return _rpc_error(None, PARSE_ERROR, "The message was not valid JSON.", 400)
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _rpc_error(None, INVALID_REQUEST, "Send one JSON-RPC 2.0 message at a time.", 400)
    method, id_, params = message.get("method"), message.get("id"), message.get("params") or {}
    if "id" not in message:
        return Response(status_code=202)                       # a notification: nothing to answer
    if not isinstance(method, str) or not isinstance(params, dict):
        return _rpc_error(id_, INVALID_REQUEST, "A request has a method name and, when it has parameters, an object.")

    if method == "initialize":
        asked = params.get("protocolVersion")
        return _result(id_, {"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0], "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER, "instructions": INSTRUCTIONS})
    if method == "ping":
        return _result(id_, {})
    if method == "tools/list":
        credential, tools = await _visible(request)
        if credential is None:
            return _unauthorized()
        return _result(id_, {"tools": [tool_view(t) for t in tools if executor.is_wired(t)]})
    if method == "tools/call":
        try:
            return _result(id_, await _call(credential_id, params))
        except ValueError as exc:
            return _rpc_error(id_, INVALID_PARAMS, str(exc))
    return _rpc_error(id_, METHOD_NOT_FOUND, f"There is no method called {method}.")


@router.get("/mcp")
async def mcp_stream(request: Request) -> Response:
    """This server answers each message in the reply to it; it offers no separate stream."""
    return Response(status_code=405, headers={"Allow": "POST"})
