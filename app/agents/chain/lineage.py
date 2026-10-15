"""Where a piece of work came from: one `derived_from` list on every item a chain makes, and a walk back through them.

An entry is `{"kind": "piece" | "audio" | "image" | "media" | "project", "id": "..."}`. The list lives on the item itself, so it
survives the chain run record being cleaned up and is read the same way for every type.
"""
from __future__ import annotations

from typing import Any, Optional

from app.db.mongo import get_db

#: kind -> the collection that holds items of that kind
COLLECTIONS = {
    "piece": "content_pieces", "audio": "audio_assets", "image": "media_assets", "media": "media_assets", "project": "video_projects",
}
MAX_DEPTH = 8


def ref(kind: str, item_id: Optional[str]) -> Optional[dict[str, str]]:
    return {"kind": kind, "id": item_id} if item_id and kind in COLLECTIONS else None


async def link(kind: str, item_id: Optional[str], workspace_id: str, parents: list[Optional[dict[str, str]]]) -> bool:
    """Record what `item_id` was made from. Never raises: a missing link must not fail the work it describes."""
    clean = [p for p in parents if p]
    if not item_id or not clean or kind not in COLLECTIONS:
        return False
    try:
        done = await get_db()[COLLECTIONS[kind]].update_one({"id": item_id, "workspace_id": workspace_id}, {"$set": {"derived_from": clean}})
        return done.matched_count == 1
    except Exception:  # noqa: BLE001
        return False


async def ancestors(kind: str, item_id: str, workspace_id: str, *, depth: int = MAX_DEPTH) -> list[dict[str, Any]]:
    """Everything this item was made from, nearest first, each entry with the parents it was itself made from. Cycles and
    missing items end a branch quietly."""
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    frontier = [(kind, item_id)]
    for _ in range(depth):
        nxt: list[tuple[str, str]] = []
        for k, i in frontier:
            collection = COLLECTIONS.get(k)
            if not collection:
                continue
            doc = await get_db()[collection].find_one({"id": i, "workspace_id": workspace_id}, {"derived_from": 1})
            for parent in (doc or {}).get("derived_from") or []:
                key = (parent.get("kind"), parent.get("id"))
                if key in seen or key[0] not in COLLECTIONS or not key[1]:
                    continue
                seen.add(key)
                out.append({"kind": key[0], "id": key[1], "child": {"kind": k, "id": i}})
                nxt.append(key)
        if not nxt:
            break
        frontier = nxt
    return out
