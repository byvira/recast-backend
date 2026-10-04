"""One row for each output the guard blocked or rewrote, so Ops can see what was caught and why."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

EXCERPT = 280


def _fingerprint(workspace_id: Optional[str], piece_id: Optional[str], outcome: str, text: str) -> str:
    raw = f"{workspace_id}|{piece_id}|{outcome}|{text[:400]}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


async def record_event(
    *,
    outcome: str,
    categories: list[str],
    matches: list[str],
    text: str,
    where: str,
    workspace_id: Optional[str] = None,
    piece_id: Optional[str] = None,
    platform: Optional[str] = None,
) -> None:
    """Save one row. The same text for the same post is stored once, so a post that keeps being retried does not flood the list."""
    from pymongo.errors import DuplicateKeyError

    from app.db.mongo import safety_events

    row: dict[str, Any] = {
        "fingerprint": _fingerprint(workspace_id, piece_id, outcome, text),
        "outcome": outcome,
        "categories": categories,
        "matches": matches[:10],
        "excerpt": text[:EXCERPT],
        "where": where,
        "workspace_id": workspace_id,
        "piece_id": piece_id,
        "platform": platform,
        "created_at": datetime.now(timezone.utc),
    }
    try:
        await safety_events.insert_one(row)
    except DuplicateKeyError:
        pass
    except Exception as exc:
        logger.warning("Could not record a content safety event: %s", exc)
