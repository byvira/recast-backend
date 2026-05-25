"""
Build the brand voice context string injected at the top of every generation prompt.
This is what makes every piece of content sound like the user, not like a generic LLM.
"""

from typing import Optional


def build_brand_context(brand_profile: dict) -> str:
    """
    Convert a brand_profile document into a structured prompt context block.
    Called once per pipeline run. The result is passed to every agent task.
    """
    lines = [
        "=== BRAND VOICE — FOLLOW EXACTLY ===",
        "These instructions override all defaults. Apply every rule without exception.",
        "",
    ]

    identity = brand_profile.get("identity", {})
    brand_type = brand_profile.get("brand_type", "Person")

    if identity:
        if brand_type == "Person":
            if identity.get("name"):
                lines.append(f"Writing on behalf of: {identity['name']}")
            if identity.get("profession"):
                lines.append(f"Profession: {identity['profession']}")
            if identity.get("bio"):
                lines.append(f"Background: {identity['bio']}")
        elif brand_type == "Business":
            if identity.get("companyName"):
                lines.append(f"Company: {identity['companyName']}")
            if identity.get("description"):
                lines.append(f"What they do: {identity['description']}")
            if identity.get("industry"):
                lines.append(f"Industry: {identity['industry']}")
        elif brand_type == "Personal Brand":
            if identity.get("name"):
                lines.append(f"Personal brand: {identity['name']}")
            if identity.get("tagline"):
                lines.append(f"Tagline: {identity['tagline']}")
            if identity.get("mission"):
                lines.append(f"Mission: {identity['mission']}")
        elif brand_type == "Product":
            if identity.get("productName"):
                lines.append(f"Product: {identity['productName']}")
            if identity.get("description"):
                lines.append(f"What it does: {identity['description']}")
        lines.append("")

    audience = brand_profile.get("audience", {})
    if audience:
        if audience.get("reading_level"):
            lines.append(f"Audience reading level: {audience['reading_level']}")
        if audience.get("knowledge_base"):
            lines.append(f"Audience knowledge level: {audience['knowledge_base']}")
        if audience.get("primary_pain_point"):
            lines.append(f"Audience pain point: {audience['primary_pain_point']}")
        for field, label in [
            ("interests", "Audience interests"),
            ("goals", "Audience goals"),
            ("buyingMotivations", "Buying motivations"),
            ("valueMetrics", "Success metrics"),
        ]:
            if audience.get(field):
                lines.append(f"{label}: {audience[field]}")
        lines.append("")

    voice_tone = brand_profile.get("voice_tone", {})
    if voice_tone:
        if voice_tone.get("tones"):
            lines.append(f"Tone: {', '.join(voice_tone['tones'])}")
        if voice_tone.get("humor"):
            lines.append(f"Humor: {voice_tone['humor']}")
        if voice_tone.get("emoji"):
            lines.append(f"Emoji: {voice_tone['emoji']}")
        if voice_tone.get("style"):
            lines.append(f"Style directives: {voice_tone['style']}")
        lines.append("")

    manual_data = brand_profile.get("manual_data", {})
    if manual_data:
        openers = manual_data.get("openers", [])
        closers = manual_data.get("closers", [])
        phrases = manual_data.get("phrases", [])
        banned = manual_data.get("banned_words") or manual_data.get("bannedWords") or []
        synonyms = manual_data.get("preferred_synonyms") or manual_data.get("preferredSynonyms") or []
        
        if openers:
            lines.append("Opener patterns (use these styles, not word-for-word):")
            for o in openers[:3]:
                lines.append(f"  - {o}")
            lines.append("")

        if closers:
            lines.append("Closer patterns (use these styles, not word-for-word):")
            for c in closers[:3]:
                lines.append(f"  - {c}")
            lines.append("")

        if phrases:
            hook_phrases = [p["text"] for p in phrases if p.get("placement") == "hook"]
            transition_phrases = [p["text"] for p in phrases if p.get("placement") == "transition"]
            any_phrases = [p["text"] for p in phrases if p.get("placement") == "any"]
            if hook_phrases:
                lines.append(f"Hook phrases: {', '.join(hook_phrases[:3])}")
            if transition_phrases:
                lines.append(f"Transition phrases: {', '.join(transition_phrases[:3])}")
            if any_phrases:
                lines.append(f"Signature phrases: {', '.join(any_phrases[:3])}")
            lines.append("")

        if banned:
            lines.append(f"NEVER use these words (banned): {', '.join(banned)}")
            lines.append("")

        if synonyms:
            valid = [(s["original"], s["replacement"]) for s in synonyms if s.get("original") and s.get("replacement")]
            if valid:
                lines.append("Preferred word substitutions:")
                for orig, repl in valid:
                    lines.append(f"  - '{orig}' → '{repl}'")
                lines.append("")

    lines.append("=== END BRAND VOICE ===")
    lines.append("")
    return "\n".join(lines)


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
