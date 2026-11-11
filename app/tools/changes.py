"""The change history and the undo: `changes.list` shows what was changed through tools and how each can be taken back, `changes.undo` takes
one back. An undo is a change too, so it is recorded and can be undone. How each tool is undone is declared in undo.py."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.tools import undo
from app.tools.contract import Mode, Risk, Scope, ToolSpec
from app.tools.executor import ToolFailure, handler, request_route
from app.tools.gatekeeper import CAP_DELETE, Admission, Call, Gatekeeper, Refusal
from app.tools.registry import get_registry
from app.tools.undo import Kind


class ListChangesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(20, ge=1, le=50)
    cursor: Optional[str] = Field(None, max_length=200)
    tool: Optional[str] = Field(None, max_length=80)
    target_id: Optional[str] = Field(None, max_length=80, description="Only changes to this post, picture, video, recording or setting.")
    undoable_only: bool = False


class UndoIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    change_id: str = Field(min_length=1, max_length=64)


class JsonOut(BaseModel):
    model_config = ConfigDict(extra="allow")


def specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="changes.list", summary="List what was changed through tools, newest first, and how each change can be taken back.", risk=Risk.READ,
            scope=Scope.READ, mode=Mode.INSTANT, input_model=ListChangesIn, output_model=JsonOut, source="query:changes.list",
            examples=("What did my assistant change today?", "Show the changes to this post"),
        ),
        ToolSpec(
            name="changes.undo", summary="Take back one change made through a tool: restore the version from before it, put back the saved copy, or do the reverse.",
            risk=Risk.WRITE, scope=Scope.CREATE, mode=Mode.INSTANT, input_model=UndoIn, output_model=JsonOut, permission="create_content", cost_note="Free.",
            undo="An undo is recorded as a change of its own; undo that one to go forward again.", source="query:changes.undo",
            examples=("Undo that last edit", "Put the post back how it was"),
        ),
    ]


def _view(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": row["id"], "at": undo._now().isoformat() if not row.get("at") else row["at"].isoformat().replace("+00:00", "Z"), "tool": row["tool"],
            "target": row.get("target"), "kind": row["kind"], "undoable": bool(row.get("undoable")) and not row.get("undone_by"), "undone": bool(row.get("undone_by")),
            "how": row.get("how") or "", "surface": row.get("surface"), "fields": row.get("fields") or []}


@handler("changes.list")
async def list_changes(admission: Admission) -> dict[str, Any]:
    import base64
    import json
    from datetime import datetime

    a = admission.arguments.model_dump(mode="json", exclude_none=True)
    q: dict[str, Any] = {"workspace_id": admission.credential.workspace_id}
    if a.get("tool"):
        q["tool"] = a["tool"]
    if a.get("target_id"):
        q["target.id"] = a["target_id"]
    if a.get("undoable_only"):
        q["undoable"] = True
        q["undone_by"] = None
    if a.get("cursor"):
        try:
            when, last = json.loads(base64.urlsafe_b64decode(a["cursor"].encode("ascii")))
            moment = datetime.fromisoformat(when.replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            raise ToolFailure("input_invalid", "That cursor is not valid. Start again without one.", 422)
        q["$or"] = [{"at": {"$lt": moment}}, {"at": moment, "id": {"$lt": last}}]
    limit = a.get("limit", 20)
    rows = await undo.changes().find(q, {"_id": 0, "before": 0}).sort([("at", -1), ("id", -1)]).limit(limit + 1).to_list(length=limit + 1)
    more = len(rows) > limit
    rows = rows[:limit]
    nxt = base64.urlsafe_b64encode(json.dumps([rows[-1]["at"].isoformat(), rows[-1]["id"]]).encode("utf-8")).decode("ascii") if more and rows else None
    return {"items": [_view(r) for r in rows], "next_cursor": nxt}


async def _gate_call(admission: Admission, tool: str, arguments: dict[str, Any]):
    """Run the reverse tool through the same gatekeeper as any other call, so its scope, switches and delete capability all apply."""
    from app.tools import executor
    from app.tools.services import LiveServices

    gate = Gatekeeper(get_registry(), LiveServices())
    return await executor.run_call(gate, Call(credential_id=admission.credential.id, tool=tool, arguments=arguments))


@handler("changes.undo")
async def undo_change(admission: Admission) -> dict[str, Any]:
    from app.tools.services import LiveServices

    credential = admission.credential
    row = await undo.changes().find_one({"id": admission.arguments.change_id, "workspace_id": credential.workspace_id}, {"_id": 0})
    if row is None:
        raise ToolFailure("upstream_404", "That change was not found. Changes are kept for 90 days.", 404)
    if row.get("undone_by"):
        raise ToolFailure("already_undone", "That change was already taken back. Undo the undo (see its id in changes.list) to go forward again.", 409)
    if not row.get("undoable"):
        raise ToolFailure("cannot_undo", row.get("how") or "That change cannot be taken back by Ekavo.", 409)

    rule_tool = row.get("undo_rule_tool") or row["tool"]
    original = get_registry().get(rule_tool)
    permissions = await LiveServices().member_permissions(credential.workspace_id, credential.user_id) or frozenset()
    if original is not None and original.permission and original.permission not in permissions:
        raise ToolFailure("role_missing", "The person this acts for no longer has the right to make that kind of change.", 403)

    kind = Kind(row["kind"])
    if kind is Kind.VERSION:
        done = await _undo_version(admission, row)
    elif kind is Kind.SNAPSHOT:
        done = await _undo_snapshot(admission, row)
    else:
        inverse = row.get("inverse") or {}
        tool = get_registry().get(inverse.get("tool", ""))
        if tool is None:
            raise ToolFailure("cannot_undo", "That change has no way back recorded.", 409)
        if tool.risk is Risk.DESTRUCTIVE and credential.surface == "api" and CAP_DELETE not in credential.capabilities:
            raise ToolFailure("capability_missing", "Undoing this removes what was made, and keys cannot delete unless the owner switches that on for this key.", 403)
        try:
            _, outcome = await _gate_call(admission, tool.name, inverse.get("arguments") or {})
        except Refusal as refusal:
            raise ToolFailure(refusal.code, refusal.message, refusal.status)
        done = {"new_change": (outcome.change if outcome else None), "kind": kind.value}
    new_id = (done.get("new_change") or {}).get("id")
    await undo.changes().update_one({"id": row["id"]}, {"$set": {"undone_by": new_id or "done", "undone_at": undo._now()}})
    return {"undone": row["id"], "tool": row["tool"], "kind": kind.value, **done}


async def _undo_version(admission: Admission, row: dict[str, Any]) -> dict[str, Any]:
    credential = admission.credential
    rule = undo.UNDO[row.get("undo_rule_tool") or row["tool"]]
    target = row["target"]["id"]
    _, _, restore = undo._FAMILIES[rule.entity]
    now_version = await undo.current_version(rule, credential, target, request_route)
    wanted = int(row["before"]["version"])
    await request_route(credential, "POST", restore.format(id=target, n=wanted), None, None)
    after = await undo.current_version(rule, credential, target, request_route)
    change = {"id": __import__("uuid").uuid4().hex}
    await undo.changes().insert_one({
        **{k: v for k, v in row.items() if k not in ("id", "at", "undone_by", "undone_at", "before", "call_id")},
        "id": change["id"], "at": undo._now(), "tool": "changes.undo", "undone_by": None, "before": {"version": now_version}, "restored_from": row["id"],
        "user_id": credential.user_id, "credential_id": credential.id, "surface": credential.surface, "how": undo.words(row.get("undo_rule_tool") or row["tool"]), "undoable": True,
        "undo_rule_tool": row.get("undo_rule_tool") or row["tool"],
    })
    return {"new_change": {"id": change["id"], "undo": "version", "undoable": True}, "restored_version": wanted, "now_on_version": after, "target": row["target"]}


async def _undo_snapshot(admission: Admission, row: dict[str, Any]) -> dict[str, Any]:
    credential = admission.credential
    rule = undo.UNDO[row.get("undo_rule_tool") or row["tool"]]
    before = row["before"]
    flt = before["filter"]
    current = await undo.read_document(rule, flt)
    if undo.digest(current) != row.get("after_hash"):
        raise ToolFailure("changed_since", "That was changed again after this change. Undo the newer change first, then this one.", 409)
    await undo.put_document(rule, flt, before.get("doc"))
    restored = await undo.read_document(rule, flt)
    change_id = __import__("uuid").uuid4().hex
    await undo.changes().insert_one({
        **{k: v for k, v in row.items() if k not in ("id", "at", "undone_by", "undone_at", "before", "call_id", "after_hash")},
        "id": change_id, "at": undo._now(), "tool": "changes.undo", "undone_by": None, "before": {"doc": current, "filter": flt}, "after_hash": undo.digest(restored),
        "restored_from": row["id"], "user_id": credential.user_id, "credential_id": credential.id, "surface": credential.surface, "undoable": True,
        "undo_rule_tool": row.get("undo_rule_tool") or row["tool"],
    })
    return {"new_change": {"id": change_id, "undo": "snapshot", "undoable": True}, "target": row["target"]}
