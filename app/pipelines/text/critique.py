"""A second look at a finished draft before it is accepted.

A separate model call (not the one that wrote the draft) scores it on a fixed rubric: specificity, grounding, audience
fit, voice match and platform fit. A weak draft comes back as a short list of concrete fixes, which the existing
rewrite step uses as feedback. A failed or unreadable review never blocks a post.
"""
from __future__ import annotations

import logging
from typing import Optional

from app.core.config import settings
from app.models.text import Platform
from app.prompts.registry import load_prompt
from app.shared.llm import GroqModel, call_llm_structured

logger = logging.getLogger(__name__)

DIMENSIONS = ("specificity", "grounding", "audience_fit", "voice_match", "platform_fit")
_SKIP_PLATFORMS = {Platform.TWITTER}
_MIN_WORDS = 40
_PASS_AVERAGE = 3.6


def should_review(platform: Platform, content: str) -> bool:
    return bool(settings.TEXT_CRITIQUE_ENABLED) and platform not in _SKIP_PLATFORMS and len((content or "").split()) >= _MIN_WORDS


def _verdict(result: dict) -> Optional[list[str]]:
    """The fixes to apply, or None when the draft is good enough or the reply cannot be read."""
    scores = result.get("scores") if isinstance(result, dict) else None
    if not isinstance(scores, dict):
        return None
    values = []
    for key in DIMENSIONS:
        try:
            values.append(float(scores[key]))
        except (KeyError, TypeError, ValueError):
            return None
    weak = min(values) <= 2 or sum(values) / len(values) < _PASS_AVERAGE
    fixes = [str(f).strip() for f in (result.get("fixes") or []) if str(f).strip()][:3]
    if not weak or not fixes:
        return None
    return fixes


async def review_draft(
    *, draft: str, platform: Platform, platform_label: str, brand_context: str, source_content: str, language_line: str,
) -> Optional[list[str]]:
    """Concrete fixes for a weak draft, or None. Never raises."""
    try:
        prompt = load_prompt(
            "text/critique/judge",
            draft=draft[:6000], platform_label=platform_label, brand_context=brand_context[:6000],
            source_content=(source_content or "")[:4000], language_line=language_line,
        )
        result = await call_llm_structured(prompt, model=GroqModel.FAST, max_tokens=900)
        return _verdict(result)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Draft review failed, accepting the draft: %s", exc)
        return None
