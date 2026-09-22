from app.platforms.base import PlatformDefinition, register_platform


@register_platform
def _instagram() -> PlatformDefinition:
    return PlatformDefinition(
        key="instagram",
        label="Instagram",
        category="Social and feed networks",
        pipelines=frozenset({"text", "image", "video", "audio"}),
        native_formats={"image": "native", "video": "native", "text": "card", "audio": "audiogram"},
        mode="code_driven",
        integration_pattern="api_publish",
        status="active",
        publisher_cls="app.pipelines.publish.meta.instagram.InstagramPublisher",
        analytics_fetcher_cls="app.pipelines.analytics.instagram.InstagramAnalyticsFetcher",
        validator_fn="app.pipelines.publish.validators.validate_instagram",
        audit_required=True,
        policy_constraints=[
            "Image-first — needs a Business or Creator account linked to a Facebook Page.",
        ],
        tone_profile="visual, casual",
        access_notes="Connected; Meta App Review required before real (non-tester) users can post.",
        confidence="verified",
        has_text_prompt_rules=True,
        text_enum_value="Instagram",
    )
