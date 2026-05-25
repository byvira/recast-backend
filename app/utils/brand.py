def build_brand_context(brand: dict, pipeline: str) -> str:
    """
    Build a system prompt string from the brand profile dict.
    Injected as system message into every LLM call.

    Args:
        brand: brand profile dict from MongoDB brand_profiles collection
        pipeline: which pipeline is calling (text/audio/video/image)

    Returns:
        Formatted system prompt string with brand voice instructions
    """
    name = brand.get("name", "")
    tone = brand.get("voice", {}).get("tone", "professional")
    style = brand.get("voice", {}).get("style", "")
    
    blacklist = brand.get("blacklist", [])
    audience = brand.get("audience", "general audience")

    blacklist_str = (
        f"NEVER use these words: {', '.join(blacklist)}."
        if blacklist else ""
    )

    return f"""You are a content expert for {name}.
Brand tone: {tone}
Writing style: {style}
Target audience: {audience}
Pipeline: {pipeline}
{blacklist_str}
Always match the brand voice exactly."""
