"""Runs a validated plan: the steps in order, each one a registered tool, with skip rules, budgets, a pause and cancel point
before every step, and a decision entry after it.

A planner (rules, or a model checked by `ToolRegistry.validate_plan`) chooses the steps; this is the part that carries them
out, so two different goals differ by plan, not by a new graph. A step the member must act on (a sign-off) is a decision the
run hands back, never something the run does by itself."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from app.agents.runtime.budget import Budget, BudgetExceeded
from app.agents.runtime.decisions import record_decision
from app.agents.runtime.tools import ToolRegistry

logger = logging.getLogger(__name__)

SkipRule = Callable[[dict], Awaitable[Optional[str]]]
_BUDGET_FOR_COST = {"llm": "llm_calls", "provider": "provider_calls", "worker": "worker_jobs"}


@dataclass
class Step:
    tool: str
    label: str
    args: dict[str, Any] = field(default_factory=dict)
    required: bool = True                 # a required step that fails stops the run; an optional one is reported and skipped past
    skip_if: Optional[SkipRule] = None    # returns a plain reason to skip, or None to run


@dataclass
class Outcome:
    tool: str
    label: str
    status: str                           # done, skipped, failed, stopped, not_run
    detail: str = ""
    value: Any = None
    seconds: float = 0.0

    def public(self) -> dict:
        return {"tool": self.tool, "label": self.label, "status": self.status, "detail": self.detail}


class PlanExecutor:
    def __init__(
        self, registry: ToolRegistry, *, workspace_id: str, user_id: Optional[str] = None, budget: Optional[Budget] = None,
        before_step: Optional[Callable[[str, int, int], Awaitable[None]]] = None, run_id: Optional[str] = None,
        subject: Optional[dict] = None,
    ) -> None:
        self.registry = registry
        self.workspace_id = workspace_id
        self.user_id = user_id
        self.budget = budget or Budget()
        self.before_step = before_step
        self.run_id = run_id
        self.subject = subject or {}

    async def run(self, steps: list[Step], state: dict) -> list[Outcome]:
        self.registry.validate_plan([s.tool for s in steps])
        outcomes: list[Outcome] = []
        stopped: Optional[str] = None
        total = len(steps)
        for index, step in enumerate(steps):
            if stopped:
                outcomes.append(Outcome(step.tool, step.label, "not_run", stopped))
                continue
            if self.before_step:
                # the pause and cancel point: it raises when the member cancels, which ends the run where it stands
                await self.before_step(step.label, index, total)
            reason = await step.skip_if(state) if step.skip_if else None
            if reason:
                outcomes.append(Outcome(step.tool, step.label, "skipped", reason))
                await self._decide(step, "skipped", reason)
                continue
            spec = self.registry.spec(step.tool)
            bucket = _BUDGET_FOR_COST.get(spec.cost)
            try:
                if bucket:
                    self.budget.spend(bucket)
            except BudgetExceeded as exc:
                stopped = str(exc)
                outcomes.append(Outcome(step.tool, step.label, "stopped", stopped))
                await self._decide(step, "stopped", stopped, outcome="stopped")
                continue
            result = await self.registry.run(step.tool, state=state, **step.args)
            if result.ok:
                outcomes.append(Outcome(step.tool, step.label, "done", "", result.value, result.seconds))
                await self._decide(step, "done", step.label)
                continue
            outcomes.append(Outcome(step.tool, step.label, "failed", result.error or "", None, result.seconds))
            await self._decide(step, "failed", result.error or "", outcome="failed")
            if step.required:
                stopped = f"Stopped because {step.label.lower()} did not finish."
        return outcomes

    async def _decide(self, step: Step, status: str, summary: str, *, outcome: str = "ok") -> None:
        await record_decision(
            workspace_id=self.workspace_id, user_id=self.user_id, agent=self.registry.agent, step=step.tool, run_id=self.run_id,
            summary=f"{step.label}: {summary}" if summary else step.label, outcome=outcome,
            evidence={"status": status, "subject": self.subject, "budget": self.budget.snapshot()},
        )
