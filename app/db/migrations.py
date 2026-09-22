"""One-time, idempotent startup migrations.

Each migration records its id in the ``migrations`` collection and runs at most
once per database. Called from the app lifespan right after ``create_indexes()``.
Failures are logged, never raised — a migration hiccup must not stop startup.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.db.mongo import content_pieces, get_db, workspaces

logger = logging.getLogger(__name__)


async def _already_applied(db, migration_id: str) -> bool:
    return await db["migrations"].find_one({"_id": migration_id}) is not None


async def _mark_applied(db, migration_id: str, result: dict) -> None:
    await db["migrations"].insert_one({
        "_id": migration_id,
        "applied_at": datetime.now(timezone.utc),
        "result": result,
    })


async def _backfill_content_pieces_pipeline_type(db) -> None:
    """Stamp ``pipeline_type="text"`` onto every pre-existing ``content_pieces``
    document that predates the field.

    The text pipeline is the only producer today, so all legacy pieces are text.
    Once this has run, the persona history adapter's back-compat branch is a
    permanent no-op.
    """
    migration_id = "2026-09-10_content_pieces_pipeline_type_text"
    if await _already_applied(db, migration_id):
        return

    res = await content_pieces.update_many(
        {"pipeline_type": {"$exists": False}},
        {"$set": {"pipeline_type": "text"}},
    )
    logger.info(
        "migration %s: stamped pipeline_type=text on %d/%d content_pieces",
        migration_id, res.modified_count, res.matched_count,
    )
    await _mark_applied(
        db, migration_id,
        {"matched": res.matched_count, "modified": res.modified_count},
    )


async def _backfill_workspace_is_personal_and_language(db) -> None:
    """Stamp ``is_personal=False`` and ``language=None`` onto every workspace
    document that predates those fields.

    ``POST /api/v1/workspaces/`` (app/api/v1/workspace.py::create_workspace)
    never wrote either field — only ``create_personal_workspace()`` did, and
    only ``is_personal`` (always ``True`` there). So every workspace missing
    ``is_personal`` was, unambiguously, created via the general route and is
    not personal. Frontend schema validation on GET /workspaces/:id was
    failing outright on these documents (both fields entirely absent, not
    just null) — this is the real fix; both write paths are also fixed
    going forward (see create_workspace/create_personal_workspace).
    """
    migration_id = "2026-09-23_workspace_is_personal_and_language_backfill"
    if await _already_applied(db, migration_id):
        return

    res = await workspaces.update_many(
        {"is_personal": {"$exists": False}},
        {"$set": {"is_personal": False}},
    )
    res_lang = await workspaces.update_many(
        {"language": {"$exists": False}},
        {"$set": {"language": None}},
    )
    logger.info(
        "migration %s: is_personal backfilled on %d/%d, language backfilled on %d/%d",
        migration_id, res.modified_count, res.matched_count,
        res_lang.modified_count, res_lang.matched_count,
    )
    await _mark_applied(
        db, migration_id,
        {
            "is_personal_matched": res.matched_count, "is_personal_modified": res.modified_count,
            "language_matched": res_lang.matched_count, "language_modified": res_lang.modified_count,
        },
    )


async def run_startup_migrations() -> None:
    db = get_db()
    for migration in (
        _backfill_content_pieces_pipeline_type,
        _backfill_workspace_is_personal_and_language,
    ):
        try:
            await migration(db)
        except Exception as exc:  # noqa: BLE001 — never block startup
            logger.error("startup migration %s failed: %s", migration.__name__, exc, exc_info=True)
