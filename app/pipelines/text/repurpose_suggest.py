"""
Lightweight pre-generation suggestions for the repurpose flow.

New repurpose flow — a real "AI Suggestions" step between pasting content
and picking target platforms. Quick Recast used to jump straight from a
raw paste to blind platform checkboxes with no read of the content at
all. This is a single cheap structured LLM call (no persistence, no full
rewrite — that's still /repurpose's job) suggesting which platforms best
fit the content and what tone/angle to lead with, which the user can
accept or override.
"""

import logging

from app.models.text import Platform
from app.pipelines.text.generator import build_language_instruction
from app.prompts.registry import load_prompt
from app.shared.language import detect_language
from app.shared.llm import call_llm_structured

logger = logging.getLogger(__name__)

# Matches the channels Quick Recast actually offers as generation targets —
# no point suggesting a platform the picker can't select.
SUGGESTABLE_PLATFORMS = [
    Platform.LINKEDIN,
    Platform.TWITTER,
    Platform.INSTAGRAM,
    Platform.FACEBOOK,
]


async def suggest_repurpose_targets(
    content: str,
    source_platform: Platform,
    brand_context: str,
) -> dict:
    """Suggest target platforms + tone/angle for repurposing `content`.

    Read-only — never persists anything. Returns an empty-ish suggestion
    (empty suggested_platforms) rather than raising if the LLM comes back
    unusable, so a suggestion failure never blocks the user from just
    picking platforms manually, the way they always could before.
    """
    language_instruction = build_language_instruction(detect_language(content) or "en")
    candidates = [p.value for p in SUGGESTABLE_PLATFORMS if p != source_platform]

    prompt = load_prompt(
        "text/repurpose/suggest",
        brand_context=brand_context,
        source_platform=source_platform.value,
        candidate_platforms=candidates,
        content=content[:3000],
        language_instruction=language_instruction,
    )
    result = await call_llm_structured(prompt, max_tokens=600)

    if not result or not result.get("suggested_platforms"):
        logger.warning("Repurpose-suggest returned nothing usable — falling back to manual picking")
        return {
            "suggested_platforms": [],
            "rationale": "",
            "suggested_tone": None,
            "suggested_angle": None,
        }

    # Never trust the LLM's platform strings blindly — keep only ones that
    # are both real Platform values and actually offered as candidates.
    valid = {p.value for p in SUGGESTABLE_PLATFORMS}
    result["suggested_platforms"] = [
        p for p in result["suggested_platforms"] if p in valid and p != source_platform.value
    ][:2]
    return result
