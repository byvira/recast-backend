"""Makes a render appear in the product's existing run layer.

A render is done by the worker on another machine, but members see it where they see every other long piece of work: the
activity dock, Control Tower, and a notice in the Activity Log when it ends. This module keeps one `pipeline_runs` row in step
with a render job. The run is marked `external_worker`, so the layer that closes runs whose server died leaves it alone: the
job queue (its leases and reconciler) is what decides whether a render is alive.

Nothing here may break job handling, so every call is best effort and logs instead of raising, except opening the run, which
refuses the render with a plain message when the workspace already has the most jobs it may run.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from app.db.mongo import pipeline_runs
from app.shared import pipeline_runs as run_layer

logger = logging.getLogger(__name__)

KIND = "video"
TOTAL_STEPS = 100


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def open_run(*, workspace_id: str, user_id: str, title: str, project_id: Optional[str], job_id: str, href: Optional[str]) -> dict:
    """Create the run for a render about to be queued. Raises HTTPException 429 over the workspace's limit and
    run_layer.RunAlreadyActive when this project is already rendering."""
    return await run_layer.create_run(
        workspace_id=workspace_id, user_id=user_id, kind=KIND, title=title or "Video render", steps_total=TOTAL_STEPS, href=href,
        ref={"project_id": project_id, "job_id": job_id}, active_key=f"video:{project_id or job_id}",
        extra={"external_worker": True, "stage": "Waiting for a worker"},
    )


async def _guard(label: str, run_id: Optional[str], work) -> Any:
    if not run_id:
        return None
    try:
        return await work()
    except Exception as exc:  # noqa: BLE001 - the render must carry on whatever the run layer does
        logger.warning("Could not update run %s (%s): %s", run_id, label, exc)
        return None


async def started(run_id: Optional[str]) -> None:
    async def work():
        now = _now()
        await run_layer._set(run_id, status="running", started_at=now, heartbeat_at=now, stage="Starting", steps_done=0)
    await _guard("started", run_id, work)


async def waiting(run_id: Optional[str], stage: str) -> None:
    async def work():
        await run_layer._set(run_id, status="queued", stage=stage, heartbeat_at=_now())
    await _guard("waiting", run_id, work)


async def progress(run_id: Optional[str], stage: str, fraction: Optional[float]) -> bool:
    """Record what the render is doing. Returns True when the member asked to cancel it from anywhere in the app."""
    async def work() -> bool:
        fields: dict[str, Any] = {"heartbeat_at": _now(), "pause_requested": False}
        if stage:
            fields["stage"] = stage
        if fraction is not None:
            fields["steps_done"] = max(0, min(TOTAL_STEPS - 1, int(fraction * TOTAL_STEPS)))
        await run_layer._set(run_id, **fields)
        doc = await pipeline_runs.find_one({"id": run_id}, {"cancel_requested": 1})
        return bool(doc and doc.get("cancel_requested"))
    return bool(await _guard("progress", run_id, work))


async def cancel_requested(run_id: Optional[str]) -> bool:
    async def work() -> bool:
        doc = await pipeline_runs.find_one({"id": run_id}, {"cancel_requested": 1})
        return bool(doc and doc.get("cancel_requested"))
    return bool(await _guard("cancel_requested", run_id, work))


async def _close(run_id: Optional[str], status: str, *, result: Optional[dict] = None, error: Optional[str] = None) -> None:
    async def work():
        doc = await pipeline_runs.find_one({"id": run_id}, {"_id": 0})
        if doc:
            await run_layer._finish(doc, status, result=result, error=error)
    await _guard(f"finish {status}", run_id, work)


async def finish_ok(run_id: Optional[str], result: dict) -> None:
    await _close(run_id, "done", result=result)


async def finish_failed(run_id: Optional[str], message: str) -> None:
    await _close(run_id, "failed", error=message)


async def finish_cancelled(run_id: Optional[str]) -> None:
    await _close(run_id, "cancelled")
