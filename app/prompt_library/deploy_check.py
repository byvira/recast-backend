"""After each deploy, compare the prompt files with what Ops has published.

- A code default changed under a live version: "code default changed" (a publisher keeps their text or takes the new one).
- A section the live version has text for no longer exists: "orphaned" (that text is ignored until it is cleared).
- A prompt file was deleted: its history is kept and marked as no longer in the code.
- A prompt proposed in Ops now has a file at the same path: it becomes a normal prompt and keeps its history.

Runs once at start-up, on one server at a time (a short lock in the database).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from pymongo.errors import DuplicateKeyError

from app.prompt_library import rules, service

logger = logging.getLogger(__name__)

LOCK_MINUTES = 5
DEPLOY = "Deploy"


async def _take_lock(db) -> bool:
    now = datetime.now(timezone.utc)
    try:
        result = await db.prompt_library_settings.update_one(
            {"_id": "deploy_lock", "$or": [{"until": {"$lt": now}}, {"until": {"$exists": False}}]},
            {"$set": {"until": now + timedelta(minutes=LOCK_MINUTES)}},
            upsert=True,
        )
    except DuplicateKeyError:
        return False
    return result.modified_count == 1 or result.upserted_id is not None


async def _release_lock(db) -> None:
    await db.prompt_library_settings.update_one({"_id": "deploy_lock"}, {"$set": {"until": datetime.now(timezone.utc)}})


async def check(db) -> dict[str, int]:
    """Brings every stored prompt state in line with the files. Returns counts of what it found."""
    found = {"changed": 0, "orphaned": 0, "missing": 0, "linked": 0}
    now = service.now_iso()

    async for doc in db.prompt_proposed.find({}, {"_id": 1}):
        if service.file_prompt(doc["_id"]) is not None:
            await db.prompt_proposed.delete_one({"_id": doc["_id"]})
            await service.audit(db, DEPLOY, "prompt.linked", doc["_id"], "The proposed prompt now has a file in the code")
            found["linked"] += 1

    async for doc in db.prompt_state.find({}, {"_id": 1, "repoHash": 1, "missing": 1}):
        path = doc["_id"]
        prompt = service.file_prompt(path)
        if prompt is None:
            if await db.prompt_proposed.find_one({"_id": path}, {"_id": 1}):
                continue
            if not doc.get("missing"):
                await db.prompt_state.update_one({"_id": path}, {"$set": {"missing": True}})
                await service.audit(db, DEPLOY, "prompt.removed", path, "The prompt file is no longer in the code. Its history is kept.")
                found["missing"] += 1
            continue
        if doc.get("missing"):
            await db.prompt_state.update_one({"_id": path}, {"$unset": {"missing": ""}})
        if doc.get("repoHash") == prompt.repo_hash:
            continue
        before: dict = {}

        def apply(state: dict) -> dict:
            before["state"] = state
            return rules.apply_repo_deploy(state, prompt.regions, prompt.repo_hash, now)

        after = await service.update_state(db, prompt, apply)
        old = before["state"]
        if after.get("repoChange") and after["repoChange"] != old.get("repoChange"):
            rid = after["repoChange"]["regionId"]
            await service.audit(db, DEPLOY, "repo_default.changed", path, f"Section {rid} changed in code")
            found["changed"] += 1
        new_orphans = sorted(set(after["orphaned"]) - set(old.get("orphaned") or []))
        if new_orphans:
            await service.audit(db, DEPLOY, "region.orphaned", path, f"{', '.join(new_orphans)} no longer exists in code")
            found["orphaned"] += 1
    return found


async def run_once(db) -> dict[str, int] | None:
    if not await _take_lock(db):
        return None
    try:
        found = await check(db)
        if any(found.values()):
            logger.info("Prompt deploy check: %s", found)
        return found
    finally:
        await _release_lock(db)
