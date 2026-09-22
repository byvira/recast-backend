from app.platforms.base import PlatformDefinition, register_platform


@register_platform
def _youtube() -> PlatformDefinition:
    return PlatformDefinition(
        key="youtube",
        label="YouTube",
        category="Video platforms",
        pipelines=frozenset({"text", "image", "video", "audio"}),
        native_formats={
            "video": "native", "audio": "audiogram", "image": "thumbnail", "text": "description",
        },
        mode="code_driven",
        integration_pattern="api_publish",
        # Analytics fetcher is real and live (app/pipelines/analytics/youtube.py, used by
        # the aggregator today); no publisher exists yet (not in PUBLISHERS/registry.py) —
        # reading works, publishing doesn't, hence "partial" rather than "active" or "planned".
        status="partial",
        publisher_cls=None,
        analytics_fetcher_cls="app.pipelines.analytics.youtube.YouTubeAnalyticsFetcher",
        validator_fn=None,
        audit_required=True,
        rate_limits="Default 100 video uploads/day; extra quota needs an audit (Google quota page, checked 2026-09-20).",
        policy_constraints=[
            "Unverified projects created after July 2020 are restricted to private uploads until audit passes.",
        ],
        access_notes=(
            "OAuth connected for analytics reading. A connected account with no YouTube channel "
            "correctly returns nothing. Publishing not built — no YouTube publisher exists yet."
        ),
        confidence="verified",
        has_text_prompt_rules=True,
        text_enum_value="YouTube",
    )
