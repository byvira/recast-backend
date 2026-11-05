"""Search, status filter and sort for the History lists of the four pipelines (pictures, audio, video, text), written once.

Every History screen sends the same three options: ``q`` (words in the title), ``status`` (approved, pending or not approved) and
``sort`` (newest, oldest, or by title). Each list turns them into its own query with these helpers, so they mean the same everywhere."""
from __future__ import annotations

import re
from typing import Any, Literal, Optional

HistorySort = Literal["newest", "oldest", "title"]
HistoryStatus = Literal["approved", "pending", "rejected"]
MAX_TERM = 80


def term_of(q: Optional[str]) -> str:
    """The search words, trimmed and short enough to be safe."""
    return " ".join((q or "").split())[:MAX_TERM]


def search_clause(q: Optional[str], fields: tuple[str, ...]) -> Optional[dict[str, Any]]:
    """A query that matches when the words are in any of the fields (any case). None when nothing was typed."""
    term = term_of(q)
    if not term:
        return None
    pattern = {"$regex": re.escape(term), "$options": "i"}
    return {"$or": [{field: pattern} for field in fields]}


def status_clause(status: Optional[str], field: str = "approval_status") -> Optional[dict[str, Any]]:
    """Approved or not approved match exactly. Pending also matches work that never had a status, which is waiting too."""
    if status in ("approved", "rejected"):
        return {field: status}
    if status == "pending":
        return {field: {"$nin": ["approved", "rejected"]}}
    return None


def sort_spec(sort: Optional[str], id_field: str = "id") -> list[tuple[str, int]]:
    """The sort for a Mongo find. Ties are broken by id so a page never repeats or skips an item."""
    if sort == "oldest":
        return [("created_at", 1), (id_field, 1)]
    if sort == "title":
        return [("title", 1), (id_field, 1)]
    return [("created_at", -1), (id_field, -1)]


def add_clauses(flt: dict[str, Any], *clauses: Optional[dict[str, Any]]) -> dict[str, Any]:
    """The filter with each clause that exists added (all of them must match)."""
    extra = [c for c in clauses if c]
    if not extra:
        return flt
    return {"$and": [flt, *extra]}


def in_memory_sort(rows: list[dict[str, Any]], sort: Optional[str], title_key: str = "title") -> list[dict[str, Any]]:
    """The same three sorts for lists that are built in code (the video list is made from clips inside recordings)."""
    if sort == "title":
        return sorted(rows, key=lambda r: (str(r.get(title_key) or "").lower(), str(r.get("id") or "")))
    return sorted(rows, key=lambda r: (str(r.get("created_at") or ""), str(r.get("id") or "")), reverse=(sort != "oldest"))
