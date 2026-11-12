"""Compound requests: "write a Facebook post about business tactics, with hashtags and a picture" as one run of steps.

Each step is an ordinary tool call, made through the gatekeeper like any other, so every check, price, limit and record still applies. What
this adds is what happens when a step fails:

* every step's result, charge and error are kept on the run, so a failure at step three keeps steps one and two (and their drafts);
* a step marked `optional` (the picture, say) never sinks the run: the run ends `partial` and names what is missing;
* a failure that trying again can fix is retried inside the call only when the wait is short (a second or three); a longer wait (the writing
  service is busy for a minute) stops the run cleanly with `retry_after_seconds` instead of holding the connection open;
* `compose.resume` redoes only what failed or never ran. Each step carries its own idempotency key (`<run>:<step>`), so a step that already
  worked is answered from its first result and is never made or charged twice;
* work that takes a while (writing a post is a background run) is waited for up to a minute, then the run says `waiting` and can be checked.

Publishing, deciding and deleting cannot be steps: they need a person's yes or the owner's switch, so they are called on their own."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.core import failures
from app.db.mongo import get_db
from app.shared import pipeline_runs
from app.tools import executor
from app.tools.contract import Mode, Risk, Scope, ToolSpec
from app.tools.executor import ToolFailure, handler
from app.tools.gatekeeper import Admission, Call, Gatekeeper, Refusal
from app.tools.registry import get_registry

MAX_STEPS = 6
WAIT_SECONDS = 60
POLL_SECONDS = 2.0
KEEP_DAYS = 30
MAX_RESULT_BYTES = 20_000
_LABEL = r"^[a-z][a-z0-9_]{0,23}$"
_REF = re.compile(r"^\$\{([a-z][a-z0-9_]*)\.([A-Za-z0-9_.]+)\}$")
#: a step that cannot be part of a run: it needs a person's yes, the owner's switch, or is a run itself
_NOT_A_STEP = {Risk.PUBLISH, Risk.DECISION, Risk.DESTRUCTIVE, Risk.ADMIN}

RunTool = Callable[[Call], Awaitable[tuple[Any, Optional[dict[str, Any]]]]]
GetJob = Callable[[str, str], Awaitable[Optional[dict[str, Any]]]]


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(pattern=_LABEL, description="A short name other steps can refer to, such as write or picture.")
    tool: str = Field(min_length=3, max_length=80)
    arguments: dict[str, Any] = Field(default_factory=dict, description='A value written exactly as "${label.path}" is replaced by that earlier step\'s result.')
    optional: bool = Field(False, description="An optional step that fails does not fail the run: it ends partial and names what is missing.")


class ComposeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: list[Step] = Field(min_length=1, max_length=MAX_STEPS)


class RunRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(min_length=1, max_length=64)


class ComposeOut(BaseModel):
    model_config = ConfigDict(extra="allow")


def specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="compose.run", summary="Do several things as one request, for example write a post, add hashtags and make a picture, keeping every finished step if one fails.",
            risk=Risk.WRITE, scope=Scope.CREATE, mode=Mode.INSTANT, input_model=ComposeIn, output_model=ComposeOut, permission="create_content",
            cost_note="The sum of what its steps cost; nothing extra for the run. A dry run shows the total.", source="query:compose.run",
            examples=("Make a Facebook post about business tactics with hashtags and a picture",), version=1,
        ),
        ToolSpec(
            name="compose.status", summary="Show where a compound request stands: each step, what finished, what failed and what to do next.", risk=Risk.READ, scope=Scope.READ,
            mode=Mode.INSTANT, input_model=RunRef, output_model=ComposeOut, source="query:compose.status", examples=("How did that post request go?",),
        ),
        ToolSpec(
            name="compose.resume", summary="Carry on a compound request: redo only the steps that failed or never ran. Finished steps are not made or charged again.",
            risk=Risk.WRITE, scope=Scope.CREATE, mode=Mode.INSTANT, input_model=RunRef, output_model=ComposeOut, permission="create_content",
            cost_note="Only the steps that run again are priced.", source="query:compose.resume", examples=("Try the picture again",),
        ),
    ]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def runs():
    return get_db()["tool_compose_runs"]


async def create_indexes() -> None:
    await runs().create_index("id", unique=True)
    await runs().create_index("created_at", expireAfterSeconds=KEEP_DAYS * 24 * 3600)


# ── arguments that point at an earlier step ──────────────────────────────

class RefError(Exception):
    pass


def _dig(value: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        elif isinstance(value, dict) and part in value:
            value = value[part]
        else:
            raise KeyError(part)
    return value


def resolve(arguments: Any, earlier: list[dict[str, Any]]) -> Any:
    """Fill in "${label.path}" values from the steps before. A reference to a step that did not finish, or to something it did not return, is
    a plain error naming it."""
    if isinstance(arguments, str):
        found = _REF.match(arguments)
        if not found:
            return arguments
        label, path = found.groups()
        step = next((s for s in earlier if s["label"] == label), None)
        if step is None or step["state"] != "done":
            raise RefError(f"The step {label!r} has not finished, so {arguments} cannot be used yet.")
        try:
            return _dig(step.get("result"), path)
        except KeyError:
            raise RefError(f"The step {label!r} did not return {path!r}.")
    if isinstance(arguments, dict):
        return {k: resolve(v, earlier) for k, v in arguments.items()}
    if isinstance(arguments, list):
        return [resolve(v, earlier) for v in arguments]
    return arguments


def validate(steps: list[Step]) -> None:
    """A run's steps are checked before anything starts, so a typo costs nothing."""
    registry, seen = get_registry(), set()
    for step in steps:
        if step.label in seen:
            raise ToolFailure("input_invalid", f"Two steps are called {step.label!r}. Give each a different label.", 422)
        seen.add(step.label)
        tool = registry.get(step.tool)
        if tool is None:
            raise ToolFailure("tool_unknown", f"There is no tool called {step.tool!r}.", 404)
        if tool.name.startswith("compose.") or tool.name == "changes.undo" or tool.risk in _NOT_A_STEP:
            raise ToolFailure("not_a_step", f"{step.tool} cannot be a step. Publishing, deciding and deleting need a person's yes, so call them on their own.", 422)
        for value in _walk_refs(step.arguments):
            label = _REF.match(value).group(1)  # type: ignore[union-attr]
            if label not in seen or label == step.label:
                raise ToolFailure("input_invalid", f"Step {step.label!r} refers to {label!r}, which does not come before it.", 422)


