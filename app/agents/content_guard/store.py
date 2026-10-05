"""One row for each output the guard blocked or rewrote, so Ops can see what was caught and why."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

EXCERPT = 280
ORIGINAL_CAP = 8000


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
    rewritten_text: Optional[str] = None,
    source: Optional[str] = None,
    model_reason: Optional[str] = None,
    stage: Optional[str] = None,
    strictness: Optional[str] = None,
    model_check: Optional[str] = None,
    media_kind: Optional[str] = None,
    media_id: Optional[str] = None,
    brand_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> None:
    """Save one row. The same text for the same post is stored once and counted again each time it is caught, so a post that
    keeps being retried does not flood the list.

    `source` says what caught it: "rules", "model", or "media". `rewritten_text` is the safe version, when one was made."""
    from pymongo.errors import DuplicateKeyError

    from app.db.mongo import safety_events

    now = datetime.now(timezone.utc)
    fingerprint = _fingerprint(workspace_id, piece_id, outcome, text)
    row: dict[str, Any] = {
        "outcome": outcome,
        "categories": categories,
        "matches": matches[:10],
        "excerpt": text[:EXCERPT],
        "original_text": text[:ORIGINAL_CAP],
        "rewritten_text": rewritten_text[:ORIGINAL_CAP] if rewritten_text else None,
        "where": where,
        "source": source,
        "model_reason": model_reason,
        "stage": stage,
        "strictness": strictness,
        "model_check": model_check,
        "media_kind": media_kind,
        "media_id": media_id,
        "workspace_id": workspace_id,
        "brand_id": brand_id,
        "user_id": user_id,
        "piece_id": piece_id,
        "platform": platform,
        "status": "open",
        "created_at": now,
    }
    try:
        await safety_events.update_one(
            {"fingerprint": fingerprint},
            {"$setOnInsert": row, "$set": {"last_seen_at": now}, "$inc": {"occurrences": 1}},
            upsert=True,
        )
    except DuplicateKeyError:
        pass
    except Exception as exc:
        logger.warning("Could not record a content safety event: %s", exc)
