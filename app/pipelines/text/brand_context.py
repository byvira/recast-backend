"""
Build the brand voice context string injected at the top of every generation prompt.
This is what makes every piece of content sound like the user, not like a generic LLM.
"""

from typing import Optional

from app.prompts.registry import load_prompt


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


def build_tone_override(tone: Optional[str]) -> str:
    """
    Maps to ConfigPanel ToneSelector.
    Only active when tone is not 'brand'. Overrides brand voice tone for this run.
    """
    TONE_INSTRUCTIONS = {
        "formal": "TONE OVERRIDE (this run only): Write in a formal, professional tone. Complete sentences. No contractions. Structured and authoritative.",
        "casual": "TONE OVERRIDE (this run only): Write in a casual, conversational tone. Contractions welcome. Like a knowledgeable friend talking, not presenting.",
        "punchy": "TONE OVERRIDE (this run only): Write punchy. Short sentences. Bold statements. Cut every word that doesn't pull its weight. High energy.",
        "storytelling": "TONE OVERRIDE (this run only): Use narrative storytelling. Open with a scene or moment. Build through the piece. Make it personal and specific.",
    }
    if not tone or tone == "brand":
        return ""
    instruction = TONE_INSTRUCTIONS.get(tone, "")
    return f"{instruction}\n\n" if instruction else ""
