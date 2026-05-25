# Re-export everything from shared/llm.py
from app.shared.llm import (
    call_llm,
    call_llm_structured,
    call_llm_fallback,
    call_vision,
    transcribe_audio,
    GroqModel,
    GeminiModel,
)


async def enhance_image_prompt(
    raw_prompt: str,
    brand_colors: str = "",
    brand_style: str = "",
    brand_aesthetic: str = "",
) -> str:
    """
    Enhance a raw image prompt with brand visual identity.
    Used by: image agent generate_node
    Placeholder: returns raw_prompt until image gen is wired.
    """
    # Placeholder: will call call_llm to rewrite prompt
    # with brand colors, style, and aesthetic injected
    return raw_prompt


async def generate_image(prompt: str) -> bytes:
    """
    Generate an image from a text prompt.
    Used by: image agent generate_node
    Placeholder: returns empty bytes until image provider wired.
    Providers: Stability AI, DALL-E 3, Ideogram, Flux
    """
    # Placeholder: will call image generation API
    return b""
