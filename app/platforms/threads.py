from app.platforms.base import PlatformDefinition, register_platform


@register_platform
def _threads() -> PlatformDefinition:
    return PlatformDefinition(
        key="threads",
        label="Threads",
        category="Social and feed networks",
        pipelines=frozenset({"text", "image", "video", "audio"}),
        native_formats={"text": "native", "image": "native", "video": "native", "audio": "audiogram"},
        mode="code_driven",
        integration_pattern="api_publish",
        status="active",
        publisher_cls="app.pipelines.publish.meta.threads.ThreadsPublisher",
        analytics_fetcher_cls="app.pipelines.analytics.threads.ThreadsAnalyticsFetcher",
        validator_fn="app.pipelines.publish.validators.validate_threads",
        audit_required=False,
        policy_constraints=[
            "Conversational register — tied to an Instagram identity, 500-character posts.",
        ],
        tone_profile="conversational",
        access_notes=(
            "Connected, own OAuth flow. Insights need the threads_manage_insights scope "
            "(added after initial connect — workspaces connected earlier must reconnect once)."
        ),
        confidence="verified",
    )
