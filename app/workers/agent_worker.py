"""The agent worker — a SECOND process, separate from the API.

Run it alongside uvicorn:

    uvicorn app.main:app                       # API process
    arq app.workers.agent_worker.WorkerSettings   # this process

It shares only MongoDB + Redis with the API. Nothing here is on any request's
latency path.

Stage 1 responsibilities:
  * consume ``content.created`` / ``content.updated`` from the ``recast:events``
    Redis Stream (consumer group ``personal``) and run the personal-assistant
    graph for each.
  * recover in-flight entries a crashed worker left un-ACKed (``XAUTOCLAIM``).
  * a 1-minute cron guard that restarts the consume loop if it ever dies.

(Stage 2 will add the supervisor cron jobs to this same worker.)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from arq import cron
from arq.connections import RedisSettings
from redis.exceptions import ResponseError

from app.agents.personal.graph import run_personal_graph
from app.core.config import settings
from app.db.redis import get_redis
from app.shared.events import EVENTS_STREAM

logger = logging.getLogger(__name__)

GROUP = "personal"
CONSUMER = f"personal-{os.getpid()}"
BATCH = 50
BLOCK_MS = 5000
RECLAIM_IDLE_MS = 60_000        # provisional: treat an entry idle this long as abandoned
MAX_DELIVERIES = 5             # provisional: dead-letter after this many failed attempts

_HANDLED_TYPES = {"content.created", "content.updated"}
_delivery_counts: dict[str, int] = {}


# ─────────────────────────────────────────────────────────────────────────────
# stream plumbing
# ─────────────────────────────────────────────────────────────────────────────

async def _ensure_group(r) -> None:
    try:
        await r.xgroup_create(EVENTS_STREAM, GROUP, id="0", mkstream=True)
        logger.info("created consumer group %r on %s", GROUP, EVENTS_STREAM)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def _handle_entry(r, entry_id: str, fields: dict) -> None:
    try:
        raw = fields.get("data")
        event = json.loads(raw) if raw else None
        if event and event.get("event_type") in _HANDLED_TYPES:
            await run_personal_graph(event)
        await r.xack(EVENTS_STREAM, GROUP, entry_id)
        _delivery_counts.pop(entry_id, None)
    except Exception as exc:  # noqa: BLE001
        cnt = _delivery_counts.get(entry_id, 0) + 1
        _delivery_counts[entry_id] = cnt
        if cnt >= MAX_DELIVERIES:
            logger.error(
                "personal consumer: DEAD-LETTER entry %s after %d attempts: %s",
                entry_id, cnt, exc,
            )
            await r.xack(EVENTS_STREAM, GROUP, entry_id)
            _delivery_counts.pop(entry_id, None)
        else:
            logger.error(
                "personal consumer: entry %s failed (attempt %d/%d), left un-ACKed: %s",
                entry_id, cnt, MAX_DELIVERIES, exc,
            )


async def _reclaim_stale(r) -> int:
    """XAUTOCLAIM anything left pending by a dead worker and re-process it."""
    reclaimed = 0
    cursor = "0-0"
    while True:
        try:
            res = await r.xautoclaim(
                EVENTS_STREAM, GROUP, CONSUMER, min_idle_time=RECLAIM_IDLE_MS,
                start_id=cursor, count=BATCH,
            )
        except ResponseError as exc:
            logger.warning("xautoclaim failed (continuing): %s", exc)
            break
        # redis-py 5.x: (next_cursor, [(id, fields), ...], [deleted_ids])
        cursor, claimed = res[0], res[1]
        for entry_id, fields in claimed:
            await _handle_entry(r, entry_id, fields)
            reclaimed += 1
        if cursor in ("0-0", b"0-0") or not claimed:
            break
    if reclaimed:
        logger.info("personal consumer: reclaimed %d stale entries on startup", reclaimed)
    return reclaimed


async def drain_personal_once(max_batches: int = 1) -> int:
    """Read + process up to ``max_batches`` batches, non-blocking. Used by the
    Stage-1 smoke test to exercise the exact consumer path without running arq."""
    r = await get_redis()
    await _ensure_group(r)
    processed = 0
    for _ in range(max_batches):
        resp = await r.xreadgroup(GROUP, CONSUMER, {EVENTS_STREAM: ">"}, count=BATCH, block=100)
        if not resp:
            break
        for _stream, entries in resp:
            for entry_id, fields in entries:
                await _handle_entry(r, entry_id, fields)
                processed += 1
    return processed


# ─────────────────────────────────────────────────────────────────────────────
# long-running consume loop + cron guard
# ─────────────────────────────────────────────────────────────────────────────

async def _consume_loop(stop: asyncio.Event) -> None:
    r = await get_redis()
    await _ensure_group(r)
    await _reclaim_stale(r)
    logger.info("personal consume loop started (consumer=%s)", CONSUMER)
    while not stop.is_set():
        try:
            resp = await r.xreadgroup(
                GROUP, CONSUMER, {EVENTS_STREAM: ">"}, count=BATCH, block=BLOCK_MS
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("personal consumer: xreadgroup error: %s", exc)
            await asyncio.sleep(1.0)
            continue
        if not resp:
            continue
        for _stream, entries in resp:
            for entry_id, fields in entries:
                await _handle_entry(r, entry_id, fields)
    logger.info("personal consume loop stopped")


async def personal_consumer_guard(ctx: dict) -> None:
    """1-minute cron: (re)start the consume loop if it isn't running."""
    task: asyncio.Task | None = ctx.get("personal_consumer_task")
    if task is not None and not task.done():
        return
    if task is not None and task.done() and not task.cancelled():
        logger.warning("personal consumer had exited (exc=%r) — restarting", task.exception())
    stop: asyncio.Event = ctx.setdefault("personal_consumer_stop", asyncio.Event())
    stop.clear()
    ctx["personal_consumer_task"] = asyncio.create_task(_consume_loop(stop))
    logger.info("personal consumer (re)started by guard")


