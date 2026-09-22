from app.platforms.base import PlatformDefinition, register_platform


@register_platform
def _facebook() -> PlatformDefinition:
    return PlatformDefinition(
        key="facebook",
        label="Facebook",
        category="Social and feed networks",
        pipelines=frozenset({"text", "image", "video", "audio"}),
        native_formats={"text": "native", "image": "native", "video": "native", "audio": "audiogram"},
        mode="code_driven",
        integration_pattern="api_publish",
        status="active",
        publisher_cls="app.pipelines.publish.meta.facebook.FacebookPublisher",
        analytics_fetcher_cls="app.pipelines.analytics.facebook.FacebookAnalyticsFetcher",
        validator_fn="app.pipelines.publish.validators.validate_facebook",
        audit_required=True,
        policy_constraints=[
            "Pages are business-first; posting to Groups is restricted (verify before offering it).",
        ],
        tone_profile="general audience",
        access_notes="Connected; still in dev mode until Meta App Review completes.",
        confidence="verified",
        has_text_prompt_rules=True,
        text_enum_value="Facebook",
    )
