"""Lightweight pre-generation suggestions for the campaign runner form.

Same pattern as app.pipelines.text.repurpose_suggest.suggest_repurpose_targets
— a single cheap structured LLM call, no persistence, that the user can
accept or ignore. Previously the campaign wizard had no read of the brief
at all before running; this lets a real model suggestion inform the batch
topics list instead of the user guessing blind.
"""

import logging

from app.prompts.registry import load_prompt
from app.shared.llm import call_llm_structured

logger = logging.getLogger(__name__)


async def suggest_campaign_topics(
    topic_cluster: str,
    brand_context: str,
    existing_topics: list[str],
) -> dict:
    """Suggest additional angles/topics for a campaign brief.

    Read-only — never persists anything. Returns an empty-ish suggestion
    rather than raising if the LLM comes back unusable, so a suggestion
    failure never blocks filling the batch topics list manually, the way
    the user always could before.
    """
    prompt = load_prompt(
        "campaigns/suggest_topics",
        brand_context=brand_context,
        topic_cluster=topic_cluster[:3000],
        existing_topics=existing_topics[:10],
    )
    result = await call_llm_structured(prompt, max_tokens=600)

    if not result or not result.get("suggested_topics"):
        logger.warning("Campaign topic suggestion returned nothing usable")
        return {
            "suggested_topics": [],
            "suggested_tone": None,
            "rationale": "",
        }

    result["suggested_topics"] = [
        t for t in result["suggested_topics"] if isinstance(t, str) and t.strip()
    ][:3]
    return result
