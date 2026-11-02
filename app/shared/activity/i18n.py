"""Every Activity row in its reader's language: titles, descriptions and the few words shown beside them.

Text reaches a row in one of three forms:
  * ``phrase("Scheduled post to {platform} failed", platform="LinkedIn")``: a sentence with named slots. The sentence is translated
    once per language and cached; the slot values (names of platforms, campaigns, people, numbers) go in as they are, so a name is
    never translated or mangled.
  * a plain string: translated whole and cached by its exact words (error messages repeat, so a repeat costs nothing).
  * ``as_written(text)``: already written in the reader's language (Remy's and Odette's own words), kept as it is.

The reader is the member for a member-only row, and the workspace (its language, then its owner's) for everything else. The English
source of each field is kept on the row under ``i18n``, so rows are written again in a new language when someone changes theirs.
Any failure keeps the English: a row is never lost or left blank because a translation did not happen.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

TEXT_FIELDS = ("title", "description", "target_type")
META_FIELDS = ("severity", "decision")
RELOCALIZE_DAYS = 60
RELOCALIZE_MAX = 500


def phrase(template: str, **ctx: Any) -> dict[str, Any]:
    """A sentence with {named} slots. Names and numbers go in as they are; a slot holding words to translate too (an error
    message, a reason) is given as ``words(text)`` or another phrase."""
    return {"$t": template, "$c": {k: v if isinstance(v, dict) else ("" if v is None else str(v)) for k, v in ctx.items()}}


def words(text: Optional[str]) -> dict[str, Any]:
    """Free English text for a slot, translated with the rest (error messages, reasons)."""
    return {"$s": text or ""}


def as_written(text: Optional[str]) -> dict[str, Any]:
    """Text already in the reader's language."""
    return {"$done": text or ""}


def _key(prefix: str, text: str) -> str:
    return f"activity.{prefix}.{hashlib.sha1(text.encode('utf-8')).hexdigest()[:20]}"


def english(value: Any) -> str:
    """The value as English (or as written), with no translation."""
    if isinstance(value, dict):
        if "$done" in value:
            return value["$done"]
        if "$s" in value:
            return value["$s"]
        if "$t" in value:
            try:
                return value["$t"].format(**{k: english(v) for k, v in value.get("$c", {}).items()})
            except (KeyError, IndexError, ValueError):
                return value["$t"]
    return "" if value is None else str(value)


async def render(value: Any, language: Optional[str]) -> str:
    """The value in `language`. English, an unset language or any failure gives the English."""
    if value is None:
        return ""
    if isinstance(value, dict) and "$done" in value:
        return value["$done"]
    if not language or language == "en":
        return english(value)
    from app.shared.localized_strings import get_localized_string

    try:
        if isinstance(value, dict) and "$t" in value:
            ctx = {k: (await render(v, language) if isinstance(v, dict) else v) for k, v in value.get("$c", {}).items()}
            return await get_localized_string(_key("t", value["$t"]), language, value["$t"], ctx)
        text = value["$s"] if isinstance(value, dict) and "$s" in value else str(value)
        if not text.strip():
            return text
        # a plain string has no slots: its braces are kept as text
        escaped = text.replace("{", "{{").replace("}", "}}")
        return await get_localized_string(_key("s", text), language, escaped)
    except Exception as exc:  # noqa: BLE001 - the English is a correct answer, a blank row is not
        logger.warning("activity translation failed (%s): %s", language, exc)
        return english(value)


async def reading_language(doc: dict[str, Any]) -> str:
    """Whose language the row is read in: the member for a member-only row, the workspace for the rest."""
    from app.shared.activity.projector import member_language, workspace_admin_language

    try:
        if doc.get("visibility") == "member" and doc.get("member_user_id"):
            return await member_language(doc["member_user_id"], doc.get("workspace_id"))
        return await workspace_admin_language(doc.get("workspace_id"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("activity reading language lookup failed for %s: %s", doc.get("workspace_id"), exc)
        return "en"


async def localize(doc: dict[str, Any]) -> dict[str, Any]:
    """Turns the row's text fields into its reader's language in place, keeping each field's source under ``i18n``. A row coming
    fresh from where it happened gives new sources; a stored row saved again (it carries ``i18n``, its fields already translated)
    keeps the sources it has, so a translation is never mistaken for the English."""
    stored = (doc.get("i18n") or {}).get("sources")
    sources: dict[str, Any] = dict(stored or {})
    meta = doc.get("metadata") or {}
    if stored is None:
        for field in TEXT_FIELDS:
            if doc.get(field) is not None:
                sources[field] = doc[field]
        for field in META_FIELDS:
            if meta.get(field) is not None:
                sources[f"meta_{field}"] = meta[field]
    language = await reading_language(doc)
    for field in TEXT_FIELDS:
        if field in sources and field in doc:
            doc[field] = await render(sources[field], language)
    if meta:
        meta = dict(meta)
        for field in META_FIELDS:
            if f"meta_{field}" in sources and field in meta:
                meta[field] = await render(sources[f"meta_{field}"], language)
        doc["metadata"] = meta
    doc["i18n"] = {"language": language, "sources": sources}
    return doc


async def relocalize(workspace_id: str, *, member_user_id: Optional[str] = None) -> int:
    """Writes recent rows again in their reader's current language, after the workspace's or a member's language changed. Only rows
    that kept their sources, from the last 60 days, at most 500, newest first. Returns how many rows changed."""
    from app.db.mongo import activity_entries

    query: dict[str, Any] = {
        "workspace_id": workspace_id, "i18n.sources": {"$exists": True},
        "occurred_at": {"$gte": datetime.now(timezone.utc) - timedelta(days=RELOCALIZE_DAYS)},
    }
    if member_user_id:
        query.update({"visibility": "member", "member_user_id": member_user_id})
    changed = 0
    async for doc in activity_entries.find(query).sort("occurred_at", -1).limit(RELOCALIZE_MAX):
        language = await reading_language(doc)
        if (doc.get("i18n") or {}).get("language") == language:
            continue
        sources = doc["i18n"]["sources"]
        update: dict[str, Any] = {"i18n.language": language}
        for field in TEXT_FIELDS:
            if field in sources:
                update[field] = await render(sources[field], language)
        for field in META_FIELDS:
            if f"meta_{field}" in sources and (doc.get("metadata") or {}).get(field) is not None:
                update[f"metadata.{field}"] = await render(sources[f"meta_{field}"], language)
        await activity_entries.update_one({"_id": doc["_id"]}, {"$set": update})
        changed += 1
    return changed


def relocalize_soon(workspace_id: str, *, member_user_id: Optional[str] = None) -> None:
    """Starts `relocalize` in the background, so saving a language never waits for the rows to be rewritten."""
    import asyncio

    async def run() -> None:
        try:
            changed = await relocalize(workspace_id, member_user_id=member_user_id)
            logger.info("relocalized %d activity rows for workspace %s", changed, workspace_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("relocalize failed for workspace %s: %s", workspace_id, exc)

    try:
        task = asyncio.get_running_loop().create_task(run())
        _RUNNING.add(task)
        task.add_done_callback(_RUNNING.discard)
    except RuntimeError:
        pass


_RUNNING: set = set()

