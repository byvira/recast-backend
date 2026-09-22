"""Runs the agent worker's jobs on the web service's own event loop, instead
of a separate arq worker process (see ``app.workers.agent_worker`` for that
deployment path and why it isn't used right now).

Wired into ``app.main``'s lifespan, reusing the same ``AsyncIOScheduler``
already running ``process_scheduled_posts`` etc. All four jobs are the exact
functions arq would have run — nothing here reimplements their logic.
"""

from __future__ import annotations

import logging

from app.agents.supervisor.ticks import (
    personal_volume_sweep,
    supervisor_reason_tick,
    supervisor_rules_tick,
)
from app.workers.agent_worker import personal_consumer_guard, stop_consumer

logger = logging.getLogger(__name__)

# Shared across calls so personal_consumer_guard can find the task it
# started last time and tell whether it's still alive.
_ctx: dict = {}


async def start(scheduler) -> None:
    """Call once during app startup, before ``scheduler.start()``."""
    await personal_consumer_guard(_ctx)  # start the stream consumer immediately
    scheduler.add_job(
        personal_consumer_guard, "interval", minutes=1, args=[_ctx],
        id="agent_consumer_guard",
    )
    scheduler.add_job(
        supervisor_rules_tick, "interval", minutes=1, args=[_ctx],
        id="supervisor_rules_tick",
    )
    scheduler.add_job(
        supervisor_reason_tick, "cron", minute="*/5", args=[_ctx],
        id="supervisor_reason_tick",
    )
    scheduler.add_job(
        personal_volume_sweep, "cron", hour="0,6,12,18", minute=7, args=[_ctx],
        id="personal_volume_sweep",
    )
    logger.info("in-process agent workers started")


async def stop() -> None:
    """Call once during app shutdown."""
    await stop_consumer(_ctx)
    logger.info("in-process agent workers stopped")
