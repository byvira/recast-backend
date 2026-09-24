"""Pipeline chaining, as a suggestion — never an automatic run.

When a member's manual text run finishes and the workspace has connected
channels that run didn't cover, Remy offers one next step in the member's
Active lane: "repurpose this for Threads and Facebook too". Accepting opens
the Library (Quick Recast) — it doesn't spend an LLM run on its own.

Which channels count is derived from the platform registry (the text
``Platform`` enum) matched against the workspace's live connections, so a
platform added to the registry later is covered with no change here. Only
channels whose connection isn't broken are suggested.

Feedback: a member who has dismissed ``DISMISS_LIMIT`` of these in the last
``DISMISS_WINDOW`` stops getting them; and there's never more than one open
next-step suggestion per member at a time.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.db.mongo import activity_entries, workspace_connections
from app.models.text import Platform
from app.shared.activity import actors
from app.shared.activity.store import LANE_ACTIVE, upsert_entry

logger = logging.getLogger(__name__)

SOURCE_KIND = "next_step"
DISMISS_LIMIT = 3
DISMISS_WINDOW = timedelta(days=30)


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


async def suggest_after_run(event: dict) -> None:
    """Called by the Activity projector for every ``pipeline.run_completed``.
    Never raises."""
    try:
        p = event.get("payload") or {}
        if p.get("trigger") != "manual" or not p.get("pieces") or not p.get("session_id"):
            return
        workspace_id, user_id = event.get("workspace_id"), event.get("actor_user_id")
        if not workspace_id or not user_id:
            return

        base = {"workspace_id": workspace_id, "member_user_id": user_id, "source.kind": SOURCE_KIND}
        if await activity_entries.find_one({**base, "lane": LANE_ACTIVE}):
            return
        dismissed = await activity_entries.count_documents({
            **base, "decision.outcome": "dismissed",
            "decided_at": {"$gte": datetime.now(timezone.utc) - DISMISS_WINDOW},
        })
        if dismissed >= DISMISS_LIMIT:
            return

        connected = {
            c["platform"].lower()
            async for c in workspace_connections.find(
                {"workspace_id": workspace_id, "is_active": True, "health.state": {"$ne": "broken"}},
                {"platform": 1},
            )
        }
        covered = {str(x).lower() for x in (p.get("platforms") or [])}
        missing = [
            member.value for member in Platform
            if member.value.lower() in connected and member.value.lower() not in covered
        ]
        if not missing:
            return

        covered_label = _join(list(p.get("platforms") or [])) or "some channels"
        await upsert_entry({
            "_id": f"{SOURCE_KIND}:{p['session_id']}",
            "workspace_id": workspace_id,
            "lane": LANE_ACTIVE,
            "visibility": "member",
            "member_user_id": user_id,
            "source": {"kind": SOURCE_KIND, "id": p["session_id"], "type": "repurpose"},
            "actor": dict(actors.REMY),
            "category": "recommendation",
            "title": f"Repurpose this for {_join(missing)} too?",
            "description": (
                f"Your run on \"{p.get('title') or 'your latest topic'}\" covered {covered_label}. "
                f"{_join(missing)} {'is' if len(missing) == 1 else 'are'} connected as well — "
                f"a version for {'it' if len(missing) == 1 else 'them'} is one Quick Recast away."
            ),
            "target_id": p["session_id"],
            "target_type": "Pipeline Execution",
            "href": "/dashboard/library",
            "status": "warning",
            "metadata": {"suggestedChannels": ", ".join(missing)},
            "occurred_at": datetime.now(timezone.utc),
        })
    except Exception as exc:  # noqa: BLE001
        logger.error("next-step suggestion failed for %s: %s", event.get("_id"), exc)
