from app.platforms.base import PlatformDefinition, register_platform


@register_platform
def _linkedin() -> PlatformDefinition:
    return PlatformDefinition(
        key="linkedin",
        label="LinkedIn",
        category="Social and feed networks",
        pipelines=frozenset({"text", "image", "video", "audio"}),
        native_formats={"text": "native", "image": "native", "video": "native", "audio": "audiogram"},
        mode="code_driven",
        integration_pattern="api_publish",
        status="active",
        publisher_cls="app.pipelines.publish.linkedin.publisher.LinkedInPublisher",
        analytics_fetcher_cls="app.pipelines.analytics.linkedin.LinkedInAnalyticsFetcher",
        validator_fn="app.pipelines.publish.validators.validate_linkedin",
        audit_required=False,
        policy_constraints=[
            "B2B audience — longer, more formal tone expected than other networks.",
        ],
        tone_profile="professional",
        access_notes="Connected and live. Company Page publishing needs its own approval — confirm before offering Page posting.",
        confidence="verified",
        has_text_prompt_rules=True,
        text_enum_value="LinkedIn",
    )
