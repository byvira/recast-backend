"""Real preview-rewrite for My Voices' Playground tab.

The Playground tab used to show a hardcoded 98.2% "tone match" for any
input, backed by nothing real — it was replaced with an honest "Coming
soon" placeholder rather than keep faking a score. This is the real
thing: one structured LLM call rewriting arbitrary sample text in the
brand's actual voice (identity/audience/voice_tone, same context
suggest_voice_patterns already summarises), with a per-input tone-match
estimate instead of a constant. Read-only — never persists anything.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from app.models.text import strip_em_dashes
from app.pipelines.brand.voice_suggestions import (
    _audience_summary,
    _identity_summary,
    _tone_summary,
)
from app.prompts.registry import load_prompt
from app.shared.llm import GroqModel, call_llm_structured

logger = logging.getLogger(__name__)


async def preview_rewrite_in_voice(
    brand_profile: dict, sample_text: str
) -> Optional[dict[str, Any]]:
    """Rewrite `sample_text` in `brand_profile`'s voice.

    Returns None (never raises) on an outright LLM failure or an unusable
    result — the caller turns that into a friendly "couldn't generate,
    try again" response rather than a raw 500.
    """
    brand_type = brand_profile.get("brand_type", "Person")
    identity_line = _identity_summary(brand_type, brand_profile.get("identity") or {})
    audience_line = _audience_summary(brand_profile.get("audience") or {})
    tone_line = _tone_summary(brand_profile.get("voice_tone") or {})
    style = (brand_profile.get("voice_tone") or {}).get("style", "")

    prompt = load_prompt(
        "brand/preview_rewrite",
        brand_type=brand_type,
        identity_line=identity_line,
        audience_line=audience_line,
        tone_line=tone_line,
        style=style,
        sample_text=sample_text[:2000],
    )

    try:
        result = await call_llm_structured(
            prompt=prompt,
            system=(
                "You are a skilled ghostwriter rewriting text in a specific "
                "brand's voice. Return JSON only."
            ),
            model=GroqModel.BALANCED,
            max_tokens=1200,
        )
    except Exception:  # noqa: BLE001 - never let a preview feature 500
        logger.exception("voice preview-rewrite raised unexpectedly")
        return None

    if not result or not str(result.get("rewritten", "")).strip():
        return None

    score = result.get("tone_match_score", 0)
    try:
        score = max(0, min(100, int(score)))
    except (TypeError, ValueError):
        score = 0

    return {
        "rewritten": strip_em_dashes(str(result["rewritten"])),
        "tone_match_score": score,
    }
