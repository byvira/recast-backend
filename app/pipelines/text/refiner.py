"""
Refinement chat — iterative content improvement via conversation.

Each turn takes the full message history and returns refined content.
Brand context and banned words injected as system prompt on every turn.
Version saved to MongoDB automatically if piece_id is provided.

The system prompt is rebuilt on every turn to ensure brand enforcement
is always the most recent context the model reads before generating.
"""

import logging
from app.shared.llm import call_llm_chat, GroqModel

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# SYSTEM PROMPT BUILDER
# ─────────────────────────────────────────────────────────────────────────────

REFINEMENT_SYSTEM_PREFIX = """
You are refining content for a specific brand.
Your job is to apply the user's refinement instruction while keeping
the brand voice, platform format, and content specificity intact.

RULES — apply on every turn:
  ✓ Keep all specific numbers, real stories, and brand facts
  ✓ Never introduce vague generalisations
  ✓ Preserve the platform format — LinkedIn stays LinkedIn
  ✓ Return only the refined content — no explanation, no preamble, no JSON
  ✓ Use the brand's exact story: numbers, names, real moments
  ✗ Never add generic motivational language: "game-changer", "save your sanity",
    "never worry again", "transform", "revolutionize"
  ✗ Never introduce banned words
  ✗ Never lose the hook or the closing unless explicitly asked
  ✗ Never add empathy preamble: "I've been where you are",
    "I know how you feel", "You're not alone"
  ✗ Never use adjectives without evidence:
    "game-changer" → "Four hours every two weeks beats thirty minutes every day"
    "powerful system" → "system that ran 11 brands without burnout"
"""

def build_refinement_system(
    brand_context: str,
    platform: str,
    banned_words: list[str] = [],
) -> str:
    """
    Build the system prompt for refinement chat.
    Rebuilt on every turn — brand context always fresh.
    """
    banned_block = ""
    if banned_words:
        banned_list = ", ".join(f"'{w}'" for w in banned_words)
        banned_block = (
            f"\nBANNED WORDS — never use any of these: {banned_list}\n"
            f"If you are about to write a banned word — STOP and rephrase.\n"
        )

    return (
        f"{REFINEMENT_SYSTEM_PREFIX}\n\n"
        f"{brand_context}\n"
        f"{banned_block}\n"
        f"Platform: {platform}\n"
        f"Return only the refined content. No explanation. No preamble."
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