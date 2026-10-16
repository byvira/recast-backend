"""A reader's honest reaction to a post, asked for by the writer while writing.

A separate model call reads the post as one member of the brand's audience and answers in a fixed shape: what they took away, whether it works,
what helped, what to change and what they still wonder. It never rewrites the post and never changes anything: it is advice the writer chooses
to act on. Whatever the model sends back is tidied to that shape before it is returned, so the page can always draw it.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.prompts.registry import load_prompt
from app.shared.llm import GroqModel, call_llm_structured

logger = logging.getLogger(__name__)

VERDICTS = ("ready", "almost", "needs_work")
MIN_WORDS = 5
MAX_CHARS = 8000
_LIMITS = {"headline": 240, "strength": 200, "where": 120, "problem": 280, "suggestion": 280, "question": 200}


def _clip(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit].rstrip()


def tidy(result: Any) -> Optional[dict]:
    """The reply in the shape the page draws, or None when it holds nothing a reader could use."""
    if not isinstance(result, dict):
        return None
    headline = _clip(result.get("headline"), _LIMITS["headline"])
    verdict = str(result.get("verdict") or "").strip().lower().replace(" ", "_")
    strengths = [s for s in (_clip(x, _LIMITS["strength"]) for x in (result.get("strengths") or [])[:3]) if s]
    fixes = []
    for f in (result.get("fixes") or [])[:4]:
        if not isinstance(f, dict):
            continue
        problem, suggestion = _clip(f.get("problem"), _LIMITS["problem"]), _clip(f.get("suggestion"), _LIMITS["suggestion"])
        if problem or suggestion:
            fixes.append({"where": _clip(f.get("where"), _LIMITS["where"]), "problem": problem, "suggestion": suggestion})
    questions = [q for q in (_clip(x, _LIMITS["question"]) for x in (result.get("questions") or [])[:3]) if q]
    if not headline and not fixes and not strengths:
        return None
    if verdict not in VERDICTS:
        # a missing or odd verdict is worked out from what the reader said, so the page never shows a made-up one
        verdict = "ready" if not fixes else "almost" if len(fixes) <= 2 else "needs_work"
    return {"headline": headline, "verdict": verdict, "strengths": strengths, "fixes": fixes, "questions": questions}


async def review(*, content: str, platform_label: str, brand_context: str, language_line: str) -> Optional[dict]:
    """The reader's reaction, or None when the review could not be made. Never raises."""
    try:
        prompt = load_prompt(
            "text/reader/review", content=content[:MAX_CHARS], platform_label=platform_label,
            brand_context=brand_context[:6000], language_line=language_line,
        )
        return tidy(await call_llm_structured(prompt, model=GroqModel.BALANCED, max_tokens=1200))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Reader review failed: %s", exc)
        return None
