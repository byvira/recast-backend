"""The member's own writing settings (Remy, Vocabulary tab: sentence length, rhetorical devices, personal examples) as plain
instructions for the writing prompt.

Only settings the member moved away from the starting values say anything, so a member who never opened them gets exactly the
prompt they had before. The instructions apply to new text only: nothing already written is changed or checked against them.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from app.models.lexicon import WritingBlueprint

logger = logging.getLogger(__name__)

_DEFAULT = WritingBlueprint()
_HEADER = "THE WRITER'S OWN STYLE SETTINGS (follow them in this new text):"
_NEVER = re.compile(r"\b(never|none|no|zero)\b", re.IGNORECASE)


def _safe_phrase(text: str, limit: int = 40) -> str:
    """The member's own words, kept to letters, digits and spaces so they cannot carry instructions."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s.]", "", str(text or ""))).strip()[:limit]


def steering_text(blueprint: dict | WritingBlueprint | None) -> Optional[str]:
    """The instructions for one set of settings, or None when nothing was changed from the starting values."""
    try:
        bp = blueprint if isinstance(blueprint, WritingBlueprint) else WritingBlueprint(**(blueprint or {}))
    except Exception:  # noqa: BLE001
        return None
    lines: list[str] = []

    words = max(5, min(int(bp.sentence_max_words), 80))
    if words < _DEFAULT.sentence_max_words:
        lines.append(f"- Keep sentences short: none longer than about {words} words.")
    elif words > _DEFAULT.sentence_max_words:
        lines.append(f"- Longer sentences are fine, up to about {words} words.")

    density = int(bp.rhetorical_density)
    if density <= 8:
        lines.append("- Write plainly. Avoid rhetorical questions and dramatic contrasts.")
    elif density < 14:
        lines.append("- Use rhetorical devices (questions, contrasts, lists of three) sparingly.")
    elif density >= 24:
        lines.append("- Use rhetorical devices freely: questions, contrasts and lists of three.")
    elif density > 18:
        lines.append("- Use rhetorical devices (questions, contrasts, lists of three) often.")

    if bp.anecdote_frequency != _DEFAULT.anecdote_frequency:
        phrase = _safe_phrase(bp.anecdote_frequency)
        if _NEVER.search(phrase):
            lines.append("- Do not include personal anecdotes.")
        elif phrase:
            lines.append(f"- When the piece is long enough, include a short personal example about {phrase}.")

    return f"{_HEADER}\n" + "\n".join(lines) if lines else None


def drift_sensitivity(blueprint: dict | None) -> float:
    """How much stronger (above 1) or weaker (below 1) the voice check should be for this member. 1.0 while the "drift limit" is
    at its starting value, so a member who never moved it gets the check exactly as before."""
    try:
        pct = int((blueprint or {}).get("drift_threshold_pct", _DEFAULT.drift_threshold_pct))
    except (TypeError, ValueError):
        return 1.0
    pct = max(5, min(pct, 30))
    return round(max(0.5, min(_DEFAULT.drift_threshold_pct / pct, 3.0)), 3)


async def member_drift_sensitivity(workspace_id: Optional[str], user_id: Optional[str]) -> float:
    """`drift_sensitivity` for a member's saved settings. 1.0 when there are none. Never raises."""
    if not workspace_id or not user_id:
        return 1.0
    try:
        from app.db.mongo import member_lexicon

        doc = await member_lexicon.find_one({"workspace_id": workspace_id, "user_id": user_id}, {"writing_blueprint": 1})
        return drift_sensitivity((doc or {}).get("writing_blueprint"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read the voice check setting for %s: %s", workspace_id, exc)
        return 1.0


async def member_writing_steering(workspace_id: Optional[str], user_id: Optional[str]) -> Optional[str]:
    """The member's instructions for the writing prompt, or None. Never raises: it is an extra on top of the prompt."""
    if not workspace_id or not user_id:
        return None
    try:
        from app.db.mongo import member_lexicon

        doc = await member_lexicon.find_one({"workspace_id": workspace_id, "user_id": user_id}, {"writing_blueprint": 1})
        return steering_text((doc or {}).get("writing_blueprint")) if doc else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read the writing settings for %s: %s", workspace_id, exc)
        return None
