"""Brand voice applicator — rewrites content in the brand's voice."""


async def apply_brand_voice(content: str, brand_profile: dict) -> str:
    """Rewrite *content* to match the tone and vocabulary in *brand_profile*.

    Uses the brand's preferred vocabulary, sentence structure, and tone
    settings to adapt the content without changing its core meaning.

    Args:
        content: Original text content to rewrite.
        brand_profile: Dict containing tone, vocabulary, and style preferences.

    Returns:
        Content rewritten in the brand's voice.
    """
    # Placeholder: build a brand-voice prompt and call LLM to rewrite content
    return content
