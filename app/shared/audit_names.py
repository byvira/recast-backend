"""Plain names for the ids a change history points at, so Ops reads "Acme Studio" and "Priya", never an object id.

A history row keeps the id it was about. Names change, and some rows were written with the id in place of a name, so the name is
looked up when the history is read: a workspace, a person, a staff role or a page of website content. A row that points at something
since deleted keeps whatever name it was saved with."""
from __future__ import annotations

from typing import Any, Iterable

from app.db.mongo import get_db


def _looks_like_id(value: Any) -> bool:
    text = str(value or "")
    return bool(text) and " " not in text and "@" not in text and len(text) >= 12 and any(c.isdigit() for c in text)


async def readable(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each row with `target_name` and `by_email` filled in with something a person can read."""
    rows = [dict(r) for r in rows]
    targets = {str(r["target"]) for r in rows if r.get("target") and (not r.get("target_name") or r.get("target_name") == r.get("target")) and _looks_like_id(r["target"])}
    actors = {str(r["by"]) for r in rows if r.get("by") and not r.get("by_email") and _looks_like_id(r["by"])}
    ids = list(targets | actors)
    names: dict[str, str] = {}
    if ids:
        db = get_db()
        async for w in db["workspaces"].find({"id": {"$in": ids}}, {"id": 1, "name": 1}):
            names[w["id"]] = w.get("name") or ""
        async for u in db["users"].find({"id": {"$in": ids}}, {"id": 1, "name": 1, "email": 1}):
            names[u["id"]] = u.get("name") or u.get("email") or ""
        async for r in db["staff_roles"].find({"id": {"$in": ids}}, {"id": 1, "name": 1}):
            names[r["id"]] = r.get("name") or ""
        async for d in db["site_content"].find({"id": {"$in": ids}}, {"id": 1, "title": 1, "key": 1}):
            names[d["id"]] = d.get("title") or d.get("key") or ""
    emails = {u["id"]: u.get("email") async for u in get_db()["users"].find({"id": {"$in": list(actors)}}, {"id": 1, "email": 1})} if actors else {}
    for r in rows:
        t = str(r.get("target") or "")
        if t in names and names[t] and (not r.get("target_name") or r.get("target_name") == t):
            r["target_name"] = names[t]
        elif not r.get("target_name"):
            r["target_name"] = r.get("target") or ""
        if not r.get("by_email") and emails.get(str(r.get("by") or "")):
            r["by_email"] = emails[str(r["by"])]
    return rows
