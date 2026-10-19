"""Durable background runs.

A long piece of work (a campaign batch, a recording, a set of pictures) is saved here as one `pipeline_runs` row, so it
survives the member leaving the page, can be paused, resumed or cancelled, and ends with a notice in the Activity Log.

States: queued, running, paused, done, failed, cancelled. The work itself runs as an asyncio task in this process. It calls
`checkpoint()` between units of work (for a campaign, between days); that is where a pause waits and a cancel stops it.
Work that is one long call (a picture, a narration, a recording) has no unit to wait for, so it is started with
`interrupt_on_cancel=True`: a cancel then stops the task itself within a couple of seconds, closing the calls it had open.
A cancel for a run that no server is working on any more (it died with a restart) closes it at once.
A restart ends any run that was still going, because its task died with the process: `fail_interrupted()` marks those as
failed on startup so nothing looks busy forever.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional
from uuid import uuid4

from app.db.mongo import pipeline_runs

logger = logging.getLogger(__name__)

ACTIVE = ("queued", "running", "paused")
FINISHED = ("done", "failed", "cancelled")
KINDS = ("campaign", "text", "audio", "image", "video", "odette")
POLL_SECONDS = 1.5
#: A run that is going writes a sign of life this often. One with none for STALE_AFTER is taken to have died with its server.
HEARTBEAT_SECONDS = 30
STALE_AFTER = timedelta(minutes=2)
MAX_POLL_SECONDS = 10.0

_tasks: dict[str, asyncio.Task] = {}


class RunCancelled(Exception):
    """Raised by `checkpoint` when the member cancelled the run."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


# What a job keeps for itself: the key that stops double starts and what it needs to start again after a restart.
_INTERNAL_FIELDS = ("_id", "idem_key", "live_run_id", "payload_digest", "active_key", "interrupted", "payload", "restartable", "restart_attempts", "action_secret")


def public(doc: Optional[dict]) -> Optional[dict]:
    """The row as the API returns it: no internal id, plus a progress percentage when the number of steps is known."""
    if not doc:
        return None
    out = {k: v for k, v in doc.items() if k not in _INTERNAL_FIELDS}
    total = out.get("steps_total") or 0
    if out.get("status") == "done":
        out["progress"] = 100
    elif total:
        out["progress"] = min(95, round(100 * (out.get("steps_done") or 0) / total))
    else:
        out["progress"] = None
    # `progress` is kept for existing readers. A campaign also has a `progress` (post counts), so the percentage has its own name too.
    out["progress_percent"] = out["progress"]
    return out


MAX_ACTIVE_PER_WORKSPACE = 3


async def assert_capacity(workspace_id: str) -> None:
    """Refuse to start more work when this workspace already has MAX_ACTIVE_PER_WORKSPACE going. Several long jobs at once
    would run into the model providers' limits and slow every one of them."""
    from fastapi import HTTPException

    going = await pipeline_runs.count_documents({"workspace_id": workspace_id, "status": {"$in": list(ACTIVE)}})
    if going >= MAX_ACTIVE_PER_WORKSPACE:
        raise HTTPException(
            status_code=429,
            detail=f"{going} jobs are already running. Wait for one to finish, or cancel one, then start this again.",
        )


class RunAlreadyActive(Exception):
    """A run with the same start key is already going. `existing` is that run."""

    def __init__(self, existing: Optional[dict]) -> None:
        super().__init__("A run with this key is already active.")
        self.existing = existing


async def create_run(
    *, workspace_id: str, user_id: str, kind: str, title: str, ref: Optional[dict] = None,
    steps_total: Optional[int] = None, href: Optional[str] = None, extra: Optional[dict] = None,
    active_key: Optional[str] = None,
) -> dict:
    """Save a new run. With an active_key, only one active run may hold the key: a second start with the same key raises
    RunAlreadyActive (a unique index decides, so two requests arriving together cannot both start). The workspace's limit on
    running jobs is checked again after the row is saved, so requests arriving together cannot both take the last slot."""
    from pymongo.errors import DuplicateKeyError

    now = _now()
    doc = {
        "id": str(uuid4()), "workspace_id": workspace_id, "created_by": user_id, "kind": kind, "title": title[:200],
        "status": "queued", "steps_done": 0, "steps_total": steps_total, "ref": ref or {}, "href": href,
        "result": None, "error": None, "pause_requested": False, "cancel_requested": False,
        "created_at": now, "updated_at": now, "started_at": None, "finished_at": None, "paused_at": None,
        "last_reminded_at": None, "reminders_sent": 0,
        **(extra or {}),
    }
    if active_key:
        doc["active_key"] = active_key
    try:
        await pipeline_runs.insert_one(dict(doc))
    except DuplicateKeyError:
        raise RunAlreadyActive(await pipeline_runs.find_one({"active_key": active_key}, {"_id": 0}))
    await _drop_if_over_capacity(doc)
    return doc