# ─────────────────────────────────────────────────────────────────────────────
# enqueueable job — manual replay / tests
# ─────────────────────────────────────────────────────────────────────────────

async def run_personal_graph_job(ctx: dict, event: dict) -> dict:
    return await run_personal_graph(event)


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2 — workspace supervisor cron bodies (imported from the agent package)
# ─────────────────────────────────────────────────────────────────────────────

from app.agents.supervisor.ticks import (  # noqa: E402
    personal_volume_sweep,
    run_supervisor_now,
    supervisor_reason_tick,
    supervisor_rules_tick,
)


# ─────────────────────────────────────────────────────────────────────────────
# arq worker settings
# ─────────────────────────────────────────────────────────────────────────────

async def _on_startup(ctx: dict) -> None:
    logger.info("agent worker starting (pid=%s)", os.getpid())
    from app.core.tracing import init_tracing
    init_tracing()
    await personal_consumer_guard(ctx)


async def _on_shutdown(ctx: dict) -> None:
    stop: asyncio.Event | None = ctx.get("personal_consumer_stop")
    if stop:
        stop.set()
    task: asyncio.Task | None = ctx.get("personal_consumer_task")
    if task:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    logger.info("agent worker stopped")


_EVERY_5_MIN = {m for m in range(60) if m % 5 == 0}

class WorkerSettings:
    functions = [run_personal_graph_job, run_supervisor_now]
    cron_jobs = [
        # Layer 1 — keep the personal-assistant stream consumer alive
        cron(personal_consumer_guard, run_at_startup=True),
        # Layer 2 — deterministic hard-limit flags, every minute
        cron(supervisor_rules_tick, name="supervisor_rules_tick"),
        # Layer 2 — debounced LLM reasoning pass, every 5 minutes
        cron(supervisor_reason_tick, name="supervisor_reason_tick", minute=_EVERY_5_MIN),
        # volume_drop sweep (moved off the per-piece personal graph), every 6h
        cron(personal_volume_sweep, name="personal_volume_sweep",
             hour={0, 6, 12, 18}, minute=7),
    ]
    on_startup = _on_startup
    on_shutdown = _on_shutdown
    redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)
    max_tries = 3
    job_timeout = 300
    keep_result = 3600
