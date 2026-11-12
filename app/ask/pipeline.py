"""From a person's words to a typed plan. Code decides, models propose: every answer a model gives is checked against the closed list of tools the
person's role allows right now, and anything outside it is dropped. The result is a plan (steps the compound runner already understands) that the
gatekeeper then prices and checks; nothing here runs a tool.

Steps, each a model call that costs one planning credit:
  1. decide: one tool, a plan, a short question, or nothing fits.
  2. plan (only when decide said more than one step): the steps, using only the shortlisted tools."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from pydantic import ValidationError

from app.ask import fence
from app.tools import compose
from app.tools.contract import Mode, Risk, Scope, ToolSpec

logger = logging.getLogger(__name__)

Think = Callable[[str, str, str], Awaitable[dict[str, Any]]]
#: charged once per model step; the caller wraps this so a failed step is released
Charge = Callable[[str], Any]

#: a tool that needs a person's own yes is shown to them, never run by Ask
NOT_RUN_BY_ASK = {Risk.PUBLISH, Risk.DECISION, Risk.DESTRUCTIVE, Risk.ADMIN}


@dataclass
class Understanding:
    action: str                                   # tool | plan | ask | none | hand_off
    language: str = ""
    tool: Optional[str] = None
    question: Optional[str] = None
    reason: str = ""
    steps: list[compose.Step] = field(default_factory=list)
    shortlist: list[str] = field(default_factory=list)
    how: str = "keywords"
    model_steps: int = 0                          # how many planning credits this used
    problems: list[str] = field(default_factory=list)


def _line(spec: ToolSpec) -> str:
    return f"- {spec.name}: {spec.summary}"


def _inputs(spec: ToolSpec) -> str:
    schema = spec.input_schema() or {}
    props = list((schema.get("properties") or {}).keys())
    required = set(schema.get("required") or [])
    shown = ", ".join(f"{p}{'*' if p in required else ''}" for p in props[:12])
    return f"- {spec.name}({shown})"


def decide_prompt(message: str, allowed: list[ToolSpec], language: str) -> tuple[str, str]:
    system = (
        f"{fence.RULES}\nYou are the router of an assistant that manages a content app. Choose what to do with the person's request.\n"
        'Answer: {"action": "tool" | "plan" | "ask" | "none", "tool": "<one allowed name or null>", "question": "<one short question or null>", "reason": "<one short sentence>"}\n'
        'Use "tool" when one allowed tool does the whole job, "plan" when several steps are needed, "ask" only when something essential is missing, '
        '"none" when no allowed tool fits. Write any question in the language with code '
        f'"{language or "the same as the request"}".'
    )
    prompt = "Allowed tools:\n" + "\n".join(_line(s) for s in allowed) + "\n\nRequest:\n" + fence.fence(message)
    return prompt, system


def plan_prompt(message: str, allowed: list[ToolSpec]) -> tuple[str, str]:
    system = (
        f"{fence.RULES}\nMake a plan of at most {compose.MAX_STEPS} steps using only the allowed tools, in the order they must run.\n"
        'Answer: {"steps": [{"label": "one_lowercase_word", "tool": "<allowed name>", "arguments": {}, "optional": false}]}\n'
        'A later step may use an earlier step\'s result, written exactly as "${label.path}". Use only the argument names a tool lists (a * marks a required one). '
        "Never invent an id; if an id is needed, an earlier step must produce it."
    )
    prompt = "Allowed tools (arguments in brackets):\n" + "\n".join(_inputs(s) for s in allowed) + "\n\nRequest:\n" + fence.fence(message)
    return prompt, system


def parse_decision(raw: dict[str, Any], names: set[str]) -> tuple[str, Optional[str], Optional[str], str]:
    """(action, tool, question, reason) from a model's answer, with anything outside the closed set turned into "none"."""
    action = str(raw.get("action") or "none").lower().strip()
    tool = raw.get("tool") if isinstance(raw.get("tool"), str) else None
    question = fence.clean(str(raw.get("question") or ""), 300) or None
    reason = fence.clean(str(raw.get("reason") or ""), 200)
    if action not in {"tool", "plan", "ask", "none"}:
        action = "none"
    if action == "tool" and tool not in names:
        return "none", None, None, reason or "No allowed tool fits."
    if action == "ask" and not question:
        return "none", None, None, reason
    return action, tool if action == "tool" else None, question if action == "ask" else None, reason


