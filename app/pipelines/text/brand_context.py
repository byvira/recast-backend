"""
Build the brand voice context string injected at the top of every generation prompt.
This is what makes every piece of content sound like the user, not like a generic LLM.
"""

from typing import Optional

from app.core.config import settings
from app.prompts.registry import load_prompt
from app.pipelines.text.generator import resolve_language_name


def _load_text() -> dict:
    """The goal, tone and engagement-priority instruction text, kept in app/prompts/fixtures/brand_context_text.json and not
    in code. The wording is exactly what it was when it lived here."""
    from app.prompts.registry import load_fixture

    return load_fixture("brand_context_text")

def build_brand_context(brand_profile: dict) -> str:
    """
    Convert a brand_profile document into a structured prompt context block.
    Called once per pipeline run. The result is passed to every agent task.

    Renders app/prompts/fragments/brand_context.jinja (document_shape="text_pipeline").
    """
    return load_prompt(
        "fragments/brand_context", document_shape="text_pipeline", brand_profile=brand_profile
    )


def build_goal_context(goal: Optional[str]) -> str:
    """
    Maps to ConfigPanel ContentGoalSelector.
    Injected between brand context and platform rules.
    """
    GOAL_INSTRUCTIONS = _load_text()["goal_instructions"]
    if not goal or goal == "brand":
        return ""
    instruction = GOAL_INSTRUCTIONS.get(goal, "")
    return f"{instruction}\n\n" if instruction else ""


# Row 15 — durable best-practice patterns per platform, not a claim of
# reverse-engineering an actual black-box ranking algorithm. Distinct from
# generator.py's ENGAGEMENT_PATTERNS, which is generic hook/body/closing
# craft with no platform awareness at all — this is specifically "what
# this platform's audience and format reward," on top of that. Explicitly
# tied to the piece's real content, never a generic template phrase — same
# principle hook_agent.py's anti-generic filtering already proves works.
#
# Keyed by app.models.text.Platform's real enum *values* (confirmed live:
# Blog/Newsletter/Facebook/Instagram/LinkedIn/Twitter/X/Twitter/X Thread/
# YouTube) — NOT by every publish-time platform. Threads and Bluesky are
# real publishers (app/pipelines/publish/) but have no dedicated
# has_text_prompt_rules=True entry (app/platforms/base.py), so they're not
# part of this enum at all today — content for them is generated under a
# different platform's rules and cross-posted, not generated with its own
# engagement framing. Adding one here for either would be dead code that
# never runs, not a real fix — a genuine follow-up once/if they get their
# own generation rules, not silently faked now.
_ENGAGEMENT_PRIORITY = _load_text()["engagement_priority"]


def build_engagement_context(platform: str) -> str:
    """
    Per-platform 'what actually drives engagement here' instruction.
    Empty for any platform not in _ENGAGEMENT_PRIORITY above — no fabricated
    claim for a platform this hasn't been reasoned through for yet.
    """
    instruction = _ENGAGEMENT_PRIORITY.get(platform, "")
    return f"{instruction}\n\n" if instruction else ""



# Tones that read as informal/conversational — for a non-English output
# language, real bilingual speakers naturally code-switch in registers like
# this (loanwords for modern/technical/business terms), so the model is
# explicitly told that's expected rather than defaulting to zero mixing.
_INFORMAL_TONES = {"casual", "punchy", "storytelling"}
# Tones that read as composed/considered — lean toward native vocabulary
# instead, the way a real bilingual speaker would when writing carefully.
_FORMAL_TONES = {"formal", "professional", "direct"}


def build_tone_and_terms(tone: Optional[str], language_code: str = "en") -> str:
    """The tone override plus, for a post in one non-English language under the brand's own tone, the rule that English words
    people normally say in English (brand and product names, technical and business terms) stay in English. Mixed codes
    ("ta+en") have their own mixing rules and informal or formal tones already say how English terms are handled, so this
    only fills the gap those leave: the default tone. English output gets nothing extra."""
    text = build_tone_override(tone, language_code)
    code = (language_code or "en").strip().lower().split("-")[0]
    if settings.ENGLISH_TERMS_STAY_ENGLISH and (not tone or tone == "brand") and code and code != "en" and "+" not in code:
        from app.pipelines.text.generator import resolve_language_name
        from app.prompts.registry import load_prompt

        text += load_prompt("text/generate/english_terms", language_name=resolve_language_name(language_code)) + "\n\n"
    return text


def build_tone_override(tone: Optional[str], language_code: str = "en") -> str:
    """
    Maps to ConfigPanel ToneSelector.
    Only active when tone is not 'brand'. Overrides brand voice tone for this run.

    `language_code` additionally steers HOW that tone should code-switch for
    a non-English output language — e.g. Casual+Tamil should read like real
    bilingual conversation (Tanglish), Professional+Tamil should lean toward
    composed native vocabulary instead. Without this, tone and language were
    two fully independent instruction blocks with zero interaction, so every
    non-English tone read the same regardless of which one was picked.
    """
    TONE_INSTRUCTIONS = _load_text()["tone_instructions"]
    if not tone or tone == "brand":
        return ""
    instruction = TONE_INSTRUCTIONS.get(tone, "")
    if not instruction:
        return ""

    normalised_lang = (language_code or "en").strip().lower().split("-")[0]
    # A mixed-language code (e.g. "ta+en") already gets a dedicated
    # code-switching directive from build_language_instruction()'s own
    # mixed_language_instruction.jinja — skip this addition here to avoid
    # two overlapping instructions in the same prompt.
    if normalised_lang and normalised_lang != "en" and "+" not in normalised_lang:
        name = resolve_language_name(language_code)
        if tone in _INFORMAL_TONES:
            instruction += (
                f" Natural code-switching is expected and welcome here — mix "
                f"in common English words for modern/technical/business ideas "
                f"the way a real bilingual {name} speaker actually talks "
                f"casually. Still express full ideas and sentences in {name} "
                f"though — don't paste whole English phrases or sentences, "
                f"just individual loanwords the way people naturally do."
            )
        elif tone in _FORMAL_TONES:
            instruction += (
                f" Lean toward composed, native {name} vocabulary — keep "
                f"English only for genuinely untranslatable technical or "
                f"product terms, not casual filler words."
            )

    return f"{instruction}\n\n"
