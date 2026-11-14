"""The public API's OpenAPI document, generated from the tool registry. One operation for each tool, so the page can never describe a tool
that does not exist or miss one that does. Each tool's own typed input is its request body."""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

from app.tools.contract import ToolSpec

API_VERSION = "1"
SERVER_PATH = "/api/public/v1"
_REF = re.compile(r"#/\$defs/")


def _lift(schema: dict[str, Any], prefix: str, components: dict[str, Any]) -> dict[str, Any]:
    """Move a model schema's own definitions into the shared components, named by tool, and point its references there."""
    schema = json.loads(json.dumps(schema))
    for name, definition in (schema.pop("$defs", None) or {}).items():
        components[f"{prefix}_{name}"] = definition
    text = _REF.sub(f"#/components/schemas/{prefix}_", json.dumps(schema))
    out = json.loads(text)
    for key in list(components):
        if key.startswith(f"{prefix}_"):
            components[key] = json.loads(_REF.sub(f"#/components/schemas/{prefix}_", json.dumps(components[key])))
    return out


def _undo(tool: ToolSpec) -> str:
    from app.tools import undo

    return undo.words(tool.name) or tool.undo


def _operation(tool: ToolSpec, components: dict[str, Any]) -> dict[str, Any]:
    prefix = re.sub(r"[^A-Za-z0-9]", "_", tool.name)
    body = _lift(tool.input_schema(), f"{prefix}_in", components)
    components[f"{prefix}_call"] = {
        "type": "object", "required": ["arguments"],
        "properties": {
            "arguments": body,
            "dry_run": {"type": "boolean", "default": False, "description": "Check the call and show what it would cost, without doing it."},
            "confirmed_credits": {"type": "integer", "description": "The amount a call over 20 credits asked you to confirm."},
            "approval_token": {"type": "string", "description": "A person's one-time yes, for a tool that publishes."},
        },
    }
    return {
        "operationId": tool.name.replace(".", "_"),
        "summary": tool.summary,
        "description": (
            f"Risk: {tool.risk.value}. Needs the `{tool.scope.value}` permission on the key"
            + (f" and the role right `{tool.permission}` for the person it acts for" if tool.permission else "")
            + f". Cost: {tool.cost_key or tool.cost_note or 'free'}."
            + (f" Undo: {_undo(tool)}" if _undo(tool) else "")
        ),
        "tags": [tool.name.split(".")[0]],
        "x-ekavo-scope": tool.scope.value, "x-ekavo-risk": tool.risk.value, "x-ekavo-mode": tool.mode.value,
        "x-ekavo-examples": list(tool.examples),
        "parameters": [{"name": "Idempotency-Key", "in": "header", "required": False, "schema": {"type": "string", "maxLength": 120},
                        "description": "Send the same key to repeat a request safely: it is answered from the first and charged once."}],
        "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{prefix}_call"}}}},
        "responses": {
            "200": {"description": "Done. `data` holds the answer."},
            "202": {"description": "Started in the background. `run` says where to follow it (see runs.get)."},
            "4XX": {"description": "Refused. The body is `{error: {code, message}, request_id}`; the code never changes."},
        },
    }


def _kind(schema: dict[str, Any]) -> str:
    """A short plain name for what a field holds: text, number, a choice, a list, and so on."""
    if "enum" in schema:
        return "one of: " + ", ".join(str(v) for v in schema["enum"][:8])
    options = schema.get("anyOf") or schema.get("oneOf")
    if options:
        names = [_kind(o) for o in options if o.get("type") != "null"]
        return " or ".join(dict.fromkeys(names)) or "any"
    kind = schema.get("type", "any")
    return {"string": "text", "integer": "whole number", "number": "number", "boolean": "yes or no", "array": "list", "object": "object"}.get(kind, str(kind))


def reference(tools: Iterable[ToolSpec]) -> dict[str, Any]:
    """The tools in a form a page can show: no schemas to follow, just each tool's plain facts and the fields it takes. A tool only a person can use is left out."""
    out = []
    for tool in sorted(tools, key=lambda t: t.name):
        if tool.human_only:
            continue
        schema = tool.input_schema() or {}
        required = set(schema.get("required") or ())
        fields = [{"name": name, "kind": _kind(spec), "required": name in required, "about": str(spec.get("description") or "")[:200]}
                  for name, spec in (schema.get("properties") or {}).items()]
        out.append({"name": tool.name, "group": tool.name.split(".")[0], "summary": tool.summary, "risk": tool.risk.value, "scope": tool.scope.value, "mode": tool.mode.value,
                    "cost": tool.cost_key or tool.cost_note or "free", "approval": tool.approval, "undo": _undo(tool) or "", "examples": list(tool.examples)[:2],
                    "fields": fields, "beta": bool(tool.beta)})
    return {"version": API_VERSION, "server": SERVER_PATH, "count": len(out), "tools": out}


def build(tools: Iterable[ToolSpec], *, base_url: str = "") -> dict[str, Any]:
    components: dict[str, Any] = {}
    paths: dict[str, Any] = {}
    for tool in sorted(tools, key=lambda t: t.name):
        paths[f"/tools/{tool.name}/call"] = {"post": _operation(tool, components)}
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Ekavo API", "version": API_VERSION,
            "description": "Every action in Ekavo as a typed tool. Send `Authorization: Bearer <key>`. Every call passes the same checks, "
                           "credits, plan limits and approvals as the app. A key acts as the person who made it.",
        },
        "servers": [{"url": f"{base_url}{SERVER_PATH}"}],
        "security": [{"bearer": []}],
        "paths": paths,
        "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}, "schemas": components},
    }
