"""
Build the brand voice context string injected at the top of every generation prompt.
This is what makes every piece of content sound like the user, not like a generic LLM.
"""

from typing import Optional

from app.prompts.registry import load_prompt
from app.pipelines.text.generator import resolve_language_name


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
    GOAL_INSTRUCTIONS = {
        "educate": "CONTENT GOAL: Educate the audience. Lead with a clear insight. Structure for clarity and comprehension. End with a concrete takeaway they can apply immediately.",
        "promote": "CONTENT GOAL: Promote a product, service, or idea. Lead with the primary benefit. Build desire through specificity. End with a direct, friction-free CTA.",
        "entertain": "CONTENT GOAL: Entertain. Use an unexpected angle, wit, or a compelling story. Keep it light but on-brand. The reader should enjoy reading this.",
        "inspire": "CONTENT GOAL: Inspire. Share a transformation, belief, or hard-won lesson. Make the reader feel something. End with a statement that lingers.",
        "announce": "CONTENT GOAL: Announce something new. Be clear and direct. State what it is, why it matters, and what happens next. No fluff.",
        "engage": "CONTENT GOAL: Drive engagement. Invite a response, spark a debate, or ask a question the reader feels compelled to answer. The content exists to start a conversation.",
        "convert": "CONTENT GOAL: Convert. Every sentence builds toward a single action. Remove anything that distracts from the CTA. Be specific about what the reader should do next.",
    }
    if not goal or goal == "brand":
        return ""
    instruction = GOAL_INSTRUCTIONS.get(goal, "")
    return f"{instruction}\n\n" if instruction else ""



# Tones that read as informal/conversational — for a non-English output
# language, real bilingual speakers naturally code-switch in registers like
# this (loanwords for modern/technical/business terms), so the model is
# explicitly told that's expected rather than defaulting to zero mixing.
_INFORMAL_TONES = {"casual", "punchy", "storytelling"}
# Tones that read as composed/considered — lean toward native vocabulary
# instead, the way a real bilingual speaker would when writing carefully.
_FORMAL_TONES = {"formal", "professional", "direct"}


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
    TONE_INSTRUCTIONS = {
        "formal": "TONE OVERRIDE (this run only): Write in a formal, composed register. Structured, precise, authoritative phrasing.",
        "casual": "TONE OVERRIDE (this run only): Write in a casual, conversational tone. Like a knowledgeable friend talking, not presenting.",
        "punchy": "TONE OVERRIDE (this run only): Write punchy. Short sentences. Bold statements. Cut every word that doesn't pull its weight. High energy.",
        "storytelling": "TONE OVERRIDE (this run only): Use narrative storytelling. Open with a scene or moment. Build through the piece. Make it personal and specific.",
        "professional": "TONE OVERRIDE (this run only): Write with a professional, polished register — credible and composed, like a skilled practitioner speaking plainly to a peer. Avoid corporate jargon, buzzwords, and empty formal filler. Confident and clear, not stiff.",
        "direct": "TONE OVERRIDE (this run only): Say exactly what you mean, plainly and literally. No metaphors, no hedging, no flourish. Short, concrete statements the reader can act on immediately.",
    }
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
