"""Shared building blocks for resolving a caller's effective language.

Every language value handled here is a fully opaque string — nothing in this
module validates it against LANGUAGE_NAMES or any other fixed set, and a
lookup failure or missing field is never silently rewritten to "en" except as
the final, explicit last-resort fallback documented at each call site.

Different consumers legitimately want different precedence orders, so this
module does not impose one chain — it exposes the two lookups
(``workspace_language``, ``user_language``) plus ``first_present`` and lets
each call site compose them to match its own semantics:

  - Content generation (app.api.v1.text) — the piece being generated belongs
    to the workspace's audience, not to whichever staff member clicked
    Generate, so: request override > workspace default > caller's own
    default > "en".
  - Remy, the personal assistant (app.agents.personal) — speaks to one member
    about their own work, so their own preference should win over a
    workspace-wide default: member's own default > workspace default > "en".
  - Odette, the workspace supervisor (app.agents.supervisor.ticks) — briefs
    the workspace's admins as a group, so: workspace default > owner's own
    default (best available proxy until an admin sets one explicitly) > "en".
"""

from __future__ import annotations

import logging
from typing import Optional

from app.db.mongo import users, workspaces

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Content-based language detection — the tier below "no explicit preference
# found anywhere" and above the hardcoded "en" last resort. Library choice:
# py3langid — self-contained (its model ships in the package, no runtime
# download), deterministic (unlike langdetect, which needs a fixed seed to
# stop varying run to run), and its stock model covers all 24
# LANGUAGE_NAMES codes plus 70+ more, ISO-639-1 "ml"/"ta" included — the
# exact two languages this session's live proof and Tanglish false-positive
# test needed.
#
# Live probe on 2026-09-11 (see conversation for full raw output) — genuine
# native-script content scored 0.96-1.00 confidence across ml/en/ta/hi/ko;
# romanized "Tanglish" (Tamil+English code-switched, Latin script) and other
# short/ambiguous strings scored 0.01-0.30, a wide, clean margin. That gap is
# the whole fix: MIN_DETECTION_CONFIDENCE sits at 0.7, comfortably inside it,
# so a low-confidence guess returns None (report "uncertain", fall through to
# "en") instead of a wrong, misleadingly specific language code.
# ─────────────────────────────────────────────────────────────────────────────

MIN_DETECTION_CONFIDENCE = 0.7
_DETECTION_TEXT_CAP = 2000  # langid needs only a modest sample; caps event-loop stall risk

_identifier = None  # lazy singleton — building it loads py3langid's model file


def _get_identifier():
    global _identifier
    if _identifier is None:
        from py3langid.langid import LanguageIdentifier, MODEL_FILE
        _identifier = LanguageIdentifier.from_model_file(MODEL_FILE, norm_probs=True)
    return _identifier


def detect_language(text: str, min_confidence: float = MIN_DETECTION_CONFIDENCE) -> Optional[str]:
    """Best-effort ISO 639-1 guess for what language `text` is written in.

    Returns None — never a guess — when there isn't enough signal to be
    confident, or the input is empty/whitespace. Callers must treat None as
    "detection was inconclusive," not as "the language is unset"; those are
    different facts. This function does not validate its result against
    LANGUAGE_NAMES or any other fixed set — whatever py3langid's model
    returns passes straight through.
    """
    sample = (text or "").strip()
    if not sample:
        return None
    try:
        lang, prob = _get_identifier().classify(sample[:_DETECTION_TEXT_CAP])
    except Exception as exc:  # noqa: BLE001
        logger.warning("detect_language: classification failed: %s", exc)
        return None
    if prob < min_confidence:
        logger.info(
            "detect_language: below confidence threshold (%.3f < %.3f) for a %d-char sample — "
            "treating as uncertain rather than guessing %r",
            prob, min_confidence, len(sample), lang,
        )
        return None
    return lang


async def workspace_language(workspace_id: Optional[str]) -> Optional[str]:
    """The workspace's configured default language, or None if unset/missing."""
    if not workspace_id:
        return None
    try:
        doc = await workspaces.find_one({"id": workspace_id}, {"language": 1})
        return (doc or {}).get("language") or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("workspace_language: lookup failed for workspace %s: %s", workspace_id, exc)
        return None


async def user_language(user_id: Optional[str]) -> Optional[str]:
    """A user's own account default language, or None if unset/missing."""
    if not user_id:
        return None
    try:
        doc = await users.find_one({"id": user_id}, {"language": 1})
        return (doc or {}).get("language") or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("user_language: lookup failed for user %s: %s", user_id, exc)
        return None


def first_present_or_none(*values: Optional[str]) -> Optional[str]:
    """The first truthy value in precedence order, or None if none are set.

    Distinct from ``first_present`` below: this makes "no explicit
    preference found at any level" observable to the caller, rather than
    collapsing it into "en" immediately — needed wherever a lower-priority
    tier (e.g. detect_language on the request's own content) should still
    get a chance before the hardcoded "en" last resort.
    """
    for v in values:
        if v:
            return v
    return None


def first_present(*values: Optional[str]) -> str:
    """The first truthy value in precedence order, or "en" if none are set.

    "en" here is the fully-legacy fallback — not a validated default, just
    what a system has to do when literally nothing, at any level, was ever
    configured. It is never substituted for an explicitly-set value.
    """
    return first_present_or_none(*values) or "en"