def _walk_refs(value: Any):
    if isinstance(value, str) and _REF.match(value):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _walk_refs(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk_refs(v)


# ── running ──────────────────────────────────────────────────────────────

def _trim(data: Any) -> Any:
    try:
        text = json.dumps(data, default=str)
    except (TypeError, ValueError):
        return None
    return json.loads(text) if len(text.encode("utf-8")) <= MAX_RESULT_BYTES else {"note": "The result was too large to keep here. Open the item itself."}


def _warnings(outcome_data: Any) -> list[dict[str, str]]:
    return list(executor.degraded(outcome_data))


def _error(body: dict[str, Any]) -> dict[str, Any]:
    return {k: body[k] for k in ("code", "message", "retryable", "retry_after_seconds", "next") if k in body}


async def _default_run_tool(gate: Gatekeeper, call: Call) -> tuple[Any, Optional[dict[str, Any]]]:
    _, outcome = await executor.run_call(gate, call)
    if outcome is None:                                    # an answer repeated from the first
        return {}, None
    return outcome.data, outcome.run


async def _default_get_job(workspace_id: str, run_id: str) -> Optional[dict[str, Any]]:
    return await pipeline_runs.get_run(run_id, workspace_id)


def from_job_failure(job: dict[str, Any]) -> dict[str, Any]:
    """A background run that ended badly, in the shared error shape. Its own words (a model's, a provider's) are never passed on."""
    text = str(job.get("error") or "")
    if failures._AI_WORDS.search(text) or "503" in text:
        return failures.ai_busy().body()
    return failures.Failure("step_failed", "That step did not finish. Nothing was charged for what did not get made.", True, 30, "Resume the run to try that step again.", 500).body()


async def _wait_job(workspace_id: str, job_id: str, budget: float, get_job: GetJob, sleep: Callable[[float], Awaitable[None]]) -> tuple[str, Optional[dict[str, Any]]]:
    """Wait for a background run, for at most `budget` seconds. Returns ("done" | "failed" | "running", the run)."""
    waited = 0.0
    while True:
        job = await get_job(workspace_id, job_id)
        status = (job or {}).get("status")
        if status in ("done", "failed", "cancelled"):
            return ("done" if status == "done" else "failed"), job
        if waited >= budget:
            return "running", job
        await sleep(POLL_SECONDS)
        waited += POLL_SECONDS


async def _do_step(run: dict[str, Any], index: int, credential_id: str, run_tool: RunTool, get_job: GetJob, sleep: Callable[[float], Awaitable[None]], budget: list[float]) -> None:
    step = run["steps"][index]
    step["attempts"] = int(step.get("attempts") or 0)
    if step["state"] == "running" and step.get("job_id"):
        state, job = await _wait_job(run["workspace_id"], step["job_id"], budget[0], get_job, sleep)
        _finish_job(step, state, job)
        return
    try:
        arguments = resolve(step["arguments"], run["steps"][:index])
    except RefError as exc:
        step.update(state="failed", error={"code": "input_invalid", "message": str(exc), "retryable": False})
        return
    for attempt in range(1 + failures.AUTO_RETRIES):
        step["attempts"] += 1
        step["state"] = "running"
        try:
            data, job = await run_tool(Call(credential_id=credential_id, tool=step["tool"], arguments=arguments, idempotency_key=f"{run['id']}:{index}"))
        except (Refusal, ToolFailure) as failure:
            body = failure.body()
            wait = body.get("retry_after_seconds")
            # trying again right away helps only when the wait is short; a minute's wait is the caller's to make, not ours to hold a connection for
            if body.get("retryable") and wait is not None and wait <= 10 and attempt < failures.AUTO_RETRIES:
                await sleep(float(wait) or failures.RETRY_WAITS[min(attempt, 1)])
                continue
            step.update(state="failed", error=_error(body))
            return
        step.update(result=_trim({"data": data}), error=None, warnings=_warnings(data))
        if job and job.get("id"):
            step.update(state="running", job_id=job["id"])
            state, job_row = await _wait_job(run["workspace_id"], job["id"], budget[0], get_job, sleep)
            _finish_job(step, state, job_row)
        else:
            step["state"] = "done"
        return


def _finish_job(step: dict[str, Any], state: str, job: Optional[dict[str, Any]]) -> None:
    if state == "done":
        result = (job or {}).get("result")
        step.update(state="done", error=None, result=_trim({**(step.get("result") or {}), "run_result": result}))
    elif state == "failed":
        step.update(state="failed", error=from_job_failure(job or {}))
    else:
        step["state"] = "running"


def summarise(run: dict[str, Any]) -> dict[str, Any]:
    """The run as a caller sees it: where it stands, what is kept, what is missing, and what to do next."""
    steps = run["steps"]
    done = [s["label"] for s in steps if s["state"] == "done"]
    failed = [s for s in steps if s["state"] == "failed"]
    waiting = [s for s in steps if s["state"] == "running"]
    missing = [s["label"] for s in steps if s["state"] != "done"]
    required_failed = [s for s in failed if not s.get("optional")]
    if waiting:
        status = "waiting"
    elif not failed and not [s for s in steps if s["state"] == "pending"]:
        status = "done"
    elif required_failed:
        status = "partial" if done else "failed"
    elif failed:
        status = "partial"
    else:
        status = "partial" if done else "failed"
    retry = [s for s in failed if (s.get("error") or {}).get("retryable")]
    after = max([(s["error"].get("retry_after_seconds") or 0) for s in retry], default=None) if retry else None
    nxt = ""
    if status == "waiting":
        nxt = "It is still working. Check compose.status in a little while."
    elif status in ("partial", "failed") and retry:
        nxt = f"Call compose.resume with this run_id{f' after {after} seconds' if after else ''}. Only {', '.join(s['label'] for s in retry)} will run again; finished steps are kept and not charged again."
    elif status in ("partial", "failed"):
        nxt = "What failed cannot be fixed by trying again. Read the step's message, change what is needed and make a new request. Finished steps are kept."
    out: dict[str, Any] = {
        "run_id": run["id"], "status": status, "done": done, "missing": missing if status != "done" else [], "next": nxt,
        "steps": [{"label": s["label"], "tool": s["tool"], "state": s["state"], "optional": bool(s.get("optional")), "attempts": s.get("attempts", 0),
                   **({"warnings": s["warnings"]} if s.get("warnings") else {}),
                   **({"error": s["error"]} if s.get("error") else {}), **({"result": s["result"]} if s.get("result") and s["state"] == "done" else {})} for s in steps],
    }
    if after is not None:
        out["retry_after_seconds"] = after
    return out


async def advance(run: dict[str, Any], credential_id: str, *, run_tool: RunTool, get_job: GetJob = _default_get_job, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                  wait_seconds: float = WAIT_SECONDS) -> dict[str, Any]:
    """Run every step that is not done, in order. A required step that fails stops the run (later steps need it); an optional one that fails
    is passed over. The run is saved after every step, so a crash or a closed connection loses nothing."""
    budget = [wait_seconds]
    for index, step in enumerate(run["steps"]):
        if step["state"] == "done" or (step["state"] == "failed" and not (step.get("error") or {}).get("retryable")):
            continue
        await _do_step(run, index, credential_id, run_tool, get_job, sleep, budget)
        await runs().update_one({"id": run["id"]}, {"$set": {"steps": run["steps"], "updated_at": _now()}})
        if step["state"] == "failed" and not step.get("optional"):
            break
        if step["state"] == "running":
            break
    return summarise(run)


async def start(workspace_id: str, credential_id: str, user_id: Optional[str], steps: list[Step], *, run_tool: RunTool,
                on_created: Optional[Callable[[str], Awaitable[None]]] = None, **kw: Any) -> dict[str, Any]:
    validate(steps)
    run = {"id": uuid4().hex, "workspace_id": workspace_id, "credential_id": credential_id, "user_id": user_id, "created_at": _now(), "updated_at": _now(),
           "steps": [{"label": s.label, "tool": s.tool, "arguments": s.arguments, "optional": s.optional, "state": "pending", "attempts": 0} for s in steps]}
    await runs().insert_one(dict(run))
    if on_created is not None:
        await on_created(run["id"])  # a caller that shows progress learns the run's id before the first step starts
    return await advance(run, credential_id, run_tool=run_tool, **kw)


async def load(workspace_id: str, run_id: str) -> dict[str, Any]:
    row = await runs().find_one({"id": run_id, "workspace_id": workspace_id}, {"_id": 0})
    if row is None:
        raise ToolFailure("not_found", "That request was not found. Requests are kept for 30 days.", 404)
    return row


# ── the tools ────────────────────────────────────────────────────────────

def _gate() -> Gatekeeper:
    from app.tools.services import LiveServices

    return Gatekeeper(get_registry(), LiveServices())


@handler("compose.run")
async def run_tool_handler(admission: Admission) -> dict[str, Any]:
    gate = _gate()
    credential = admission.credential
    return await start(credential.workspace_id, credential.id, credential.user_id, admission.arguments.steps, run_tool=lambda call: _default_run_tool(gate, call))


@handler("compose.status")
async def status_handler(admission: Admission) -> dict[str, Any]:
    run = await load(admission.credential.workspace_id, admission.arguments.run_id)
    gate = _gate()
    if any(s["state"] == "running" for s in run["steps"]):
        # a background step may have finished since: look once, without waiting
        return await advance(run, admission.credential.id, run_tool=lambda call: _default_run_tool(gate, call), wait_seconds=0)
    return summarise(run)


@handler("compose.resume")
async def resume_handler(admission: Admission) -> dict[str, Any]:
    run = await load(admission.credential.workspace_id, admission.arguments.run_id)
    if run["credential_id"] != admission.credential.id and run.get("user_id") != admission.credential.user_id:
        raise ToolFailure("not_found", "That request was not found. Requests are kept for 30 days.", 404)
    gate = _gate()
    return await advance(run, admission.credential.id, run_tool=lambda call: _default_run_tool(gate, call))
