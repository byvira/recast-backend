from app.prompts.registry import load_prompt


def build_brand_context(brand: dict, pipeline: str) -> str:
    """
    Build a system prompt string from the brand profile dict.
    Injected as system message into every LLM call.

    Args:
        brand: brand profile dict from MongoDB brand_profiles collection
        pipeline: which pipeline is calling (text/audio/video/image)

    Returns:
        Formatted system prompt string with brand voice instructions

    Renders app/prompts/fragments/brand_context.jinja (document_shape="media_util").
    """
    return load_prompt(
        "fragments/brand_context", document_shape="media_util", brand=brand, pipeline=pipeline
    )
