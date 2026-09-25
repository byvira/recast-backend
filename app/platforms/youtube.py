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
        # Basic publishing shipped 2026-09-25 (app/pipelines/publish/youtube/
        # publisher.py) — real resumable video upload, brand-grounded
        # title/description/tags/category via metadata.py, private-by-
        # default (see policy_constraints below). Full "engaging video
        # asset" richness (chapters, captions, end screens) is a
        # deliberate later stage, not part of this build.
        status="active",
        publisher_cls="app.pipelines.publish.youtube.publisher.YouTubePublisher",
        analytics_fetcher_cls="app.pipelines.analytics.youtube.YouTubeAnalyticsFetcher",
        validator_fn="app.pipelines.publish.validators.validate_youtube",
        audit_required=True,
        rate_limits="Default 100 video uploads/day; extra quota needs an audit (Google quota page, checked 2026-09-20).",
        policy_constraints=[
            "Unverified projects created after July 2020 are restricted to private uploads until audit passes.",
        ],
        access_notes=(
            "OAuth now requests youtube.upload alongside youtube.readonly (was readonly-only "
            "until the publisher existed) — a connection made before 2026-09-25 must reconnect "
            "to publish. A connected account with no YouTube channel correctly returns nothing."
        ),
        confidence="verified",
        has_text_prompt_rules=True,
        text_enum_value="YouTube",
    )
