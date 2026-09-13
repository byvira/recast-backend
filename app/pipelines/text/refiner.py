"""
Refinement chat — iterative content improvement via conversation.

Each turn takes the full message history and returns refined content.
Brand context and banned words injected as system prompt on every turn.
Version saved to MongoDB automatically if piece_id is provided.

The system prompt is rebuilt on every turn to ensure brand enforcement
is always the most recent context the model reads before generating.
"""

import logging
from app.prompts.registry import load_prompt
from app.shared.llm import call_llm_chat, GroqModel

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# SYSTEM PROMPT BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def build_refinement_system(
    brand_context: str,
    platform: str,
    banned_words: list[str] = [],
) -> str:
    """
    Build the system prompt for refinement chat.
    Rebuilt on every turn — brand context always fresh.

    Renders app/prompts/text/refine/system.jinja, which embeds
    app/prompts/text/refine/system_prefix.jinja.
    """
    prefix = load_prompt("text/refine/system_prefix")
    return load_prompt(
        "text/refine/system", prefix=prefix, brand_context=brand_context,
        banned_words=banned_words, platform=platform,
    )




async def run_refinement_turn(
    messages: list[dict[str, str]],
    brand_context: str,
    platform: str,
    banned_words: list[str] = [],
) -> str:
    """
    Execute one refinement turn with full conversation history.

    Args:
        messages:      full conversation history including current user message
                       [{"role": "user/assistant", "content": "..."}]
        brand_context: full brand voice context string from build_brand_context()
        platform:      target platform — LinkedIn, Instagram etc
        banned_words:  brand banned words list

    Returns:
        refined content string — the assistant's response
    """
    system = build_refinement_system(brand_context, platform, banned_words)
    return await call_llm_chat(messages, system=system, model=GroqModel.BALANCED)