async def _drop_if_over_capacity(doc: dict) -> None:
    """Keep only the oldest MAX_ACTIVE_PER_WORKSPACE active runs. A run that is not among them (it lost a race for the last slot)
    is removed again and the member is told to wait."""
    from fastapi import HTTPException

    async def kept() -> bool:
        oldest = await pipeline_runs.find(
            {"workspace_id": doc["workspace_id"], "status": {"$in": list(ACTIVE)}}, {"id": 1},
        ).sort("_id", 1).limit(MAX_ACTIVE_PER_WORKSPACE).to_list(MAX_ACTIVE_PER_WORKSPACE)
        return doc["id"] in {r["id"] for r in oldest}

    # Looked at twice, a moment apart, so starts that arrived together (and whose rows were not all visible the first time) settle
    # on the same oldest few.
    if await kept():
        await asyncio.sleep(0.05)
        if await kept():
            return
    await pipeline_runs.delete_one({"id": doc["id"]})
    raise HTTPException(
        status_code=429,
        detail=f"{MAX_ACTIVE_PER_WORKSPACE} jobs are already running. Wait for one to finish, or cancel one, then start this again.",
    )


async def get_run(run_id: str, workspace_id: str) -> Optional[dict]:
    return await pipeline_runs.find_one({"id": run_id, "workspace_id": workspace_id}, {"_id": 0})


async def list_runs(
    workspace_id: str, *, kind: Optional[str] = None, ref: Optional[dict] = None, include_finished_days: int = 2,
) -> list[dict]:
    """Every active run, plus the ones that finished in the last `include_finished_days` days, newest first."""
    since = _now() - timedelta(days=include_finished_days)
    flt: dict[str, Any] = {
        "workspace_id": workspace_id,
        "$or": [{"status": {"$in": list(ACTIVE)}}, {"finished_at": {"$gte": since}}],
    }
    if kind:
        flt["kind"] = kind
    for key, value in (ref or {}).items():
        flt[f"ref.{key}"] = value
    # The saved result (up to 400 KB each) is left out of the list; it is on the run itself.
    return [d async for d in pipeline_runs.find(flt, {"_id": 0, "result": 0}).sort("created_at", -1).limit(100)]


async def active_run_for(workspace_id: str, kind: str, ref: dict) -> Optional[dict]:
    flt: dict[str, Any] = {"workspace_id": workspace_id, "kind": kind, "status": {"$in": list(ACTIVE)}}
    for key, value in ref.items():
        flt[f"ref.{key}"] = value
    return await pipeline_runs.find_one(flt, {"_id": 0})


async def _set(run_id: str, **fields: Any) -> None:
    fields["updated_at"] = _now()
    await pipeline_runs.update_one({"id": run_id}, {"$set": fields})


_stage_sink: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar("_stage_sink", default=None)


def bind_stage_sink(run_id: Optional[str]) -> None:
    """Work started for this run (and every task it starts) can then say what it is doing with `announce_stage`."""
    _stage_sink.set(run_id)


async def announce_stage(label: str) -> None:
    """Records what the current run is doing, in plain words. Does nothing when the work is not running as a saved run, and never
    fails the work: a missed label only means the line stays as it was."""
    run_id = _stage_sink.get()
    if not run_id:
        return
    try:
        await set_stage(run_id, label)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not record the stage for run %s: %s", run_id, exc)


async def set_stage(run_id: str, label: str, **fields: Any) -> None:
    """What the run is doing now, in plain words (shown in Control Tower)."""
    await _set(run_id, stage=label, **fields)


