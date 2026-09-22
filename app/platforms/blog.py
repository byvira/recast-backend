from app.platforms.base import PlatformDefinition, register_platform


@register_platform
def _blog() -> PlatformDefinition:
    """Generic long-form text target — not a specific platform from the source
    directory (WordPress/Ghost/Medium/etc are the specific ones, under
    app/platforms/planned/blogs_cms.py). This is the pre-existing generation-only
    Platform.BLOG value: real and working today, never had or needed a publisher."""
    return PlatformDefinition(
        key="blog",
        label="Blog",
        category="Generic content shape",
        pipelines=frozenset({"text"}),
        native_formats={"text": "native"},
        mode="config_driven",
        integration_pattern="generation_only",
        status="active",
        access_notes="Generic long-form generation target, not a specific publish destination. No publisher — output is copy-pasted or exported.",
        confidence="verified",
        has_text_prompt_rules=True,
        text_enum_value="Blog",
    )
