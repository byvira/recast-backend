"""Image generator — creates images from text prompts."""


async def generate_image(prompt: str) -> bytes:
    """Generate an image from the provided text *prompt*.

    Sends the prompt to an image generation API (e.g. DALL-E, Stable Diffusion)
    and returns the raw image bytes.

    Args:
        prompt: Natural language description of the image to generate.

    Returns:
        Raw PNG or JPEG bytes of the generated image.
    """
    # Placeholder: call image generation API and return image bytes
    return b""