async def mark_progress(run_id: str, steps_done: int, steps_total: Optional[int] = None) -> None:
    fields: dict[str, Any] = {"steps_done": steps_done}
    if steps_total is not None:
        fields["steps_total"] = steps_total
    await _set(run_id, **fields)


async def checkpoint(run_id: str) -> None:
    """Call between units of work. Returns when the run may continue. Waits while paused. Raises RunCancelled if cancelled."""
    waited = 0.0
    while True:
        doc = await pipeline_runs.find_one({"id": run_id}, {"pause_requested": 1, "cancel_requested": 1, "status": 1})
        if not doc or doc.get("cancel_requested"):
            raise RunCancelled()
        if doc.get("pause_requested"):
            if doc.get("status") != "paused":
                await _set(run_id, status="paused", paused_at=_now())
            # Look often at first so resume and cancel feel instant, then less often so a long pause costs little.
            delay = POLL_SECONDS if waited < 60 else MAX_POLL_SECONDS
            await asyncio.sleep(delay)
            waited += delay
            continue
        if doc.get("status") == "paused":
            await _set(run_id, status="running", paused_at=None)
        return


#: A run with no worker here and no sign of life for this long has no server working on it any more.
ORPHAN_AFTER = timedelta(seconds=75)


def _has_no_live_worker(doc: dict) -> bool:
    """True when this server is not running the run and nobody else has been beating for it lately."""
    if doc["id"] in _tasks:
        return False
    if doc.get("external_worker"):
        # A render on the video worker is not a task here; its job queue decides whether it is alive.
        return False
    beat = doc.get("heartbeat_at") or doc.get("created_at")
    if isinstance(beat, datetime) and beat.tzinfo is None:
        beat = beat.replace(tzinfo=timezone.utc)
    return not isinstance(beat, datetime) or _now() - beat > ORPHAN_AFTER


async def control(run_id: str, workspace_id: str, action: str) -> Optional[dict]:
    """pause, resume or cancel. Returns the row, or None when there is no such run. A finished run is returned unchanged."""
    doc = await get_run(run_id, workspace_id)
    if not doc:
        return None
    if doc["status"] in FINISHED:
        return doc
    if action == "cancel" and _has_no_live_worker(doc):
        # Its server died with it (a restart, a redeploy): there is nothing left to stop, so it is closed now and does not sit
        # on "Cancelling" until a later sweep.
        await _finish({**doc, "id": run_id}, "cancelled")
        return await get_run(run_id, workspace_id)
    if action in ("pause", "resume") and _has_no_live_worker(doc):
        # Nothing is working on it any more, so there is nothing to pause or resume. Say so instead of leaving a request nobody reads.
        from fastapi import HTTPException

        raise HTTPException(status_code=409, detail="This is no longer running on the server, so it can't be paused or resumed. Start it again.")
    if action == "pause":
        await _set(run_id, pause_requested=True)
    elif action == "resume":
        await _set(run_id, pause_requested=False, last_reminded_at=None, reminders_sent=0)
    elif action == "cancel":
        # A paused run is cancelled at its next checkpoint, which comes within a moment.
        await _set(run_id, cancel_requested=True, pause_requested=False)
    else:
        raise ValueError(f"Unknown action: {action}")
    return await get_run(run_id, workspace_id)


