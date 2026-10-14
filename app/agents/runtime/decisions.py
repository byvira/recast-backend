"""The decision log: what an agent did on its own, why, and on what evidence. Staff and Remy and Odette read it; a member
sees the plain version in the activity experience."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from app.db.mongo import get_db

logger = logging.getLogger(__name__)


async def record_decision(
    *, workspace_id: str, agent: str, step: str, summary: str, evidence: Optional[dict[str, Any]] = None,
    run_id: Optional[str] = None, user_id: Optional[str] = None, outcome: str = "ok",
) -> None:
    """Write one decision entry. Never raises: a missing log line must not fail the work it describes."""
    try:
        await get_db()["agent_decisions"].insert_one({
            "id": str(uuid4()), "workspace_id": workspace_id, "agent": agent, "step": step, "summary": summary[:300],
            "evidence": evidence or {}, "run_id": run_id, "user_id": user_id, "outcome": outcome,
            "created_at": datetime.now(timezone.utc),
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not record the %s decision for %s: %s", agent, workspace_id, exc)
