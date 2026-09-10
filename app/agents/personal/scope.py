"""Query-layer isolation for Layer 1.

Every Mongo read/write in the personal-assistant package spreads
``scoped_filter(event)`` into its filter, so a query can only ever touch the
(workspace, member) the triggering event is about. The values come *only* from
the validated event envelope — never from a broad scan, never from anything the
LLM produced.

Cross-member isolation within a workspace and cross-workspace isolation are both
consequences of this one helper plus the deterministic persona ``_id``
(``f"{workspace_id}:{user_id}"``): even ``find_one({"_id": ...})`` is scoped.
"""

from __future__ import annotations

from typing import Any


class ScopeError(ValueError):
    """Raised when an event lacks the identifiers needed to scope a query."""


def event_scope(event: dict[str, Any]) -> tuple[str, str]:
    """Return ``(workspace_id, subject_user_id)`` from an event envelope.

    For ``content.*`` events the subject is ``actor_user_id`` (the creator).
    Raises :class:`ScopeError` if either id is missing — the caller must abort
    rather than run an unscoped query.
    """
    workspace_id = (event or {}).get("workspace_id")
    user_id = (event or {}).get("actor_user_id")
    if not workspace_id or not user_id:
        raise ScopeError(
            f"event missing scope identifiers: workspace_id={workspace_id!r} "
            f"actor_user_id={user_id!r}"
        )
    return workspace_id, user_id


def scoped_filter(event: dict[str, Any]) -> dict[str, str]:
    """A Mongo filter fragment that pins a query to one member in one workspace."""
    workspace_id, user_id = event_scope(event)
    return {"workspace_id": workspace_id, "user_id": user_id}
