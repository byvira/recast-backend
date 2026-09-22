from app.platforms.base import PlatformDefinition, register_platform

# YouTube lives at app/platforms/youtube.py (top level, status="partial" — its
# analytics fetcher is already real). The other 3 video platforms are here.
CATEGORY = "Video platforms"

ROWS = [
    dict(
        key="tiktok", label="TikTok",
        pipelines=frozenset({"text", "image", "video", "audio"}),
        native_formats={"video": "native", "image": "photo", "text": "card", "audio": "audiogram"},
        mode="code_driven", integration_pattern="api_publish",
        audit_required=True,
        rate_limits="Unaudited clients are restricted to private viewing until audit passes; 6 requests/minute per user token (TikTok Direct Post reference, checked 2026-09-20).",
        policy_constraints=[
            "Content from unaudited apps stays private-only until the Direct Post audit passes.",
            "Build and test in TikTok's sandbox first.",
        ],
        confidence="verified",
    ),
    dict(
        key="vimeo", label="Vimeo",
        pipelines=frozenset({"video"}),
        native_formats={"video": "native"},
        mode="code_driven", integration_pattern="api_publish",
        rate_limits="Free API; plan limits apply (verify).",
        policy_constraints=["Ad-free, embeddable — hosts a master video for embedding in blogs/newsletters."],
        confidence="unverified",
    ),
    dict(
        key="dailymotion", label="Dailymotion",
        pipelines=frozenset({"video"}),
        native_formats={"video": "native"},
        mode="code_driven", integration_pattern="api_publish",
        rate_limits="Free API (verify).",
        policy_constraints=["Regional strength in France/Europe; has a partner program."],
        confidence="unverified",
    ),
]

for _row in ROWS:
    register_platform(PlatformDefinition(category=CATEGORY, status="planned", **_row))
