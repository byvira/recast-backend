"""Any action as a background job.

A job action is a named piece of work with a typed payload (`content.bulk_delete`, `text.repurpose`, ...). It is submitted through
one route, `POST /api/v1/jobs`, which answers at once with a saved run (see `pipeline_runs`). The run shows in Control Tower with
pause, resume and cancel, survives the member leaving the page, and ends with a note in the Activity Log.

What this adds on top of a plain run:

* **No double starts.** The same action with the same payload from the same member, while one is still active, answers with that
  run instead of starting another. A client can also send its own `Idempotency-Key`.
* **Permissions are checked twice**: when it is submitted and again when it starts, so a role removed in between is respected.
* **Temporary errors are retried** (a busy model, a timeout) a few times with a growing wait, saying so on the run.
* **Pause and cancel reach every model call** the work makes (see `llm.set_run_gate`) and every step it reports.
* **Safe repeats survive a restart.** An action marked `restartable` (it does the same thing when run again from the start) is
  started again after the server restarts, up to twice. Anything else is closed as interrupted, so work that posts to a platform is
  never repeated by itself.
* **Results are capped.** A result too large to keep is replaced by a short summary.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from fastapi import HTTPException
from pydantic import BaseModel, ValidationError

from app.core.rbac import assert_permission
from app.core.workspace import WorkspaceContext
from app.db.mongo import pipeline_runs, users, workspace_members, workspaces
from app.shared import pipeline_runs as runs

logger = logging.getLogger(__name__)

MAX_RESULT_BYTES = 400_000
RETRY_WAITS = (2.0, 6.0, 15.0)
MAX_RESTARTS = 2
_TRANSIENT_STATUS = {429, 500, 502, 503, 504}


@dataclass
class JobAction:
    name: str
    permission: str
    payload_model: type[BaseModel]
    #: `run(ctx, payload, reporter)` does the work and returns something JSON friendly. `reporter.step("label")` records progress
    #: and is a pause and cancel point.
    run: Callable[[WorkspaceContext, Any, runs.StepReporter], Awaitable[Any]]
    title: Callable[[Any], str]
    kind: str = "text"
    steps: Callable[[Any], Optional[int]] = lambda payload: None
    href: Optional[str] = None
    restartable: bool = False
    retries: int = 2
    #: Model calls the work makes wait at the run gate, so pause and cancel also take effect between them.
    gated: bool = False
    description: str = ""
    #: Turns what the work returned into the small piece kept on the run (for example just an id), when the full result is large.
    shape: Optional[Callable[[Any], Any]] = None
    #: Where the finished work can be opened, worked out from what it returned.
    result_href: Optional[Callable[[Any], Optional[str]]] = None
    extra: dict = field(default_factory=dict)


ACTIONS: dict[str, JobAction] = {}


def register(action: JobAction) -> JobAction:
    ACTIONS[action.name] = action
    return action


def action_summaries() -> list[dict]:
    if not ACTIONS:
        from app.shared.job_actions import register_all

        register_all()
    return [
        {"action": a.name, "permission": a.permission, "kind": a.kind, "restartable": a.restartable, "description": a.description}
        for a in sorted(ACTIONS.values(), key=lambda a: a.name)
    ]


# ── Helpers ──────────────────────────────────────────────────────────

async def build_context(workspace_id: str, user_id: str) -> WorkspaceContext:
    """The request context for work that runs after the request has ended (or after a restart), loaded fresh."""
    workspace = await workspaces.find_one({"id": workspace_id})
    member = await workspace_members.find_one({"workspace_id": workspace_id, "user_id": user_id})
    user = await users.find_one({"id": user_id})
    if not workspace or not member or not user or member.get("status", "active") != "active":
        raise HTTPException(status_code=403, detail="Your access to this workspace was removed before the work started.")
    return WorkspaceContext(workspace, member, user)


def is_temporary(exc: BaseException) -> bool:
    """Errors worth another try: a busy or briefly unavailable model, a timeout, a dropped connection."""
    if isinstance(exc, HTTPException):
        return exc.status_code in _TRANSIENT_STATUS
    import httpx

    return isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, httpx.TransportError))


def idempotency_key(workspace_id: str, user_id: str, action: str, payload: dict, client_key: Optional[str] = None) -> str:
    if client_key:
        return f"{workspace_id}:{user_id}:{action}:{client_key.strip()[:120]}"
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:32]
    return f"{workspace_id}:{user_id}:{action}:{digest}"


def make_result(value: Any) -> dict:
    """The run's result: the value as plain data, or a summary when it is too large to keep."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    elif isinstance(value, list):
        value = [v.model_dump(mode="json") if isinstance(v, BaseModel) else v for v in value]
    try:
        text = json.dumps(value, default=str)
    except (TypeError, ValueError):
        return {"data": None, "note": "The result could not be saved."}
    if len(text.encode("utf-8")) > MAX_RESULT_BYTES:
        count = len(value) if isinstance(value, (list, dict)) else 1
        return {"data": None, "truncated": True, "note": f"The result was too large to keep ({count} items). Open the items directly."}
    return {"data": json.loads(text)}


