"""One list of background agent/autonomy jobs, runnable by either runner.

Deployment today runs these on the web process's own ``AsyncIOScheduler``
(``app.workers.inprocess`` — no paid Render worker; see DEPLOY.md). The arq
entrypoint (``app.workers.agent_worker.WorkerSettings``) builds its cron list
from the same ``JOBS``, so moving them to a separate process stays a deploy
config change. Every job takes arq's ``ctx`` dict and guards itself with
``distributed_job_lock`` (or equivalent), so running both at once is safe.

The web-only jobs in ``app.main`` (scheduled publishing, campaign batches,
token refresh, analytics refresh) are request-adjacent and stay there.
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


@dataclass(frozen=True)
class Job:
    name: str
    func: Callable[[dict], Awaitable[object]]
    #: Cron fields — ``None`` means "every" (arq's and APScheduler's default).
    minute: Optional[frozenset[int]] = None
    hour: Optional[frozenset[int]] = None


def _every(step: int, *, offset: int = 0) -> frozenset[int]:
    return frozenset(m for m in range(60) if m % step == offset % step)


JOBS: list[Job] = [
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
