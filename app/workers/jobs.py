"""One list of background agent/autonomy jobs, runnable by either runner.

Deployment today runs these on the web process's own ``AsyncIOScheduler``
(``app.workers.inprocess`` — no paid Render worker; see DEPLOY.md). The arq
entrypoint (``app.workers.agent_worker.WorkerSettings``) builds its cron list
from the same ``JOBS``, so moving them to a separate process stays a deploy
config change. Every job takes arq's ``ctx`` dict and guards itself with
``distributed_job_lock`` (or equivalent), so running both at once is safe.

This is the only place background jobs are registered — including the
publishing/campaign/token/analytics jobs that used to be added directly in
``app.main``. Add new jobs here, not to either runner.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from app.agents.supervisor.ticks import (
    personal_volume_sweep,
    supervisor_reason_tick,
    supervisor_rules_tick,
)
from app.agents.feedback.cadence import cadence_monitor
from app.agents.feedback.sweep import performance_feedback_sweep
from app.agents.feedback.trust import autonomy_trust_refresh
from app.pipelines.analytics.checkpoints import capture_metric_checkpoints
from app.pipelines.analytics.scheduler import refresh_analytics
from app.workers.campaign_scheduler import run_due_campaign_batches
from app.workers.scheduled_posts import process_scheduled_posts
from app.workers.token_refresh import refresh_expiring_tokens


@dataclass(frozen=True)
class Job:
    name: str
    func: Callable[[dict], Awaitable[object]]
    #: Cron fields — ``None`` means "every" (arq's and APScheduler's default).
    minute: Optional[frozenset[int]] = None
    hour: Optional[frozenset[int]] = None


def _no_ctx(func: Callable[[], Awaitable[object]]) -> Callable[[dict], Awaitable[object]]:
    """Adapt a job that takes no arguments to the runners' ``(ctx)`` call."""
    async def _job(ctx: dict) -> object:
        return await func()
    _job.__name__ = func.__name__
    _job.__qualname__ = func.__qualname__
    return _job


def _every(step: int, *, offset: int = 0) -> frozenset[int]:
    return frozenset(m for m in range(60) if m % step == offset % step)


JOBS: list[Job] = [
    # Publishing & campaigns — every minute (each job holds its own lock).
    Job("scheduled_posts", _no_ctx(process_scheduled_posts)),
    Job("campaign_batches", _no_ctx(run_due_campaign_batches)),
    # Token renewal — hourly; it only renews connections that are due.
    Job("token_refresh", _no_ctx(refresh_expiring_tokens), minute=frozenset({11})),
    # Latest post/account metrics — every 6h.
    Job("analytics_refresh", _no_ctx(refresh_analytics),
        minute=frozenset({17}), hour=frozenset({0, 6, 12, 18})),
    # Layer 2 — deterministic hard-limit flags, every minute.
    Job("supervisor_rules_tick", supervisor_rules_tick),
    # Layer 2 — debounced LLM reasoning pass, every 5 minutes.
    Job("supervisor_reason_tick", supervisor_reason_tick, minute=_every(5)),
    # Layer 1 — volume_drop sweep, every 6h at :07.
    Job("personal_volume_sweep", personal_volume_sweep,
        minute=frozenset({7}), hour=frozenset({0, 6, 12, 18})),
    # Engagement at fixed ages (1h/24h/72h/7d) — every 15 min, offset from
    # the :00/:05 ticks above.
    Job("capture_metric_checkpoints", capture_metric_checkpoints, minute=_every(15, offset=3)),
    # Feedback loop — engagement patterns into Remy/Odette, daily at 02:23 UTC.
    Job("performance_feedback_sweep", performance_feedback_sweep,
        minute=frozenset({23}), hour=frozenset({2})),
    # Trust score (shadow mode) — daily at 02:41 UTC.
    Job("autonomy_trust_refresh", autonomy_trust_refresh,
        minute=frozenset({41}), hour=frozenset({2})),
    # Campaign cadence — stalled / behind checks, hourly at :37.
    Job("cadence_monitor", cadence_monitor, minute=frozenset({37})),
]


def _csv(values: Optional[frozenset[int]]) -> Optional[str]:
    return ",".join(str(v) for v in sorted(values)) if values else None


def schedule_inprocess(scheduler, ctx: dict) -> None:
    """Register every job on an APScheduler ``AsyncIOScheduler``."""
    for job in JOBS:
        fields = {k: v for k, v in (("minute", _csv(job.minute)), ("hour", _csv(job.hour))) if v}
        if not fields:
            fields = {"minute": "*"}
        scheduler.add_job(job.func, "cron", args=[ctx], id=job.name, **fields)


def arq_cron_jobs() -> list:
    """The same jobs as arq ``cron`` entries."""
    from arq import cron

    return [
        cron(job.func, name=job.name,
             minute=set(job.minute) if job.minute else None,
             hour=set(job.hour) if job.hour else None)
        for job in JOBS
    ]
