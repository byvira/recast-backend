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
from typing import Optional
from app.prompts.registry import load_prompt
from app.pipelines.text.brand_context import build_tone_override
from app.pipelines.text.generator import build_language_instruction
from app.shared.llm import call_llm, GroqModel

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# CHIP DEFINITIONS
# ─────────────────────────────────────────────────────────────────────────────

# One .jinja file per chip under app/prompts/text/refine/chips/. CHIP_PROMPTS
# stays a real dict (not just a set of names) because app/api/v1/text.py reads
# both its keys (validating a requested chip) and its values (the instruction
# text, recorded in version history when a chip is applied).
_CHIP_NAMES = (
    # Universal chips — work on any platform
    "make_punchier", "add_story", "shorten", "add_cta", "more_casual",
    "more_formal", "add_numbers", "stronger_hook", "fix_weasel_words",
    # Platform-specific chips
    "add_hashtags", "add_timestamps", "simplify_show_notes", "add_keywords", "expand",
)

CHIP_PROMPTS: dict[str, str] = {
    name: load_prompt(f"text/refine/chips/{name}") for name in _CHIP_NAMES
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
    custom_instruction: Optional[str] = None,
    language: str = "en",
    default_tone: Optional[str] = None,
) -> dict:
    """
    Apply a quick action chip to existing content.

    Args:
        content:            the content to refine
        chip_name:          which chip to apply — looked up in CHIP_PROMPTS,
                             unless custom_instruction is given, in which
                             case this is just a display label (any string)
        platform:           target platform — affects formatting rules
        brand_context:      full brand voice context string
        banned_words:       list of words that must not appear in output
        custom_instruction: a user-authored instruction (Feature 5) — used
                             as-is instead of looking chip_name up in the
                             fixed CHIP_PROMPTS set
        language:           resolved via _resolve_request_language() by the
                             caller — refine used to have no language
                             awareness at all, silently defaulting to
                             whatever language the LLM felt like regardless
                             of the piece's/workspace's real language
        default_tone:       the brand's persistent default tone (My Voices >
                             Calibration), if set — chip refinement had no
                             tone-override mechanism at all before this;
                             chips themselves (more_casual/more_formal) stay
                             the per-run tone tool, this only fills in the
                             brand's own standing default the same way
                             generation/regenerate already do

    Returns:
        {
          original:    original content unchanged
          refined:     refined content after chip applied
          chip:        chip name/label applied
          platform:    platform
          word_count:  refined content word count
          char_count:  refined content char count
          changed:     True if refined differs from original
        }
    """
    instruction = custom_instruction or CHIP_PROMPTS.get(chip_name)
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

    prompt = load_prompt(
        "text/refine/apply_chip",
        brand_context=brand_context,
        banned_words=banned_words,
        platform=platform,
        instruction=instruction,
        content=content,
        language_instruction=build_language_instruction(language),
        tone_override=build_tone_override(default_tone, language),
    )

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