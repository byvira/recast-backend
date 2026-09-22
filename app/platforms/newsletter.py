from app.platforms.base import PlatformDefinition, register_platform


@register_platform
def _newsletter() -> PlatformDefinition:
    """Generic newsletter text target — see blog.py's docstring for why this is
    separate from the specific email platforms (Resend/Mailchimp/etc) in
    app/platforms/planned/email.py."""
    return PlatformDefinition(
        key="newsletter",
        label="Newsletter",
        category="Generic content shape",
        pipelines=frozenset({"text"}),
        native_formats={"text": "native"},
        mode="config_driven",
        integration_pattern="generation_only",
        status="active",
        access_notes="Generic newsletter generation target, not a specific publish destination. No publisher — output is copy-pasted or exported.",
        confidence="verified",
        has_text_prompt_rules=True,
        text_enum_value="Newsletter",
    )
