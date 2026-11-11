"""The typed definition of one tool.

A tool is something a person can ask for by outcome ("write a post", "schedule this", "show my credits"). It is defined once here, and
every way in (the app's assistant, an API, a command line, an assistant such as Claude) reads this one definition: its name, what it
does, the typed input it takes, what it needs (a key scope and a role permission), what it costs, and whether a person must confirm.

Nothing here runs anything. Running goes through the gatekeeper, which checks the person, their plan and their credits first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel

from app.billing.costs import DEFAULT_COSTS
from app.core.rbac import ROLE_PERMISSIONS


class Risk(str, Enum):
    READ = "read"                  # changes nothing
    WRITE = "write"                # makes or changes content, or spends credits
    PUBLISH = "publish"            # sends something to the world, or plans to
    DESTRUCTIVE = "destructive"    # removes content
    DECISION = "decision"          # approving, sending back or signing off: a person's call
    ADMIN = "admin"                # keys, webhooks and sign-in sessions


class Scope(str, Enum):
    """What a key or a signed-in session may do. A scope never gives more than the person's own role allows."""

    READ = "read"
    CREATE = "create"
    PUBLISH = "publish"
    APPROVE = "approve"
    ADMIN = "admin"


class Mode(str, Enum):
    INSTANT = "instant"            # answers straight away
    RUN = "run"                    # starts a saved run (the job layer): shows in Control Tower, can be stopped


def _undo_words(name: str) -> str:
    from app.tools import undo

    return undo.words(name)


_NAME = re.compile(r"^[a-z]+(\.[a-z_]+)+$")
_ALL_PERMISSIONS = frozenset().union(*ROLE_PERMISSIONS.values())
_RISK_SCOPE = {
    Risk.READ: {Scope.READ},
    Risk.WRITE: {Scope.CREATE},
    Risk.PUBLISH: {Scope.PUBLISH},
    Risk.DESTRUCTIVE: {Scope.CREATE},
    Risk.DECISION: {Scope.APPROVE},
    Risk.ADMIN: {Scope.ADMIN},
}


class ToolError(ValueError):
    """A tool definition that breaks the contract."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    summary: str
    risk: Risk
    scope: Scope
    mode: Mode
    input_model: type[BaseModel]
    output_model: Optional[type[BaseModel]] = None
    #: The role permission the person must hold (see app.core.rbac). None means any member.
    permission: Optional[str] = None
    #: A row of the credit price table. None means it is free or its price depends on what is made (then `cost_note` says how).
    cost_key: Optional[str] = None
    cost_note: str = ""
    #: Plan limits this tool is held to (see app.billing.plans.LIMITS), for example "pictures_day".
    limit_keys: tuple[str, ...] = ()
    #: "person": a person must confirm before it takes effect (anything that publishes is held for approval).
    approval: str = "none"
    #: What undoing it looks like, in plain words. Empty when it cannot be undone.
    undo: str = ""
    #: Short example requests, in any language, that help the assistant choose this tool.
    examples: tuple[str, ...] = ()
    #: Where the work is done today: "action:<name>" (a registered job action) or "query:<name>".
    source: str = ""
    #: The app routes this tool stands for, as "METHOD /path" exactly as the app declares them. The coverage test uses these to show
    #: that every route is either offered as a tool or deliberately left out.
    routes: tuple[str, ...] = ()
    #: Input fields only a person may set (for example "publish anyway" on a post the safety check held). A key or an assistant that
    #: sets one is refused.
    human_only_fields: tuple[str, ...] = ()
    #: A decision (approve, send back) is a person's by default: no key or assistant holds it unless the owner switches it on.
    human_only: bool = False
    #: A coarse tool is a named outcome over a route that several tools share (for example one picture save route).
    coarse: bool = False
    version: int = 1
    enabled: bool = True
    beta: bool = False
    extra: dict = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        if not _NAME.match(self.name):
            raise ToolError(f"{self.name!r}: a tool name is lower-case words joined by dots, such as 'text.generate'")
        if not self.summary.strip() or len(self.summary) > 200:
            raise ToolError(f"{self.name}: the summary is one plain sentence of at most 200 characters")
        if self.scope not in _RISK_SCOPE[self.risk]:
            raise ToolError(f"{self.name}: a {self.risk.value} tool uses scope {sorted(s.value for s in _RISK_SCOPE[self.risk])}, not {self.scope.value}")
        if self.permission is not None and self.permission not in _ALL_PERMISSIONS:
            raise ToolError(f"{self.name}: unknown permission {self.permission!r}")
        if self.risk is Risk.READ and self.cost_key is not None:
            raise ToolError(f"{self.name}: a read costs nothing")
        if self.cost_key is not None and self.cost_key not in DEFAULT_COSTS:
            raise ToolError(f"{self.name}: {self.cost_key!r} is not a row of the price table")
        if self.cost_key is None and self.risk is not Risk.READ and not self.cost_note.strip():
            raise ToolError(f"{self.name}: a tool with no price row says why in cost_note")
        if self.risk is Risk.DESTRUCTIVE and "confirm" not in self.input_model.model_fields:
            raise ToolError(f"{self.name}: a destructive tool takes an explicit 'confirm' field")
        if self.risk is Risk.PUBLISH and self.approval != "person":
            raise ToolError(f"{self.name}: anything that publishes is held for a person's approval")
        if self.risk is Risk.DECISION and not self.human_only:
            raise ToolError(f"{self.name}: a decision is a person's by default")
        for name in self.human_only_fields:
            if name not in self.input_model.model_fields:
                raise ToolError(f"{self.name}: human_only_fields names {name!r}, which is not an input field")
        if self.approval not in ("none", "person"):
            raise ToolError(f"{self.name}: approval is 'none' or 'person'")

    def input_schema(self) -> dict[str, Any]:
        return self.input_model.model_json_schema()

    def output_schema(self) -> Optional[dict[str, Any]]:
        return self.output_model.model_json_schema() if self.output_model else None

    def catalog_entry(self) -> dict[str, Any]:
        """What an outside assistant or the planner sees about this tool. No code, no internals."""
        return {
            "name": self.name, "version": self.version, "summary": self.summary, "risk": self.risk.value, "scope": self.scope.value,
            "mode": self.mode.value, "cost": self.cost_key or self.cost_note or "free", "approval": self.approval, "undo": _undo_words(self.name) or self.undo,
            "examples": list(self.examples), "beta": self.beta, "input": self.input_schema(),
        }
