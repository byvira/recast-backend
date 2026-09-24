"""Backfill the Activity Log (``activity_entries``) from history that predates it.

The Activity Log is a projection that only fills as things happen. This
replays what's already stored into it, so the page isn't empty for work
done before it existed:

* ``workspace_events`` from the last 90 days (the same retention the log
  keeps) — pipeline runs, publishes, member/role/voice/plan changes
* Remy's ``personal_signals``, Odette's ``workspace_insights`` / ``workspace_flags``
  — open ones land in *Needs your decision*, decided ones in *Work history*

Safe to re-run: every row has a fixed id derived from its source, so a
second run updates the same rows instead of duplicating them. Next-step
("repurpose this?") suggestions are not created for historical runs — they
only make sense right after a run finishes.

Nothing is backfilled for events that were never recorded at all (e.g.
publish failures before the Activity Log existed only live in
``publish_incidents``, which has no actor/decision shape to project).

Usage:
    python -m scripts.backfill_activity              # dry run — counts only
    python -m scripts.backfill_activity --execute    # actually writes rows
    python -m scripts.backfill_activity --execute --workspace <id>
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.db.mongo import personal_signals, workspace_events, workspace_flags, workspace_insights
from app.shared.activity import (
    project_event,
    project_odette_flag,
    project_odette_insight,
    project_remy_signal,
)

WINDOW = timedelta(days=90)
#: Event types the projector turns into rows — everything else is skipped
#: without being read (content.created, assistant.signal, ...).
PROJECTED_EVENTS = [
    "pipeline.run_completed", "content.published", "member.added", "member.removed",
    "role.changed", "brand.voice_updated", "tier.changed",
]


def _scope(workspace_id: Optional[str]) -> dict:
    return {"workspace_id": workspace_id} if workspace_id else {}


async def backfill(*, execute: bool, workspace_id: Optional[str] = None) -> dict:
    since = datetime.now(timezone.utc) - WINDOW
    counts = {"events": 0, "remy_signals": 0, "odette_insights": 0, "odette_flags": 0}

    async for event in workspace_events.find({
        **_scope(workspace_id), "event_type": {"$in": PROJECTED_EVENTS},
        "occurred_at": {"$gte": since.isoformat()},
    }):
        counts["events"] += 1
        if execute:
            await project_event(event, suggest=False)

    for collection, key, project in (
        (personal_signals, "remy_signals", project_remy_signal),
        (workspace_insights, "odette_insights", project_odette_insight),
        (workspace_flags, "odette_flags", project_odette_flag),
    ):
        async for doc in collection.find({**_scope(workspace_id), "created_at": {"$gte": since}}):
            counts[key] += 1
            if execute:
                await project(doc)
    return counts


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--execute", action="store_true", help="write rows (default: dry run)")
    parser.add_argument("--workspace", help="limit to one workspace id")
    args = parser.parse_args()

    try:
        counts = await backfill(execute=args.execute, workspace_id=args.workspace)
    finally:
        # Close clients inside the loop — otherwise their cleanup runs after
        # asyncio.run() has closed it ("Event loop is closed").
        from app.db.mongo import get_client
        from app.db.redis import close_redis
        await close_redis()
        get_client().close()
    verb = "Projected" if args.execute else "Would project"
    for source, n in counts.items():
        print(f"{verb} {n} {source.replace('_', ' ')}")
    if not args.execute:
        print("\nDry run — nothing written. Re-run with --execute to backfill.")


if __name__ == "__main__":
    asyncio.run(main())
