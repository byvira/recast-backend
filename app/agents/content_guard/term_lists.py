"""Word lists kept for one language, and the queue of words people suggest adding.

Ops keeps blocked and allowed words for any language code (no fixed list of languages). A post's list is chosen from its language: the
language it was asked for, or the one its script shows. Words that apply to every language stay in the main lists. A word is never
added to a list by the system alone: it is suggested, and a person with approval rights on Safety accepts or rejects it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from app.db.mongo import get_db
from app.shared.language_profile import profile as language_profile

logger = logging.getLogger(__name__)

LANGUAGES_MAX = 40
TERMS_MAX = 300
TERM_MAX = 80
CODE_MAX = 12
NOTE_MAX = 200
IMPORT_MAX = 200
STATUSES = ("pending", "accepted", "rejected")
SOURCES = ("review", "seed", "staff")


class SuggestionError(ValueError):
    """Something the person can fix; the message is shown as written."""


def code_of(value: Optional[str]) -> str:
    """The base language code: `ta` for `ta`, `ta-IN` and the blend `ta+en`."""
    return (value or "").strip().lower().split("+")[0].split("-")[0].split("_")[0][:CODE_MAX]


def tidy_by_language(raw: Any) -> dict[str, dict[str, list[str]]]:
    """Ops' lists per language: lower-cased codes, trimmed and de-duplicated words, capped so a bad paste cannot grow the document."""
    out: dict[str, dict[str, list[str]]] = {}
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        code = code_of(str(key))
        if not code or not isinstance(value, dict) or len(out) >= LANGUAGES_MAX:
            continue
        entry: dict[str, list[str]] = {}
        for which in ("blocked", "allowed"):
            words: list[str] = []
            for t in value.get(which) or []:
                word = str(t).strip().lower()[:TERM_MAX]
                if word and word not in words:
                    words.append(word)
                if len(words) >= TERMS_MAX:
                    break
            entry[which] = words
        if entry["blocked"] or entry["allowed"]:
            out[code] = entry
    return out


def codes_for(text: str, language: Optional[str]) -> list[str]:
    """The language codes a post is in, for choosing its lists: each part of a blend, or the one its script shows."""
    p = language_profile(text, language)
    codes = [code_of(c) for c in p.code.split("+")] if p.code != "unknown" else []
    return [c for c in dict.fromkeys(codes) if c]


def apply(cfg: dict[str, Any], text: str, language: Optional[str]) -> dict[str, Any]:
    """The settings with this post's language lists added to the main ones. Nothing is removed."""
    lists = cfg.get("terms_by_language") or {}
    if not lists:
        return cfg
    blocked = list(cfg.get("extra_blocked_terms") or [])
    allowed = list(cfg.get("allowed_terms") or [])
    for code in codes_for(text, language):
        mine = lists.get(code) or {}
        blocked += mine.get("blocked") or []
        allowed += mine.get("allowed") or []
    return {**cfg, "extra_blocked_terms": list(dict.fromkeys(blocked)), "allowed_terms": list(dict.fromkeys(allowed))}


# ---- the queue of suggested words ---------------------------------------------------------------------------------------------------
def col():
    return get_db()["safety_term_suggestions"]


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k != "_id"}


def _clean_term(term: Any) -> str:
    word = " ".join(str(term or "").split()).lower()[:TERM_MAX]
    if len(word) < 2:
        raise SuggestionError("Type the word or phrase to suggest.")
    return word


async def suggest(term: Any, language: Any, *, source: str, by: Optional[str], note: Any = "", example: str = "") -> dict[str, Any]:
    """Put a word in the queue for a person to accept. The same word for the same language is queued once, and one that was rejected
    is never queued again."""
    if source not in SOURCES:
        raise SuggestionError("That is not a place suggestions come from.")
    word, code = _clean_term(term), code_of(str(language or "")) or "all"
    existing = await col().find_one({"term": word, "language": code})
    if existing:
        if existing["status"] == "pending":
            await col().update_one({"id": existing["id"]}, {"$inc": {"evidence": 1}})
        return _public(existing)
    from app.agents.content_guard.sensitive import mask_personal

    row = {"id": str(uuid4()), "term": word, "language": code, "source": source, "status": "pending", "evidence": 1, "suggested_by": by,
           "note": " ".join(str(note or "").split())[:NOTE_MAX], "example": mask_personal(example)[:200], "created_at": datetime.now(timezone.utc),
           "decided_at": None, "decided_by": None}
    await col().insert_one(dict(row))
    return _public(row)


async def import_seeds(words: list[Any], language: Any, *, by: str) -> dict[str, int]:
    """A pasted list of words (for example from an open word list) goes into the queue as suggestions, never straight into the rules."""
    added = skipped = 0
    for w in list(words or [])[:IMPORT_MAX]:
        try:
            before = await col().count_documents({})
            await suggest(w, language, source="seed", by=by)
            added += 1 if await col().count_documents({}) > before else 0
        except SuggestionError:
            skipped += 1
    return {"added": added, "skipped": skipped + max(0, len(words or []) - IMPORT_MAX)}


async def listing(status: str = "pending", limit: int = 200) -> dict[str, Any]:
    query = {"status": status} if status in STATUSES else {}
    rows = await col().find(query, {"_id": 0}).sort([("evidence", -1), ("created_at", -1)]).limit(min(max(limit, 1), 500)).to_list(500)
    return {"pending": await col().count_documents({"status": "pending"}), "items": rows}


async def decide(suggestion_id: str, *, decision: str, staff_id: str, staff_name: Optional[str]) -> dict[str, Any]:
    """Accept a suggested word, which adds it to that language's blocked words as a recorded change, or reject it for good."""
    if decision not in ("accept", "reject"):
        raise SuggestionError("Choose accept or reject.")
    row = await col().find_one({"id": suggestion_id})
    if not row:
        raise LookupError("That suggestion was not found.")
    if row["status"] != "pending":
        raise SuggestionError("This suggestion was already answered.")
    if decision == "accept":
        from app.agents.content_guard import config as guard_config

        current = await guard_config.load()
        lists = {k: {"blocked": list(v.get("blocked") or []), "allowed": list(v.get("allowed") or [])} for k, v in (current.get("terms_by_language") or {}).items()}
        if row["language"] == "all":
            blocked = list(dict.fromkeys([*(current.get("extra_blocked_terms") or []), row["term"]]))
            await guard_config.save({"extra_blocked_terms": blocked}, expected_version=int(current.get("version", 0)), user_id=staff_id,
                                    reason=f"Accepted the suggested word “{row['term']}”", user_name=staff_name)
        else:
            entry = lists.setdefault(row["language"], {"blocked": [], "allowed": []})
            entry["blocked"] = list(dict.fromkeys([*entry["blocked"], row["term"]]))
            await guard_config.save({"terms_by_language": lists}, expected_version=int(current.get("version", 0)), user_id=staff_id,
                                    reason=f"Accepted the suggested word “{row['term']}” for {row['language']}", user_name=staff_name)
    status = "accepted" if decision == "accept" else "rejected"
    await col().update_one({"id": suggestion_id}, {"$set": {"status": status, "decided_at": datetime.now(timezone.utc), "decided_by": staff_id}})
    return _public(await col().find_one({"id": suggestion_id}))
