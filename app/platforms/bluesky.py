from app.platforms.base import PlatformDefinition, register_platform


@register_platform
def _bluesky() -> PlatformDefinition:
    return PlatformDefinition(
        key="bluesky",
        label="Bluesky",
        category="Social and feed networks",
        pipelines=frozenset({"text", "image", "audio"}),
        native_formats={"text": "native", "image": "native", "audio": "audiogram"},
        mode="code_driven",
        integration_pattern="api_publish",
        status="active",
        publisher_cls="app.pipelines.publish.bluesky.publisher.BlueSkyPublisher",
        analytics_fetcher_cls="app.pipelines.analytics.bluesky.BlueskyAnalyticsFetcher",
        validator_fn="app.pipelines.publish.validators.validate_bluesky",
        audit_required=False,
        policy_constraints=[
            "Open-protocol microblog, 300-character posts — no platform approval gate.",
        ],
        tone_profile="tech, creator-oriented",
        access_notes="Connected; free API, no approval needed. Video support unverified — treat as image/text only for now.",
        confidence="verified",
    )
