"""Builds tool definitions from compact rows: the meaning is written by a person, the typed input and permission come from the route.

A row says what the tool is (a plain summary), how risky it is, which routes it stands for, what it costs and how it can be undone.
The typed input is built from the first route's own path ids, query options and body, and the permission is read from that route's
role check, so a tool cannot disagree with the route it wraps.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from pydantic import BaseModel

from app.tools.contract import Mode, Risk, Scope, ToolSpec
from app.tools.examples import EXAMPLES
from app.tools.from_routes import derive_input, route_index, route_permission

_SCOPE = {
    Risk.READ: Scope.READ, Risk.WRITE: Scope.CREATE, Risk.PUBLISH: Scope.PUBLISH, Risk.DESTRUCTIVE: Scope.CREATE,
    Risk.DECISION: Scope.APPROVE, Risk.ADMIN: Scope.ADMIN,
}


class JsonResult(BaseModel):
    """The answer for a route that returns plain JSON and has no declared response model."""

    data: Any = None


@dataclass(frozen=True)
class T:
    name: str
    summary: str
    risk: Risk
    #: The routes this tool stands for. The first one gives the typed input.
    routes: tuple[str, ...]
    cost_key: Optional[str] = None
    note: str = ""
    undo: str = ""
    examples: tuple[str, ...] = ()
    limits: tuple[str, ...] = ()
    #: Input fields only a person may set.
    human_fields: tuple[str, ...] = ()
    mode: Mode = Mode.INSTANT
    coarse: bool = False


def _class_name(tool_name: str, suffix: str) -> str:
    return "".join(part.capitalize() for part in re.split(r"[._]", tool_name)) + suffix


# A tool that changes things needs at least this role permission, even where the screen route itself lets any member in, so a key made by a
# viewer can read but never write.
_FLOOR: dict[Risk, str] = {
    Risk.WRITE: "create_content", Risk.DESTRUCTIVE: "create_content", Risk.PUBLISH: "publish_content",
    Risk.DECISION: "approve_content", Risk.ADMIN: "manage_workspace_settings",
}


def build(rows: Iterable[T]) -> list[ToolSpec]:
    index = route_index()
    out: list[ToolSpec] = []
    for row in rows:
        primary = row.routes[0]
        if primary not in index:
            raise LookupError(f"{row.name}: the app does not serve {primary}")
        for route in row.routes:
            if route not in index:
                raise LookupError(f"{row.name}: the app does not serve {route}")
        model = derive_input(primary, _class_name(row.name, "In"), row.risk is Risk.DESTRUCTIVE)
        response = index[primary].response_model
        output = response if isinstance(response, type) and issubclass(response, BaseModel) else JsonResult
        out.append(ToolSpec(
            name=row.name, summary=row.summary, risk=row.risk, scope=_SCOPE[row.risk], mode=row.mode, input_model=model,
            output_model=output, permission=route_permission(primary) or _FLOOR.get(row.risk), cost_key=row.cost_key,
            cost_note=row.note or ("" if row.cost_key or row.risk is Risk.READ else "Free."), limit_keys=row.limits,
            approval="person" if row.risk is Risk.PUBLISH else "none", undo=row.undo, examples=row.examples or EXAMPLES.get(row.name, ()),
            source=f"route:{primary}", routes=row.routes, human_only_fields=row.human_fields,
            human_only=row.risk is Risk.DECISION, coarse=row.coarse,
        ))
    return out
