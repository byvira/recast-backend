"""Asking for a review of a hold, the answer staff give, and the numbers that show whether a rule helps.

A member whose post was held can ask for a review. Staff with approval rights on Safety answer it: release the post (it may go out
exactly as written, and only as written) or uphold the hold. Every release is kept as an override. Nobody answers a review about a
rule they changed themselves. The rule results compare what each check held with what staff then released, so a check that is
usually wrong shows up in numbers.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

from app.agents.content_guard import config as guard_config
from app.agents.content_guard.rules import CATEGORIES, content_hash
from app.agents.content_guard.sensitive import mask_personal
from pymongo.errors import DuplicateKeyError

from app.db.mongo import get_db

logger = logging.getLogger(__name__)

NOTE_MAX = 400
EXCERPT_MAX = 300
STATUSES = ("open", "released", "upheld")
CONFLICT_DAYS = 30


class AppealError(ValueError):
    """Something the person can fix or should know; the message is shown as written."""


def appeals_col():
    return get_db()["safety_appeals"]


def overrides_col():
    return get_db()["safety_overrides"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _note(text: Any, *, required: bool = False) -> str:
    out = " ".join(str(text or "").split())[:NOTE_MAX]
    if required and len(out) < 4:
        raise AppealError("Say in a few words why.")
    return out


def _public(row: dict[str, Any]) -> dict[str, Any]:
    row = {k: v for k, v in row.items() if k != "_id"}
    row["category_names"] = [CATEGORIES.get(c, c) for c in row.get("categories") or []]
    return row


async def held_categories(piece: dict[str, Any]) -> list[str]:
    """The checks a held post failed, as category keys: the free rules again on the exact text, plus any model or media verdict stored."""
    from app.agents.content_guard.agent import rule_screen

    text = str(piece.get("content") or "")
    found = list(rule_screen(text, own_contacts=piece.get("own_contacts") or ()).categories)
    stored = piece.get("safety_check")
    if isinstance(stored, dict) and stored.get("ok") is False and stored.get("hash") == content_hash(text):
        found += [c for c in stored.get("categories") or [] if c not in found]
    for record in (piece.get("media_safety") or {}).values():
        if isinstance(record, dict) and record.get("ok") is False:
            found += [c for c in record.get("categories") or [] if c not in found]
    if not found and piece.get("safety_reason"):
        names = {v: k for k, v in CATEGORIES.items()}
        found = [names[n] for n in (piece["safety_reason"].get("categories") or []) if n in names]
    return found


def _source(piece: dict[str, Any], categories: list[str]) -> str:
    stored = piece.get("safety_check")
    if isinstance(stored, dict) and stored.get("ok") is False:
        return "model"
    if any(isinstance(r, dict) and r.get("ok") is False for r in (piece.get("media_safety") or {}).values()):
        return "media"
    return "rules"


async def request_review(piece: dict[str, Any], *, user_id: str, note: Any) -> dict[str, Any]:
    """A member asks for a review of a held post. One open request per post and text; an edit starts afresh."""
    if not piece.get("flagged_for_review") or not (piece.get("safety_reason") or piece.get("safety_check") or piece.get("media_safety")):
        raise AppealError("This post is not held by the safety check, so there is nothing to review.")
    text = str(piece.get("content") or "")
    digest = content_hash(text)
    if await appeals_col().find_one({"piece_id": piece.get("piece_id"), "hash": digest, "status": "open"}):
        raise AppealError("A review of this post is already waiting. You will see the answer here.")
    override = piece.get("safety_override")
    if isinstance(override, dict) and override.get("hash") == digest:
        raise AppealError("This post was already released for sending.")
    categories = await held_categories(piece)
    row = {
        "id": str(uuid4()), "status": "open", "piece_id": piece.get("piece_id"), "workspace_id": piece.get("workspace_id"),
        "brand_id": piece.get("brand_id"), "platform": piece.get("platform"), "language": piece.get("language"), "user_id": user_id,
        "hash": digest, "categories": categories, "source": _source(piece, categories),
        "matches": [mask_personal(m) for m in ((piece.get("safety_reason") or {}).get("matches") or [])][:6],
        "excerpt": mask_personal(text)[:EXCERPT_MAX], "member_note": _note(note), "created_at": _now(), "decided_at": None,
        "decided_by": None, "decision_note": None,
    }
    try:
        await appeals_col().insert_one(dict(row))
    except DuplicateKeyError:
        raise AppealError("A review of this post is already waiting. You will see the answer here.") from None
    return _public(row)


async def mine(piece_id: str, workspace_id: str) -> Optional[dict[str, Any]]:
    row = await appeals_col().find_one({"piece_id": piece_id, "workspace_id": workspace_id}, {"_id": 0}, sort=[("created_at", -1)])
    return _public(row) if row else None


async def listing(status: Optional[str] = None, limit: int = 100) -> dict[str, Any]:
    query: dict[str, Any] = {"status": status} if status in STATUSES else {}
    rows = await appeals_col().find(query, {"_id": 0}).sort("created_at", -1).limit(min(max(limit, 1), 200)).to_list(200)
    return {"open": await appeals_col().count_documents({"status": "open"}), "items": [_public(r) for r in rows]}


async def _authored_the_rule(user_id: str, appeal: dict[str, Any]) -> bool:
    """Whether this person changed the setting that probably caused the hold, in the last weeks: a term they added, or how strict."""
    since = _now() - timedelta(days=CONFLICT_DAYS)
    matches = {str(m).lower() for m in appeal.get("matches") or []}
    rows = await guard_config.history_col().find({"by": user_id, "at": {"$gte": since}}, {"_id": 0, "changes": 1}).to_list(100)
    for row in rows:
        for change in row.get("changes") or []:
            if change["path"] == "extra_blocked_terms" and matches & {str(t).lower() for t in change.get("added") or []}:
                return True
            if change["path"] == "strictness" and set(appeal.get("categories") or []) & {"profanity", "sexual"}:
                return True
    return False


async def decide(appeal_id: str, *, decision: str, note: Any, staff: dict[str, Any]) -> dict[str, Any]:
    """Release the post for sending exactly as written, or uphold the hold. Either way the member sees the answer in Activity."""
    if decision not in ("release", "uphold"):
        raise AppealError("Choose release or uphold.")
    appeal = await appeals_col().find_one({"id": appeal_id})
    if not appeal:
        raise LookupError("That review request was not found.")
    if appeal["status"] != "open":
        # "released" is saved before the post itself is released. If the server stopped between the two, answering the same way again
        # finishes the release; any other repeat is refused as before.
        if appeal["status"] == "released" and decision == "release" and not await _release_done(appeal):
            await _release(appeal, str(staff.get("id")), _note(note, required=True), _now())
            await _tell_member(appeal, "released")
            return _public(await appeals_col().find_one({"id": appeal_id}))
        raise AppealError("This request was already answered.")
    reason = _note(note, required=True)
    staff_id = str(staff.get("id"))
    if await _authored_the_rule(staff_id, appeal):
        raise AppealError("You changed the rule that held this post, so someone else needs to answer it.")
    status = "released" if decision == "release" else "upheld"
    now = _now()
    res = await appeals_col().update_one(
        {"id": appeal_id, "status": "open"},
        {"$set": {"status": status, "decided_at": now, "decided_by": staff_id, "decided_by_name": staff.get("name") or staff.get("email"), "decision_note": reason}},
    )
    if not res.modified_count:
        raise AppealError("This request was already answered.")
    if decision == "release":
        await _release(appeal, staff_id, reason, now)
    await _tell_member(appeal, status)
    return _public(await appeals_col().find_one({"id": appeal_id}))


async def _release(appeal: dict[str, Any], staff_id: str, reason: str, now: datetime) -> None:
    from app.db.mongo import content_pieces

    if not await overrides_col().find_one({"appeal_id": appeal["id"]}):
        await overrides_col().insert_one({
            "id": str(uuid4()), "appeal_id": appeal["id"], "piece_id": appeal["piece_id"], "workspace_id": appeal["workspace_id"],
            "hash": appeal["hash"], "categories": appeal.get("categories") or [], "source": appeal.get("source"), "platform": appeal.get("platform"),
            "language": appeal.get("language"), "decided_by": staff_id, "note": reason, "created_at": now,
        })
    piece = await content_pieces.find_one({"piece_id": appeal["piece_id"], "workspace_id": appeal["workspace_id"]}, {"quality_issues": 1, "content": 1})
    if not piece or content_hash(str(piece.get("content") or "")) != appeal["hash"]:
        return  # the post was edited since; the release belongs to the old text only
    issues = [i for i in (piece.get("quality_issues") or []) if not str(i).startswith("May contain")]
    update: dict[str, Any] = {
        "safety_override": {"hash": appeal["hash"], "appeal_id": appeal["id"], "by": staff_id, "at": now},
        "flagged_for_review": False, "quality_issues": issues,
    }
    if not issues:
        update["quality_passed"] = True
    await content_pieces.update_one({"piece_id": appeal["piece_id"], "workspace_id": appeal["workspace_id"]}, {"$set": update, "$unset": {"safety_reason": ""}})


async def _release_done(appeal: dict[str, Any]) -> bool:
    """Whether the release of an answered request is complete: its override is kept, and the post carries it unless the text changed."""
    from app.db.mongo import content_pieces

    if not await overrides_col().find_one({"appeal_id": appeal["id"]}):
        return False
    piece = await content_pieces.find_one({"piece_id": appeal["piece_id"], "workspace_id": appeal["workspace_id"]}, {"content": 1, "safety_override": 1})
    if not piece or content_hash(str(piece.get("content") or "")) != appeal["hash"]:
        return True
    return (piece.get("safety_override") or {}).get("appeal_id") == appeal["id"]


async def _tell_member(appeal: dict[str, Any], status: str) -> None:
    try:
        from app.shared.activity.i18n import phrase
        from app.shared.activity.projector import record_system

        released = status == "released"
        await record_system(
            workspace_id=appeal["workspace_id"], key=f"appeal:{appeal['id']}", actor_name="Ekavo safety team", actor_user_id=appeal.get("user_id"),
            category="safety",
            title=phrase("Your review request was answered"),
            description=phrase("The {platform} post can go out as written." if released else "The {platform} post stays held. Edit it, or ask for a rewrite.",
                               platform=appeal.get("platform") or "post"),
            target_id=appeal["piece_id"], target_type="piece", visibility="workspace",
            metadata={"event": "safety.appeal", "status": status},
        )
    except Exception as exc:  # noqa: BLE001 - the answer is saved; only the Activity row is missing
        logger.warning("Could not tell the member about a review answer: %s", exc)


def released_for(piece: dict[str, Any]) -> bool:
    """Whether staff released exactly this text. Any edit changes the text, so a release never carries over to new words."""
    override = piece.get("safety_override")
    return isinstance(override, dict) and override.get("hash") == content_hash(str(piece.get("content") or ""))


async def overrides(limit: int = 100) -> list[dict[str, Any]]:
    rows = await overrides_col().find({}, {"_id": 0}).sort("created_at", -1).limit(min(max(limit, 1), 200)).to_list(200)
    for r in rows:
        r["category_names"] = [CATEGORIES.get(c, c) for c in r.get("categories") or []]
    return rows


async def rule_results(days: int = 30) -> dict[str, Any]:
    """For every check: how many posts it held, how many members asked for a review, and how many staff then released. A check whose
    holds are mostly released is probably wrong and shows first."""
    since = _now() - timedelta(days=days if days in (7, 30, 90) else 30)
    held: dict[str, int] = {}
    async for e in get_db()["safety_events"].find({"created_at": {"$gte": since}, "outcome": "blocked"}, {"categories": 1, "occurrences": 1}):
        for c in e.get("categories") or []:
            held[c] = held.get(c, 0) + int(e.get("occurrences") or 1)
    asked: dict[str, int] = {}
    released: dict[str, int] = {}
    upheld: dict[str, int] = {}
    async for a in appeals_col().find({"created_at": {"$gte": since}}, {"categories": 1, "status": 1}):
        for c in a.get("categories") or []:
            asked[c] = asked.get(c, 0) + 1
            if a["status"] == "released":
                released[c] = released.get(c, 0) + 1
            elif a["status"] == "upheld":
                upheld[c] = upheld.get(c, 0) + 1
    rows = []
    for c in sorted(set(held) | set(asked)):
        answered = released.get(c, 0) + upheld.get(c, 0)
        rows.append({
            "category": c, "label": CATEGORIES.get(c, c), "held": held.get(c, 0), "asked": asked.get(c, 0), "released": released.get(c, 0),
            "upheld": upheld.get(c, 0), "released_share": None if answered == 0 else round(100 * released.get(c, 0) / answered, 1), "answered": answered,
        })
    rows.sort(key=lambda r: (-(r["released_share"] or 0), -r["held"]))
    return {"days": days if days in (7, 30, 90) else 30, "rows": rows}
