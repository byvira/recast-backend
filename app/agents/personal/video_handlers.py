"""Remy's handlers for video events. The text graph only reads content events, so these are separate and small: a video that failed
its checks, or a recipe that stopped, becomes a plain note to the member in their Active lane. Each is raised once per job or recipe
run, even if the event is delivered again."""

from __future__ import annotations

import logging
from typing import Any

from app.agents.personal.signals import emit_signal
from app.db.mongo import personal_signals

logger = logging.getLogger(__name__)

HANDLED = {"video.qc_failed", "video.automation_paused"}


def _message(event_type: str, p: dict[str, Any]) -> tuple[str, str, str]:
    """(member message, note for Odette, evidence collection) for one event."""
    reason = (p.get("detail") or "").strip() or "the checks did not pass"
    if event_type == "video.qc_failed":
        return (f"Your video did not pass its checks: {reason} Nothing was published. Open it to see what to change, then make it again.",
                f"video render failed its checks: {reason[:120]}", "video_jobs")
    title = p.get("title") or "A recipe"
    return (f"{title} stopped on its own: {reason}. Nothing was published. Open the decision to fix it or turn the recipe back on.",
            f"automatic recipe stopped: {reason[:120]}", "video_recipes")


async def handle(event: dict[str, Any]) -> bool:
    """Raise Remy's note for one video event. True when a note was made, False when it was already made or the event is not one of
    these. Never raises."""
    et = event.get("event_type")
    if et not in HANDLED:
        return False
    try:
        p = event.get("payload") or {}
        ws, user = event.get("workspace_id"), event.get("actor_user_id")
        ref_id = p.get("job_id") or p.get("recipe_id") or ""
        if not ws or not user or user == "system" or not ref_id:
            return False
        key = f"{ref_id}:{event.get('event_id', '')}" if et == "video.automation_paused" else ref_id
        if await personal_signals.find_one({"workspace_id": ws, "user_id": user, "evidence_refs.id": key}):
            return False
        message, note, collection = _message(et, p)
        await emit_signal(
            workspace_id=ws, user_id=user, pipeline_type="video", signal_type="video_check_failed", severity="medium",
            metric={"name": "video_checks_failed", "value": 1.0, "baseline": 0.0, "threshold": 0.0}, window={"kind": "rolling", "n": 1},
            evidence_refs=[{"collection": collection, "id": key}], member_message=message, supervisor_note=note,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Remy video handler failed for %s: %s", event.get("event_id"), exc)
        return False
