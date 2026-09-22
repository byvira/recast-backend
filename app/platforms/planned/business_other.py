from app.platforms.base import PlatformDefinition, register_platform

CATEGORY = "Business, images and other"

ROWS = [
    dict(
        key="google_business_profile", label="Google Business Profile",
        pipelines=frozenset({"text", "image"}),
        native_formats={"text": "native", "image": "native"},
        mode="code_driven", integration_pattern="api_publish",
        audit_required=True,
        rate_limits="Uses the business.manage sensitive scope — gated behind Google's own verification/security review, submitted by the app owner (see docs/STATUS.md R3-2).",
        policy_constraints=["Needs Google's approval before any access; local-business updates and offers."],
        access_notes="Fully scoped, not built — blocked on the app owner submitting Google's sensitive-scope verification.",
        confidence="unverified",
    ),
    dict(
        key="flickr", label="Flickr",
        pipelines=frozenset({"image"}),
        native_formats={"image": "native"},
        mode="code_driven", integration_pattern="api_publish",
        rate_limits="Free upload API (verify).",
        policy_constraints=["Photographer community and albums."],
        confidence="unverified",
    ),
    dict(
        key="imgur", label="Imgur",
        pipelines=frozenset({"image"}),
        native_formats={"image": "native"},
        mode="code_driven", integration_pattern="api_publish",
        rate_limits="Free upload API — verify commercial-use terms before relying on it.",
        policy_constraints=["Images spread through meme/community feeds; check commercial terms before use."],
        confidence="unverified",
    ),
    dict(
        key="hackernews", label="Hacker News",
        pipelines=frozenset({"text"}),
        native_formats={"text": "manual"},
        mode="config_driven", integration_pattern="manual_handoff",
        policy_constraints=["No submission API; titles are judged strictly by the community."],
        access_notes="Manual only — prefilled submit link for product launches.",
        confidence="unverified",
    ),
    dict(
        key="patreon", label="Patreon",
        pipelines=frozenset({"text", "image", "video", "audio"}),
        native_formats={"text": "manual", "image": "manual", "video": "manual", "audio": "manual"},
        mode="config_driven", integration_pattern="manual_handoff",
        policy_constraints=["The API reportedly does not support creating posts — treat as manual until confirmed otherwise."],
        access_notes="Manual — ready-to-paste supporter updates.",
        confidence="unverified",
    ),
    dict(
        key="github", label="GitHub",
        pipelines=frozenset({"text"}),
        native_formats={"text": "native"},
        mode="code_driven", integration_pattern="api_publish",
        rate_limits="Free API token.",
        policy_constraints=["Developer audience — release notes and changelog posts via Releases/Discussions."],
        confidence="unverified",
    ),
]

for _row in ROWS:
    register_platform(PlatformDefinition(category=CATEGORY, status="planned", **_row))
