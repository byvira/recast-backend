"""A registry of the tools an agent may use, and the check that a plan only names tools that exist.

A planner (a language model or fixed rules) proposes steps; code decides what runs. Each tool states what it costs, how long it
may take and whether its result may be cached, so budgets and timeouts are enforced in one place and a plan can never call
something that was not registered.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Optional

COST_CLASSES = ("free", "llm", "provider", "worker")


class PlanError(ValueError):
    """A plan names a tool that is not registered, or has no steps."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    cost: str = "free"                 # free, llm (a language model call), provider (a paid or quota-bound service), worker
    timeout_s: float = 60.0
    cacheable: bool = False
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.cost not in COST_CLASSES:
            raise ValueError(f"cost must be one of {', '.join(COST_CLASSES)}")


@dataclass
class ToolResult:
    name: str
    ok: bool
    value: Any = None
    error: Optional[str] = None
    seconds: float = 0.0


@dataclass
class ToolRegistry:
    agent: str
    _tools: dict[str, tuple[ToolSpec, Callable[..., Awaitable[Any]]]] = field(default_factory=dict)

    def register(self, spec: ToolSpec, fn: Callable[..., Awaitable[Any]]) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool {spec.name} is already registered")
        self._tools[spec.name] = (spec, fn)

    def names(self) -> list[str]:
        return list(self._tools)

    def spec(self, name: str) -> ToolSpec:
        return self._tools[name][0]

    def validate_plan(self, steps: Iterable[Any]) -> list[str]:
        """The plan as a list of registered tool names, in order. Raises PlanError for an empty plan or an unknown tool."""
        out: list[str] = []
        for step in steps or []:
            name = step.get("tool") if isinstance(step, dict) else step
            if not isinstance(name, str) or name not in self._tools:
                raise PlanError(f"The plan uses a tool that does not exist: {name!r}.")
            out.append(name)
        if not out:
            raise PlanError("The plan has no steps.")
        return out

    async def run(self, name: str, **kwargs: Any) -> ToolResult:
        """Run one tool within its timeout. Never raises: a failure is a result, so the graph can decide what to do."""
        spec, fn = self._tools[name]
        began = time.monotonic()
        try:
            value = await asyncio.wait_for(fn(**kwargs), timeout=spec.timeout_s)
            return ToolResult(name, True, value, None, time.monotonic() - began)
        except asyncio.TimeoutError:
            return ToolResult(name, False, None, f"{name} took longer than {spec.timeout_s:g} seconds.", time.monotonic() - began)
        except Exception as exc:  # noqa: BLE001 - reported to the graph as a result
            return ToolResult(name, False, None, f"{type(exc).__name__}: {exc}"[:300], time.monotonic() - began)
