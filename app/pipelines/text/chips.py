"""
Quick action chips — predefined one-click content refinement.

Each chip maps a user-facing action name to a specific rewrite instruction.
Chips are brand-aware — banned words enforced, brand voice preserved.
Chips are platform-aware — available chips differ per platform.

Usage:
  POST /api/v1/text/refine
  Body: { content, chip, platform, brand_id }
  Returns: { original, refined, chip, platform, word_count, char_count }
"""

import logging
from app.shared.llm import call_llm, GroqModel

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# CHIP DEFINITIONS
# ─────────────────────────────────────────────────────────────────────────────

CHIP_PROMPTS: dict[str, str] = {

    # ── Universal chips — work on any platform ────────────────────────────

    "make_punchier": (
        "Rewrite this content with shorter sentences. "
        "Cut every word that does not pull its weight. "
        "Bold statements. High energy. No filler. "
        "Maximum 2 sentences per paragraph. "
        "Do not add new ideas — sharpen what is already there."
    ),

    "add_story": (
        "Add a specific personal story or real example to this content. "
        "Use a named person, a specific number, or a real moment with a date or timeframe. "
        "The story must illustrate the main point concretely. "
        "Weave it into the existing content — do not just append it at the end. "
        "No invented statistics."
    ),

    "shorten": (
        "Cut this content to approximately half its current length. "
        "Keep only the strongest sentences — the ones with specific details or real tension. "
        "Every removed sentence should disappear entirely — not be summarised. "
        "Never cut the hook or the closing. Cut from the middle."
    ),

    "add_cta": (
        "Add one strong specific CTA at the very end of this content. "
        "The CTA must be specific to this content — not generic. "
        "Not 'what do you think' or 'let me know in the comments'. "
        "Ask a specific question tied to the content's main point, "
        "or direct to a specific action the reader can take today."
    ),

    "more_casual": (
        "Rewrite in a more conversational friendly tone. "
        "Use contractions. Shorter sentences. "
        "Sound like a knowledgeable friend talking — not a company presenting. "
        "Keep all the specific details and numbers."
    ),

    "more_formal": (
        "Rewrite in a more formal professional tone. "
        "Complete sentences. No contractions. "
        "Structured and authoritative. "
        "Keep all the specific details and numbers."
    ),

    "add_numbers": (
        "Add at least 2 specific numbers or concrete data points to this content. "
        "Replace vague claims with specific outcomes, timeframes, or quantities. "
        "Use only numbers that can be derived from the source content or brand context. "
        "Do not invent statistics. "
        "Examples: replace 'many brands' with '11 brands', "
        "replace 'took a long time' with 'took 4 hours every Sunday'."
    ),

    "stronger_hook": (
        "Rewrite ONLY the opening line to be more specific and scroll-stopping. "
        "Use the uncomfortable truth or specific outcome pattern. "
        "Keep the rest of the content completely unchanged. "
        "The new hook must not start with: Are you, Many people, Most people, "
        "In today's world, We all know, Did you know, Imagine, Picture this. "
        "Two punchy sentences maximum."
    ),

    "fix_weasel_words": (
        "Find and replace all vague words in this content. "
        "Replace: many → specific number, "
        "several → specific number, "
        "often → specific frequency, "
        "recently → specific timeframe, "
        "significant → specific metric, "
        "various → list the actual items. "
        "If the real number is not known, restructure the sentence to avoid needing a number. "
        "Never invent statistics."
    ),

    # ── Platform-specific chips ───────────────────────────────────────────

    "add_hashtags": (
        "Add relevant brand-appropriate hashtags at the very end of this content. "
        "Use brand vocabulary and topic-specific terms. "
        "Never use generic hashtags like #motivation #success #hustle. "
        "LinkedIn: exactly 3 hashtags on a new line. "
        "Instagram: 8-10 hashtags after two blank lines. "
        "Twitter: 1 hashtag maximum only if it adds genuine context."
    ),

    "add_timestamps": (
        "Add a timestamps section at the end of this content. "
        "Format each timestamp as: 00:00 Section Name. "
        "Create 4-6 logical sections based on the content structure. "
        "Start at 00:00. Space sections approximately evenly. "
        "Add [ADJUST TIMESTAMPS] note after the section."
    ),

    "simplify_show_notes": (
        "Rewrite this content as clean podcast show notes. "
        "Remove complex sentences. Short paragraphs of 2-3 sentences. "
        "Scannable structure with clear section breaks. "
        "End with a specific listener takeaway or action."
    ),

    "add_keywords": (
        "Add 3-5 relevant SEO keywords naturally into this content. "
        "Keywords must fit the sentence structure — never forced or awkward. "
        "Do not list keywords separately. "
        "Weave them into existing sentences where they read naturally."
    ),

    "expand": (
        "Expand this content with more specific detail and depth. "
        "Add concrete examples, specific numbers, or a real story. "
        "Do not pad with generic observations. "
        "Every added sentence must contain a specific detail "
        "that only applies to this brand and this content. "
        "Target 50% more words than the original."
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# PLATFORM CHIP AVAILABILITY
# ─────────────────────────────────────────────────────────────────────────────

PLATFORM_CHIPS: dict[str, list[str]] = {
    "LinkedIn": [
        "make_punchier", "add_story", "shorten", "add_cta",
        "stronger_hook", "add_numbers", "fix_weasel_words",
        "more_casual", "more_formal", "expand",
    ],
    "Twitter/X": [
        "make_punchier", "shorten", "stronger_hook", "add_numbers",
    ],
    "Twitter/X Thread": [
        "make_punchier", "add_story", "shorten", "add_cta",
        "add_numbers", "fix_weasel_words",
    ],
    "Instagram": [
        "more_casual", "add_cta", "add_hashtags", "add_story",
        "stronger_hook", "shorten",
    ],
    "Facebook": [
        "more_casual", "add_story", "add_cta", "expand",
        "fix_weasel_words",
    ],
    "Blog": [
        "add_story", "add_numbers", "add_keywords", "shorten",
        "expand", "fix_weasel_words", "add_cta",
    ],
    "Newsletter": [
        "more_casual", "add_story", "add_cta", "shorten",
        "expand", "fix_weasel_words",
    ],
    "YouTube": [
        "add_keywords", "add_timestamps", "shorten",
        "stronger_hook", "add_cta",
    ],
    "show_notes": [
        "simplify_show_notes", "add_cta", "shorten",
        "add_timestamps", "add_numbers",
    ],
    "video_description": [
        "add_keywords", "add_timestamps", "shorten", "stronger_hook",
    ],
    "image_caption": [
        "more_casual", "add_cta", "add_hashtags",
        "stronger_hook", "shorten",
    ],
}

# Chips available on all platforms regardless of mapping
UNIVERSAL_CHIPS = [
    "make_punchier", "add_story", "shorten", "add_cta",
    "more_casual", "more_formal", "add_numbers",
    "stronger_hook", "fix_weasel_words", "expand",
]


def get_chips_for_platform(platform: str) -> list[str]:
    """
    Return available chip names for a platform.
    Falls back to universal chips if platform not in mapping.
    """
    return PLATFORM_CHIPS.get(platform, UNIVERSAL_CHIPS)


# ─────────────────────────────────────────────────────────────────────────────
# CHIP APPLICATION
# ─────────────────────────────────────────────────────────────────────────────

async def apply_chip(
    content: str,
    chip_name: str,
    platform: str,
    brand_context: str,
    banned_words: list[str] = [],
) -> dict:
    """
    Apply a quick action chip to existing content.

    Args:
        content:       the content to refine
        chip_name:     which chip to apply — must be in CHIP_PROMPTS
        platform:      target platform — affects formatting rules
        brand_context: full brand voice context string
        banned_words:  list of words that must not appear in output

    Returns:
        {
          original:    original content unchanged
          refined:     refined content after chip applied
          chip:        chip name applied
          platform:    platform
          word_count:  refined content word count
          char_count:  refined content char count
          changed:     True if refined differs from original
        }
    """
    instruction = CHIP_PROMPTS.get(chip_name)
    if not instruction:
        logger.warning("Unknown chip: %s — returning original", chip_name)
        return {
            "original": content,
            "refined": content,
            "chip": chip_name,
            "platform": platform,
            "word_count": len(content.split()),
            "char_count": len(content),
            "changed": False,
            "error": f"Unknown chip: {chip_name}",
        }

    # Build banned words instruction
    banned_block = ""
    if banned_words:
        banned_list = ", ".join(f"'{w}'" for w in banned_words)
        banned_block = (
            f"\nBANNED WORDS — never use these in the refined content: {banned_list}\n"
            f"If you are about to write a banned word — STOP and use brand vocabulary instead.\n"
        )

    prompt = f"""
{brand_context}
{banned_block}
PLATFORM: {platform}

REFINEMENT INSTRUCTION:
{instruction}

RULES:
- Apply the instruction above faithfully
- Preserve the brand voice exactly — same tone, same vocabulary
- Never introduce banned words
- Never add generic filler sentences
- Return only the refined content — no explanation, no JSON, no preamble

ORIGINAL CONTENT:
{content}

Output the refined content only.
"""

    refined = await call_llm(prompt, model=GroqModel.BALANCED)
    refined = refined.strip()

    return {
        "original": content,
        "refined": refined,
        "chip": chip_name,
        "platform": platform,
        "word_count": len(refined.split()),
        "char_count": len(refined),
        "changed": refined != content,
    }