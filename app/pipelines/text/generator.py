"""Text content generator using the shared LLM client."""

from typing import Any


async def generate_content(input: dict[str, Any]) -> str:
    """Generate long-form text content from the provided input parameters.

    Constructs a structured prompt from topic, platform, tone, and word_count,
    then calls the LLM and returns the raw text output.

    Args:
        input: Dict with topic, platform, tone, and word_count keys.

    Returns:
        Generated text content as a plain string.
    """
    # Placeholder: build prompt from input and call shared.llm.call_llm
    return ""