def parse_plan(raw: dict[str, Any], names: set[str]) -> tuple[list[compose.Step], list[str]]:
    """The steps from a model's answer and the problems found. A step naming a tool outside the allowed set, or one that is not a valid step, drops the
    whole plan: a plan with a hole in it is not run."""
    problems: list[str] = []
    steps: list[compose.Step] = []
    items = raw.get("steps")
    if not isinstance(items, list) or not items:
        return [], ["The plan had no steps."]
    for item in items[: compose.MAX_STEPS + 1]:
        try:
            step = compose.Step(**{k: item.get(k) for k in ("label", "tool", "arguments", "optional") if isinstance(item, dict) and item.get(k) is not None})
        except (ValidationError, TypeError, AttributeError):
            problems.append("A step was not written correctly.")
            continue
        if step.tool not in names:
            problems.append(f"A step used a tool that is not allowed ({step.tool}).")
            continue
        steps.append(step)
    if problems or not steps:
        return [], problems or ["The plan had no steps."]
    try:
        compose.validate(steps)
    except Exception as exc:  # noqa: BLE001 - the runner's own checks (labels, references, step limit)
        return [], [str(getattr(exc, "message", None) or exc)[:200]]
    return steps, []


async def understand(
    message: str, specs: list[ToolSpec], language: str, *, think: Think, charge: Charge, shortlisted: list[str], how: str,
) -> Understanding:
    """Decide, and plan when more than one step is needed. `charge(step_name)` is an async context manager that takes one planning credit and gives it
    back if the block raises. Only tools Ask may run are offered to the planner; a tool that needs the person's own yes can be chosen only as a hand-off."""
    by_name = {s.name: s for s in specs}
    candidates = [by_name[n] for n in shortlisted if n in by_name]
    result = Understanding(action="none", language=language, shortlist=[s.name for s in candidates], how=how)
    if not candidates:
        result.reason = "No tool is available to you for that."
        return result

    prompt, system = decide_prompt(message, candidates, language)
    async with charge("decide"):
        raw = await think("decide", prompt, system)
    result.model_steps += 1
    action, tool, question, reason = parse_decision(raw, {s.name for s in candidates})
    result.reason, result.question = reason, question

    if action == "tool" and tool:
        spec = by_name[tool]
        if spec.risk in NOT_RUN_BY_ASK:
            result.action, result.tool = "hand_off", tool
            return result
        result.action, result.tool = "tool", tool
        required = (spec.input_schema() or {}).get("required") or []
        if not required:
            # nothing to fill in: the tool runs as it is, with no second model step
            result.steps = [compose.Step(label=spec.name.split(".")[-1].replace("-", "_")[:24].lower() or "step", tool=spec.name, arguments={})]
            return result
        prompt, system = plan_prompt(message, [spec])
        async with charge("fill"):
            raw = await think("plan", prompt, system)
        result.model_steps += 1
        steps, problems = parse_plan(raw, {spec.name})
        result.problems = problems
        if len(steps) == 1:
            result.steps = steps
        else:
            result.action, result.reason = "none", result.reason or "I could not work out what to fill in."
        return result
    if action == "ask":
        result.action = "ask"
        return result
    if action != "plan":
        return result

    runnable = [s for s in candidates if s.risk not in NOT_RUN_BY_ASK]
    if not runnable:
        return result
    prompt, system = plan_prompt(message, runnable)
    async with charge("plan"):
        raw = await think("plan", prompt, system)
    result.model_steps += 1
    steps, problems = parse_plan(raw, {s.name for s in runnable})
    result.problems = problems
    if steps:
        result.action, result.steps = "plan", steps
    else:
        result.reason = result.reason or "I could not make a safe plan for that."
    return result


def as_text(value: Any) -> str:
    """A compact, safe line for a work log from a tool result."""
    try:
        return fence.clean(json.dumps(value, ensure_ascii=False, default=str), 400)
    except Exception:  # noqa: BLE001
        return ""
