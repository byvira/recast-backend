"""Reminds a member about work they paused and left.

A run paused for a day gets a note in the Activity Log asking whether to resume or cancel it. Further notes follow at 3 and 7
days, then it stops asking. Work paused in this process also ends if the server restarts (see pipeline_runs.fail_interrupted),
so these notes cover the common case of a pause that is simply forgotten.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.core.scheduler_lock import distributed_job_lock
from app.db.mongo import pipeline_runs
from app.shared.activity import record_system

logger = logging.getLogger(__name__)

#: How long a run must have been paused before each reminder, in order.
REMINDER_AFTER = (timedelta(hours=24), timedelta(days=3), timedelta(days=7))


def _as_utc(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


def reminder_due(run: dict, now: datetime) -> bool:
    sent = int(run.get("reminders_sent") or 0)
    paused_at = _as_utc(run.get("paused_at"))
    if sent >= len(REMINDER_AFTER) or not paused_at:
        return False
    return now - paused_at >= REMINDER_AFTER[sent]


@distributed_job_lock("run_reminders", ttl_seconds=240)
async def remind_about_paused_runs() -> int:
    """Called every 10 minutes. Returns how many reminders were written."""
    from app.shared import pipeline_runs as run_layer

    # The same pass closes runs whose server died, so nothing looks busy for ever.
    await run_layer.fail_interrupted()
    now = datetime.now(timezone.utc)
    sent = 0
    async for run in pipeline_runs.find({"status": "paused"}, {"_id": 0}).limit(500):
        if not reminder_due(run, now):
            continue
        number = int(run.get("reminders_sent") or 0) + 1
        try:
            await record_system(
                workspace_id=run["workspace_id"], key=f"run-reminder:{run['id']}:{number}", actor_name="Recast",
                actor_user_id=run.get("created_by"), category="content_generated",
                title=f"{run['title']} is still paused",
                description="You paused this a while ago. Open it to resume or cancel it.",
                status="warning", target_id=run["id"], target_type="Run", href=run.get("href"),
            )
            await pipeline_runs.update_one(
                {"id": run["id"]}, {"$set": {"reminders_sent": number, "last_reminded_at": now}},
            )
            sent += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not write the reminder for run %s: %s", run.get("id"), exc)
    return sent