def run_gate(run_id: str):
    """The check a run's model calls make first: waits while paused, raises when cancelled. Looks at most once a second."""
    import time

    last = {"at": 0.0}

    async def gate() -> None:
        if time.monotonic() - last["at"] < 1.0:
            return
        await runs.checkpoint(run_id)
        last["at"] = time.monotonic()

    return gate


# ── Running ──────────────────────────────────────────────────────────

async def _cancel_requested(run_id: str) -> bool:
    row = await pipeline_runs.find_one({"id": run_id}, {"cancel_requested": 1})
    return bool(row and row.get("cancel_requested"))


def _worker(action: JobAction, payload: Any):
    async def work(doc: dict) -> dict:
        from app.shared.llm import set_run_gate

        # The member's role may have changed since the request: check again, with a fresh load.
        ctx = await build_context(doc["workspace_id"], doc["created_by"])
        assert_permission(ctx.member, action.permission)
        if action.gated:
            set_run_gate(run_gate(doc["id"]))
        reporter = runs.StepReporter(doc["id"])

        attempt = 0
        while True:
            try:
                value = await action.run(ctx, payload, reporter)
                break
            except runs.RunCancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                # A handler that turns every error into its own message also turns the cancel signal into one. If the member had
                # cancelled, that is what happened, whatever the error says.
                if await _cancel_requested(doc["id"]):
                    raise runs.RunCancelled() from exc
                if attempt >= action.retries or attempt >= len(RETRY_WAITS) or not is_temporary(exc):
                    raise
                wait = RETRY_WAITS[attempt]
                attempt += 1
                logger.warning("Job %s (%s) hit a temporary error, trying again in %.0fs: %s", doc["id"], action.name, wait, exc)
                await runs.set_stage(doc["id"], f"Trying again (attempt {attempt + 1})", attempts=attempt + 1)
                await asyncio.sleep(wait)
                await runs.checkpoint(doc["id"])
        # A cancel that came in during the last step still counts.
        await runs.checkpoint(doc["id"])
        out = make_result(action.shape(value) if action.shape else value)
        href = action.result_href(value) if action.result_href else None
        if href:
            out["href"] = href
        return out

    return work


async def submit(
    *, action_name: str, payload: dict, ctx: WorkspaceContext, client_key: Optional[str] = None,
) -> tuple[dict, bool]:
    """Start the action in the background. Returns (run, already_running)."""
    if not ACTIONS:
        from app.shared.job_actions import register_all

        register_all()
    action = ACTIONS.get(action_name)
    if action is None:
        raise HTTPException(status_code=404, detail=f"There is no action called '{action_name}'.")
    assert_permission(ctx.member, action.permission)
    try:
        parsed = action.payload_model.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=json.loads(exc.json(include_url=False, include_context=False)))

    key = idempotency_key(ctx.workspace_id, ctx.user_id, action_name, parsed.model_dump(mode="json"), client_key)
    existing = await pipeline_runs.find_one({"idem_key": key, "status": {"$in": list(runs.ACTIVE)}}, {"_id": 0})
    if existing:
        return runs.public(existing), True

    await runs.assert_capacity(ctx.workspace_id)
    doc = await runs.create_run(
        workspace_id=ctx.workspace_id, user_id=ctx.user_id, kind=action.kind, title=action.title(parsed),
        steps_total=action.steps(parsed), href=action.href,
        extra={"action": action_name, "idem_key": key, "restartable": action.restartable, "restart_attempts": 0,
               **({"payload": parsed.model_dump(mode="json")} if action.restartable else {})},
    )
    runs.start(doc, _worker(action, parsed))
    return runs.public(doc), False


async def resume_interrupted() -> int:
    """After a restart: start again every safe-to-repeat job whose server died. Others are left for `fail_interrupted`. A job is
    claimed with one atomic update, so two servers starting together never both take it. Returns how many were started."""
    from datetime import datetime, timezone

    cutoff = datetime.now(timezone.utc) - runs.STALE_AFTER
    started = 0
    async for stale in pipeline_runs.find({
        "status": {"$in": list(runs.ACTIVE)}, "restartable": True, "restart_attempts": {"$lt": MAX_RESTARTS},
        "$and": [
            {"$or": [{"heartbeat_at": {"$lt": cutoff}}, {"heartbeat_at": {"$exists": False}}, {"heartbeat_at": None}]},
            {"created_at": {"$lt": cutoff}},
        ],
    }, {"_id": 0}).limit(50):
        action = ACTIONS.get(stale.get("action", ""))
        if action is None or not stale.get("payload"):
            continue
        claimed = await pipeline_runs.find_one_and_update(
            {"id": stale["id"], "restart_attempts": stale.get("restart_attempts", 0), "status": {"$in": list(runs.ACTIVE)}},
            {"$set": {"status": "queued", "heartbeat_at": datetime.now(timezone.utc), "stage": "Starting again after a restart",
                      "steps_done": 0, "paused_at": None, "pause_requested": False},
             "$inc": {"restart_attempts": 1}},
            projection={"_id": 0}, return_document=True,
        )
        if not claimed:
            continue
        try:
            payload = action.payload_model.model_validate(claimed["payload"])
        except ValidationError:
            continue
        runs.start(claimed, _worker(action, payload))
        started += 1
    return started
