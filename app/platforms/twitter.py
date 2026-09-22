from app.platforms.base import PlatformDefinition, register_platform


@register_platform
def _twitter() -> PlatformDefinition:
    """X (Twitter). Text generation (Platform.TWITTER / Platform.TWITTER_THREAD in
    app/models/text.py) is real and working today — publishing isn't. shapes
    carries the post/thread distinction at the registry level; see
    app/platforms/__init__.py's docstring for why the generator.py call sites
    that still key off a separate TWITTER_THREAD enum member are untouched."""
    return PlatformDefinition(
        key="twitter",
        label="X (Twitter)",
        category="Social and feed networks",
        pipelines=frozenset({"text", "image", "video", "audio"}),
        native_formats={"text": "native", "image": "native", "video": "native", "audio": "audiogram"},
        shapes=["post", "thread"],
        mode="config_driven",
        integration_pattern="manual_handoff",
        status="partial",
        audit_required=False,
        rate_limits="Paid API — roughly $0.015/plain post, $0.20/post with a link (third-party estimate; confirm current rates in the X developer console before pricing).",
        policy_constraints=[],
        access_notes=(
            "Text generation is real and live. Publishing is manual handoff for now "
            "(prefilled compose link, no publish tracking) — moves to API publish once "
            "paid access is set up, same pattern as Reddit."
        ),
        confidence="third_party",
        has_text_prompt_rules=True,
        text_enum_value="Twitter/X",
        thread_enum_value="Twitter/X Thread",
    )
