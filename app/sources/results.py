"""How a source is doing, from what became of its drafts.

Every draft a source makes carries the item's link in its text (the saved draft in link mode, and the credit line in write mode), so the pieces that came from a
source are found by that link, with no extra field to keep in step. A draft is **kept** when a person approved it (or it was scheduled or published) and was not
removed; it is **published** when it went out. The keep rate is the number that says whether a source is worth following: a feed whose drafts are mostly thrown
away is noise, and says so.

Odette's advice goes to the Activity Log (not Control Tower): a source that keeps producing drafts nobody keeps is suggested for removal, and one whose drafts do well
is mentioned as worth more of the same."""
from __future__ import annotations

import re
from typing import Any

from app.db.mongo import get_db
from app.sources import flow

MIN_DRAFTS_TO_JUDGE = 8                 # fewer drafts than this say nothing yet
LOW_KEEP = 0.15                         # under this share kept is noise
GOOD_KEEP = 0.5
MAX_ITEMS_LOOKED_AT = 100


def _link_pattern(link: str) -> re.Pattern:
    return re.compile(re.escape(link.rstrip("/")))


async def pieces_for(workspace_id: str, link: str) -> list[dict[str, Any]]:
    if not link:
        return []
    return await get_db()["content_pieces"].find(
        {"workspace_id": workspace_id, "content": {"$regex": _link_pattern(link).pattern}, "deleted": {"$ne": True}},
        {"_id": 0, "piece_id": 1, "approval_status": 1, "publish_status": 1, "deleted": 1},
    ).to_list(length=5)


def judge(piece: dict[str, Any]) -> tuple[bool, bool]:
    """(kept, published) for one piece."""
    approved = piece.get("approval_status") == "approved"
    published = piece.get("publish_status") == "published"
    queued = piece.get("publish_status") in ("queued", "published")
    return approved or queued, published


def verdict(drafted: int, kept: int, published: int) -> dict[str, Any]:
    """The numbers and what they mean, in plain words. Under `MIN_DRAFTS_TO_JUDGE` drafts nothing is said, because a few drafts prove nothing."""
    rate = round(kept / drafted, 2) if drafted else None
    out: dict[str, Any] = {"drafted": drafted, "kept": kept, "published": published, "keep_rate": rate, "judgement": None}
    if drafted < MIN_DRAFTS_TO_JUDGE:
        out["judgement"] = "too_early"
    elif rate is not None and rate < LOW_KEEP:
        out["judgement"] = "noise"
    elif rate is not None and rate >= GOOD_KEEP:
        out["judgement"] = "good"
    else:
        out["judgement"] = "mixed"
    return out


async def feedback_counts(workspace_id: str, source_id: str) -> dict[str, int]:
    """What the person said about this source's items: good ones, ones not for them, and good ones that Ekavo had skipped (a sign the source is held too tight)."""
    out = {"good": 0, "not_for_me": 0, "good_skipped": 0}
    async for row in flow.items_col().aggregate([
        {"$match": {"workspace_id": workspace_id, "source_id": source_id, "feedback": {"$in": ["good", "not_for_me"]}}},
        {"$group": {"_id": {"f": "$feedback", "o": "$outcome"}, "n": {"$sum": 1}}},
    ]):
        out[row["_id"]["f"]] += row["n"]
        if row["_id"]["f"] == "good" and row["_id"]["o"] == "skipped":
            out["good_skipped"] += row["n"]
    return out


async def stats(workspace_id: str, source_id: str) -> dict[str, Any]:
    drafted = kept = published = 0
    rows = await flow.items_col().find({"workspace_id": workspace_id, "source_id": source_id, "outcome": "drafted"}, {"_id": 0, "link": 1, "item_id": 1}).limit(MAX_ITEMS_LOOKED_AT).to_list(length=MAX_ITEMS_LOOKED_AT)
    for row in rows:
        pieces = await pieces_for(workspace_id, row.get("link", ""))
        if not pieces:
            drafted += 1                                    # a draft was asked for; its piece may still be writing, or was removed (counts as not kept)
            continue
        drafted += 1
        k, p = judge(pieces[0])
        kept += int(k)
        published += int(p)
    out = verdict(drafted, kept, published)
    out["feedback"] = await feedback_counts(workspace_id, source_id)
    return out


ADVICE = {
    "noise": ("{name}: most of its drafts are not kept", "Fewer than 15 in 100 of this source's drafts were kept. Try the closer relevance setting, or remove it so it stops filling your queue."),
    "good": ("{name}: its drafts are being kept", "Half or more of this source's drafts were kept. It is working well; adding a similar feed may be worth it."),
}


async def advise(source: dict[str, Any]) -> bool:
    """One note in the Activity Log when a source clearly works or is clearly noise, at most once a week per source. Returns whether a note was written."""
    from datetime import datetime, timedelta, timezone

    result = await stats(source["workspace_id"], source["id"])
    if result["judgement"] not in ADVICE:
        return False
    marked = source.get("advised") or {}
    last = marked.get(result["judgement"])
    if isinstance(last, datetime) and (last if last.tzinfo else last.replace(tzinfo=timezone.utc)) > datetime.now(timezone.utc) - timedelta(days=7):
        return False
    title, text = ADVICE[result["judgement"]]
    from app.shared.activity import record_system

    await record_system(workspace_id=source["workspace_id"], key=f"source-advice:{source['id']}:{result['judgement']}:{datetime.now(timezone.utc).strftime('%G-%V')}", actor_name="Odette",
                        category="recommendation", title=title.format(name=source.get("name") or "A source"), description=text, status="warning" if result["judgement"] == "noise" else "success", target_id=source["id"], target_type="Source",
                        target_label=source.get("name"), href="/dashboard/library")
    await get_db()["sources"].update_one({"id": source["id"]}, {"$set": {f"advised.{result['judgement']}": datetime.now(timezone.utc)}})
    return True