async def _finish(doc: dict, status: str, *, result: Optional[dict] = None, error: Optional[str] = None) -> None:
    now = _now()
    # Only a run that is still active is closed, so a late finish can never overwrite an outcome already recorded. The start key is
    # released so the same work can be started again.
    closed = await pipeline_runs.update_one(
        {"id": doc["id"], "status": {"$in": list(ACTIVE)}},
        {"$set": {"status": status, "result": result, "error": error, "finished_at": now, "updated_at": now, "pause_requested": False},
         "$unset": {"active_key": ""}},
    )
    if not closed.modified_count:
        return
    if status == "cancelled" and doc.get("kind") == "campaign" and (doc.get("ref") or {}).get("campaign_id"):
        # A cancelled batch must not be started again by the next scheduler tick: the campaign waits for its next turn.
        try:
            from app.pipelines.campaigns.batch_runner import skip_to_next_cadence

            await skip_to_next_cadence(doc["ref"]["campaign_id"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not move campaign %s to its next turn after a cancel: %s", doc["ref"].get("campaign_id"), exc)
    try:
        from app.shared.activity import record_system

        wording = {"done": "finished", "failed": "failed", "cancelled": "was cancelled"}
        await record_system(
            workspace_id=doc["workspace_id"], key=f"run:{doc['id']}", actor_name="Ekavo",
            actor_user_id=doc.get("created_by"), category="content_generated",
            title=f"{doc['title']} {wording[status]}",
            description=error or ("Your work is ready to review." if status == "done" else "Nothing more was made."),
            status={"done": "success", "cancelled": "warning"}.get(status, "failed"),
            target_id=doc["id"], target_type="Run", href=doc.get("href"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not write the Activity Log entry for run %s: %s", doc["id"], exc)


class StepReporter:
    """Lets existing work that reports `await run.step("label")` run as a saved run. Each step records its label and progress,
    then waits at a checkpoint, so a pause or a cancel takes effect between steps."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._done = 0

    async def step(self, label: str) -> None:
        await _set(self.run_id, stage=label, steps_done=self._done)
        self._done += 1
        await checkpoint(self.run_id)


def _plain(exc: Exception) -> str:
    detail = getattr(exc, "detail", None)
    return str(detail or exc)[:300] or "The work could not be finished."


async def _watch_cancel(run_id: str, task: "asyncio.Future", flag: list, max_seconds: Optional[float] = None, *, honour_cancel: bool = True) -> None:
    """Stops `task` as soon as the member cancels the run, instead of waiting for it to reach a checkpoint. A run that goes on
    past `max_seconds` (a provider that never answers) is stopped too, so it cannot sit on "running" for ever."""
    began = asyncio.get_event_loop().time()
    while not task.done():
        await asyncio.sleep(POLL_SECONDS)
        if max_seconds and asyncio.get_event_loop().time() - began > max_seconds and not task.done():
            flag.append("timeout")
            task.cancel()
            return
        if not honour_cancel:
            continue
        try:
            doc = await pipeline_runs.find_one({"id": run_id}, {"cancel_requested": 1})
        except Exception as exc:  # noqa: BLE001 - one missed look is fine, the next one comes in a moment
            logger.warning("Could not look for a cancel on run %s: %s", run_id, exc)
            continue
        if doc and doc.get("cancel_requested") and not task.done():
            flag.append("cancel")
            task.cancel()
            return


class RunTimedOut(Exception):
    """Raised inside `start` when a run went on longer than its limit."""

    def __init__(self, seconds: Optional[float] = None) -> None:
        super().__init__("The run took too long.")
        self.seconds = seconds


#: How long a run that is one long call may take before it is stopped as failed.
MAX_SECONDS_BY_KIND = {"image": 15 * 60, "audio": 40 * 60, "campaign": 15 * 60}
#: The longest any other run may take (a batch of several days, a bulk action). A run past it is stopped and marked failed, so it
#: cannot hold one of the workspace's slots for ever.
MAX_SECONDS_DEFAULT = 60 * 60


def start(doc: dict, work: Callable[[dict], Awaitable[Optional[dict]]], *, interrupt_on_cancel: bool = False) -> None:
    """Run `work(doc)` in the background and record how it ends. `work` returns an optional result dict.

    With `interrupt_on_cancel` a cancel stops the work itself (the calls it has open are closed); without it the work stops at
    its next checkpoint. Use it for one long call that has nothing to wait at, and leave it off for work that must finish its
    current unit (a campaign day) so what it already made stays whole."""

    async def beat() -> None:
        while True:
            try:
                await _set(doc["id"], heartbeat_at=_now())
            except Exception as exc:  # noqa: BLE001 - one failed write must not make a live run look dead
                logger.warning("Could not record a sign of life for run %s: %s", doc["id"], exc)
            await asyncio.sleep(HEARTBEAT_SECONDS)

    async def runner() -> None:
        heartbeat = asyncio.create_task(beat())
        watcher = None
        try:
            await _set(doc["id"], status="running", started_at=_now(), heartbeat_at=_now())
            # Every run is watched for a time limit; a run that honours a cancel at once (interrupt_on_cancel) is also stopped on request.
            work_task = asyncio.ensure_future(work(doc))
            cancelled_by_member: list = []
            # a kind with no limit of its own (video, text, odette) still has the general one, so no run can hold a slot for ever
            limit = (MAX_SECONDS_BY_KIND.get(doc.get("kind", "")) or MAX_SECONDS_DEFAULT) if interrupt_on_cancel else MAX_SECONDS_DEFAULT
            watcher = asyncio.create_task(_watch_cancel(doc["id"], work_task, cancelled_by_member, limit, honour_cancel=interrupt_on_cancel))
            try:
                result = await work_task
            except asyncio.CancelledError:
                if cancelled_by_member == ["cancel"]:
                    raise RunCancelled()
                if cancelled_by_member == ["timeout"]:
                    raise RunTimedOut(limit)
                raise
            if result and result.get("href"):
                doc["href"] = result["href"]
                await _set(doc["id"], href=doc["href"])
            await _finish(doc, "done", result=result)
        except RunCancelled:
            await _finish(doc, "cancelled")
        except RunTimedOut as timed_out:
            minutes = int((timed_out.seconds or MAX_SECONDS_DEFAULT) // 60) or 1
            await _finish(doc, "failed", error=f"This took longer than {minutes} minutes, so it was stopped. Try again.")
        except Exception as exc:  # noqa: BLE001
            logger.error("Run %s (%s) failed: %s", doc["id"], doc["kind"], exc, exc_info=True)
            await _finish(doc, "failed", error=_plain(exc))
        finally:
            heartbeat.cancel()
            if watcher is not None:
                watcher.cancel()
            _tasks.pop(doc["id"], None)

    _tasks[doc["id"]] = asyncio.create_task(runner())


async def fail_interrupted() -> int:
    """Close runs whose server died: still queued, running or paused, but with no sign of life for STALE_AFTER. A run that is
    going on another server keeps beating, so it is left alone. Called on startup and every few minutes."""
    now = _now()
    cutoff = now - STALE_AFTER
    # Runs the member cancelled whose server died before they stopped are closed as cancelled (not failed, and never restarted).
    mine = list(_tasks)  # runs this server is working on are never taken for dead
    async for stale in pipeline_runs.find(
        {
            "id": {"$nin": mine}, "status": {"$in": list(ACTIVE)}, "cancel_requested": True, "external_worker": {"$ne": True},
            "$or": [{"interrupted": True}, {"$and": [
                {"$or": [{"heartbeat_at": {"$lt": cutoff}}, {"heartbeat_at": {"$exists": False}}, {"heartbeat_at": None}]},
                {"created_at": {"$lt": cutoff}},
            ]}],
        },
        {"_id": 0},
    ).limit(200):
        await _finish(stale, "cancelled")
    res = await pipeline_runs.update_many(
        {
            "id": {"$nin": mine}, "status": {"$in": list(ACTIVE)}, "external_worker": {"$ne": True},
            "$or": [{"interrupted": True}, {"$and": [
                {"$or": [{"heartbeat_at": {"$lt": cutoff}}, {"heartbeat_at": {"$exists": False}}, {"heartbeat_at": None}]},
                {"created_at": {"$lt": cutoff}},
            ]}],
        },
        {"$set": {
            "status": "failed",
            "error": "This was interrupted when the server restarted. Nothing was lost that you had already saved. Start it again (an upload needs the file sent again).",
            "finished_at": now, "updated_at": now,
        }, "$unset": {"active_key": ""}},
    )
    return res.modified_count


async def mark_interrupted_for_shutdown() -> int:
    """Called as the server shuts down. The runs this server is working on are stopped and marked, so a restart finds them at
    once: safe-to-repeat jobs are started again, the rest are closed as failed with a plain message. Without this they would
    look alive for another two minutes."""
    ids = list(_tasks)
    if not ids:
        return 0
    await pipeline_runs.update_many({"id": {"$in": ids}, "status": {"$in": list(ACTIVE)}}, {"$set": {"interrupted": True}})
    for task in list(_tasks.values()):
        task.cancel()
    return len(ids)
