"""PAR-015 fix — real extraction for My Voices' Training tab.

`extracted_traits` was always persisted as an empty list (see
TrainingSample's docstring in app/models/brand_profile.py): the original
mock showed 3 fixed fake traits for every sample regardless of content, and
the honest empty list deliberately replaced that rather than keep faking
it. This is the real thing: one structured LLM call reading the actual
sample text for specific, verifiable stylistic observations — never a
fixed/generic set.
"""

from __future__ import annotations

import logging

from app.prompts.registry import load_prompt
from app.shared.llm import GroqModel, call_llm_structured, set_usage_workspace

logger = logging.getLogger(__name__)

_MAX_TRAITS = 5
_MAX_TRAIT_LEN = 60


async def extract_sample_traits(content: str, *, workspace_id: str = "") -> list[str]:
    """Extract 0-5 concrete stylistic traits from a training sample.

    Returns [] (never raises) on an outright LLM failure or an unusable
    result — a failed extraction should never block saving the sample
    itself, same principle as voice_playground.py's preview-rewrite.
    """
    set_usage_workspace(workspace_id)  # PAR-012

    prompt = load_prompt("brand/extract_traits", content=content[:3000])

    try:
        result = await call_llm_structured(
            prompt=prompt,
            system=(
                "You are a sharp editor identifying specific, verifiable "
                "stylistic traits in a piece of writing. Return JSON only."
            ),
            model=GroqModel.FAST,
            max_tokens=400,
        )
    except Exception:  # noqa: BLE001 - never let this block saving the sample
        logger.exception("training sample trait extraction raised unexpectedly")
        return []

    raw_traits = result.get("traits") if result else None
    if not isinstance(raw_traits, list):
        return []

    traits: list[str] = []
    for t in raw_traits:
        trait = str(t).strip()
        if trait and len(trait) <= _MAX_TRAIT_LEN:
            traits.append(trait)
        if len(traits) >= _MAX_TRAITS:
            break
    return traits
