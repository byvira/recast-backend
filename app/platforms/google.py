from app.platforms.base import PlatformDefinition, register_platform


@register_platform
def _google() -> PlatformDefinition:
    """Not a platform from the source directory — this is the umbrella Google
    OAuth identity connection (app/api/v1/oauth.py's google_callback) that
    YouTube's connection is derived from. It holds no content pipelines and
    publishes nothing itself; it exists in the registry only because
    workspace_connections really does get a platform="google" row today, and
    Stage 1 item 6 validates every write against registry keys."""
    return PlatformDefinition(
        key="google",
        label="Google Account",
        category="OAuth identity",
        pipelines=frozenset(),
        mode="code_driven",
        integration_pattern="api_publish",
        status="active",
        audit_required=False,
        access_notes=(
            "Umbrella Google OAuth connection — YouTube's own connection (app/platforms/youtube.py) "
            "is derived from this token, not a separate OAuth flow. Not a publish target on its own."
        ),
        confidence="verified",
    )
