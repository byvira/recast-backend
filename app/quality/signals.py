"""How members treat what Ekavo writes: what they send back and why, and how much they change what they keep.

Two signals, both counted per platform and language, and neither judging a post by itself:
  * a reason, tapped by the member when they send a post back, ask for another version or throw it away;
  * an edit class for every post a member kept: untouched (the words were not changed), light (under 15% of the words changed) or
    heavy (15% or more). A platform or language where most kept posts are heavily edited is where the writing is weakest.
"""
from __future__ import annotations

import difflib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

from app.db.mongo import get_db

logger = logging.getLogger(__name__)

REASONS: dict[str, str] = {
    "too_long": "Too long", "off_brand": "Off brand", "wrong_facts": "Wrong facts", "wrong_tone": "Wrong tone", "other": "Something else",
}
KINDS: dict[str, str] = {"sent_back": "Sent back to drafts", "regenerated": "Asked for another version", "discarded": "Thrown away"}
LIGHT_EDIT_BELOW = 0.15
RECOMPUTE_DAYS = 3
NOTE_MAX = 200


class FeedbackError(ValueError):
    """Something the person can fix; the message is shown as written."""


def feedback_col():
    return get_db()["quality_feedback"]


def daily_col():
    return get_db()["quality_signals_daily"]


def edit_class(original: str, final: str) -> str:
    """untouched, light or heavy, by the share of words that changed between the first version and the one the member kept."""
    a, b = (original or "").split(), (final or "").split()
    if a == b:
        return "untouched"
    if not a or not b:
        return "heavy"
    changed = 1 - difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
    return "light" if changed < LIGHT_EDIT_BELOW else "heavy"


async def record_feedback(piece: dict[str, Any], *, kind: str, reason: str, user_id: str, note: Any = "") -> dict[str, Any]:
    """Keep what the member said about a post. Unknown kinds and reasons are refused; a reason is optional only for nothing."""
    if kind not in KINDS:
        raise FeedbackError("That is not something we can note.")
    if reason not in REASONS:
        raise FeedbackError("Pick one of the reasons shown.")
    row = {
        "id": str(uuid4()), "piece_id": piece.get("piece_id"), "workspace_id": piece.get("workspace_id"), "platform": piece.get("platform"),
        "language": piece.get("language") or "unknown", "kind": kind, "reason": reason, "note": " ".join(str(note or "").split())[:NOTE_MAX],
        "version_count": piece.get("version_count") or 1, "user_id": user_id, "at": datetime.now(timezone.utc),
    }
    await feedback_col().insert_one(dict(row))
    return {k: v for k, v in row.items() if k != "_id"}


async def compute_days(days: int = RECOMPUTE_DAYS) -> int:
    """Count, for each recent day and each platform and language, the kept posts by edit class and the things members sent back and
    why. Written again each night for the last few days, so late edits are counted. Returns how many day rows were written."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows: dict[tuple, dict[str, Any]] = {}

    def bucket(day: str, platform: str, language: str) -> dict[str, Any]:
        return rows.setdefault((day, platform, language), {"day": day, "platform": platform, "language": language, "untouched": 0, "light": 0, "heavy": 0, "kept": 0,
                                                            "sent_back": 0, "regenerated": 0, "discarded": 0, "reasons": {}})

    pieces = get_db()["content_pieces"]
    versions = get_db()["content_piece_versions"]
    async for piece in pieces.find({"created_at": {"$gte": since}, "approval_status": "approved", "deleted": {"$ne": True}},
                                   {"piece_id": 1, "platform": 1, "language": 1, "content": 1, "created_at": 1}):
        first = await versions.find_one({"piece_id": piece["piece_id"], "version_number": 1}, {"content": 1})
        if not first:
            continue
        b = bucket(piece["created_at"].date().isoformat(), str(piece.get("platform") or "Unknown"), str(piece.get("language") or "unknown"))
        b[edit_class(first.get("content") or "", piece.get("content") or "")] += 1
        b["kept"] += 1
    async for f in feedback_col().find({"at": {"$gte": since}}):
        b = bucket(f["at"].date().isoformat(), str(f.get("platform") or "Unknown"), str(f.get("language") or "unknown"))
        b[f["kind"]] += 1
        b["reasons"][f["reason"]] = b["reasons"].get(f["reason"], 0) + 1
    for row in rows.values():
        await daily_col().update_one({"day": row["day"], "platform": row["platform"], "language": row["language"]}, {"$set": row}, upsert=True)
    return len(rows)


async def summary(days: int = 30) -> dict[str, Any]:
    """The edit signal and the reasons for the window: totals, a series by day, and each platform and language with its heavy-edit share."""
    days = days if days in (7, 30, 90) else 30
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    rows = await daily_col().find({"day": {"$gte": since}}, {"_id": 0}).to_list(5000)
    totals = {"untouched": 0, "light": 0, "heavy": 0, "kept": 0, "sent_back": 0, "regenerated": 0, "discarded": 0}
    reasons: dict[str, int] = {}
    by_day: dict[str, dict[str, int]] = {}
    groups: dict[tuple, dict[str, Any]] = {}
    for r in rows:
        for k in totals:
            totals[k] += int(r.get(k) or 0)
        for k, v in (r.get("reasons") or {}).items():
            reasons[k] = reasons.get(k, 0) + int(v)
        d = by_day.setdefault(r["day"], {"untouched": 0, "light": 0, "heavy": 0})
        for k in d:
            d[k] += int(r.get(k) or 0)
        g = groups.setdefault((r["platform"], r["language"]), {"platform": r["platform"], "language": r["language"], "untouched": 0, "light": 0, "heavy": 0, "kept": 0, "sent_back": 0, "regenerated": 0, "discarded": 0})
        for k in ("untouched", "light", "heavy", "kept", "sent_back", "regenerated", "discarded"):
            g[k] += int(r.get(k) or 0)
    series = []
    today = datetime.now(timezone.utc).date()
    for i in range(days):
        day = (today - timedelta(days=days - 1 - i)).isoformat()
        series.append({"day": day, **by_day.get(day, {"untouched": 0, "light": 0, "heavy": 0})})
    share = lambda part, whole: None if whole == 0 else round(100 * part / whole, 1)  # noqa: E731
    return {
        "days": days, "totals": totals,
        "untouched_share": share(totals["untouched"], totals["kept"]), "heavy_share": share(totals["heavy"], totals["kept"]),
        "reasons": [{"reason": k, "label": REASONS.get(k, k), "count": v} for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])],
        "series": series,
        "groups": sorted(({**g, "heavy_share": share(g["heavy"], g["kept"])} for g in groups.values() if g["kept"] or g["sent_back"] or g["regenerated"] or g["discarded"]),
                         key=lambda g: (-(g["heavy_share"] or 0), -g["kept"])),
    }
