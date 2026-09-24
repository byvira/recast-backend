"""Live-run registry — what the Control Tower's "Live Processing" shows.

One Redis hash per workspace (``recast:runs:{workspace_id}``), one field per
in-flight run. Redis (not process memory) so a second web instance or a
worker process sees the same runs. Every write refreshes the key's TTL, and
reads drop entries older than ``STALE_AFTER`` — a process that dies mid-run
can't leave a ghost task spinning forever.

Progress is always derived from real signals:
* ``steps_done / steps_total`` when the run reports discrete steps
  (pipeline stages, campaign days);
* otherwise elapsed time against this workspace's own median run duration
  (from ``pipeline.run_completed``), capped below 100 until the run ends.
ETA uses the same median; with no history there is no ETA rather than a
made-up one.
"""

from __future__ import annotations

import json
import logging
import statistics
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from app.db.mongo import workspace_events
from app.db.redis import get_redis

logger = logging.getLogger(__name__)

KEY_PREFIX = "recast:runs:"
KEY_TTL_SECONDS = 30 * 60
STALE_AFTER_SECONDS = 2 * 60 * 60
#: Never show a still-running job as done.
MAX_RUNNING_PROGRESS = 95

_BASELINE_TTL_SECONDS = 300
_BASELINE_SAMPLE = 20
_baseline_cache: dict[str, tuple[float, Optional[float]]] = {}


def brand_label(brand: Optional[dict]) -> str:
    """Display name for a brand profile — same precedence as the Drafts
    list (app.pipelines.text.storage)."""
    identity = (brand or {}).get("identity") or {}
    return (
        identity.get("name")
        or identity.get("productName")
        or identity.get("company_name")
        or identity.get("companyName")
        or ""
    )


def _key(workspace_id: str) -> str:
    return f"{KEY_PREFIX}{workspace_id}"


async def start_run(
    *,
    workspace_id: str,
    run_id: str,
    kind: str,
    title: str,
    project: str = "",
    steps_total: Optional[int] = None,
) -> None:
    await _write(workspace_id, run_id, {
        "id": run_id,
        "kind": kind,
        "title": title,
        "project": project,
        "stage": "",
        "steps_done": 0,
        "steps_total": steps_total,
        "started_at": time.time(),
    })


async def update_run(
    workspace_id: str,
    run_id: str,
    *,
    stage: Optional[str] = None,
    steps_done: Optional[int] = None,
) -> None:
    try:
        r = await get_redis()
        raw = await r.hget(_key(workspace_id), run_id)
        if not raw:
            return
        run = json.loads(raw)
        if stage is not None:
            run["stage"] = stage
        if steps_done is not None:
            run["steps_done"] = steps_done
        await _write(workspace_id, run_id, run)
    except Exception as exc:  # noqa: BLE001
        logger.warning("run registry update failed for %s: %s", run_id, exc)


async def end_run(workspace_id: str, run_id: str) -> None:
    try:
        r = await get_redis()
        await r.hdel(_key(workspace_id), run_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("run registry end failed for %s: %s", run_id, exc)


@asynccontextmanager
async def tracked_run(
    *,
    workspace_id: str,
    run_id: str,
    kind: str,
    title: str,
    project: str = "",
    steps_total: Optional[int] = None,
):
    """Register a run for its whole lifetime — removed however it ends
    (success, error or cancellation)."""
    await start_run(
        workspace_id=workspace_id, run_id=run_id, kind=kind,
        title=title, project=project, steps_total=steps_total,
    )
    try:
        yield
    finally:
        await end_run(workspace_id, run_id)


def run_label(text: str, fallback: str = "Text pipeline") -> str:
    first = (text or "").strip().splitlines()[0] if (text or "").strip() else fallback
    return first if len(first) <= 60 else first[:57].rstrip() + "…"


async def _write(workspace_id: str, run_id: str, run: dict) -> None:
    try:
        r = await get_redis()
        key = _key(workspace_id)
        await r.hset(key, run_id, json.dumps(run))
        await r.expire(key, KEY_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.warning("run registry write failed for %s: %s", run_id, exc)


async def median_run_seconds(workspace_id: str) -> Optional[float]:
    """Median of this workspace's recent text-run durations, or None with
    fewer than 3 runs on record (too little to predict from)."""
    hit = _baseline_cache.get(workspace_id)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    durations: list[float] = []
    async for ev in workspace_events.find(
        {"workspace_id": workspace_id, "event_type": "pipeline.run_completed"},
        {"payload.duration_ms": 1},
    ).sort("occurred_at", -1).limit(_BASELINE_SAMPLE):
        ms = (ev.get("payload") or {}).get("duration_ms") or 0
        if ms > 0:
            durations.append(ms / 1000)
    value = statistics.median(durations) if len(durations) >= 3 else None
    _baseline_cache[workspace_id] = (time.monotonic() + _BASELINE_TTL_SECONDS, value)
    return value


def _format_eta(seconds: float) -> str:
    if seconds < 60:
        return "<1m"
    return f"{round(seconds / 60)}m"


async def list_runs(workspace_id: str) -> list[dict]:
    try:
        r = await get_redis()
        raw = await r.hgetall(_key(workspace_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("run registry read failed for %s: %s", workspace_id, exc)
        return []

    now = time.time()
    baseline = await median_run_seconds(workspace_id) if raw else None
    runs: list[dict] = []
    stale: list[str] = []
    for run_id, value in raw.items():
        try:
            run = json.loads(value)
        except ValueError:
            stale.append(run_id)
            continue
        elapsed = now - float(run.get("started_at") or now)
        if elapsed > STALE_AFTER_SECONDS:
            stale.append(run_id)
            continue

        total = run.get("steps_total")
        done = int(run.get("steps_done") or 0)
        if total:
            progress = round(100 * done / total)
            eta_seconds = (elapsed / done) * (total - done) if done else (
                max(baseline - elapsed, 0) if baseline else None
            )
        elif baseline:
            progress = round(100 * elapsed / baseline)
            eta_seconds = max(baseline - elapsed, 0)
        else:
            progress = 0
            eta_seconds = None

        runs.append({
            "id": run["id"],
            "kind": run.get("kind", "text"),
            "title": run.get("title", ""),
            "project": run.get("project", ""),
            "stage": run.get("stage", ""),
            "progress": max(0, min(progress, MAX_RUNNING_PROGRESS)),
            "eta": _format_eta(eta_seconds) if eta_seconds is not None else "",
            "startedAt": datetime.fromtimestamp(float(run["started_at"]), timezone.utc).isoformat(),
        })

    if stale:
        try:
            await r.hdel(_key(workspace_id), *stale)
        except Exception:  # noqa: BLE001
            pass
    runs.sort(key=lambda x: x["startedAt"])
    return runs
