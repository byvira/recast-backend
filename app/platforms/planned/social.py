from app.platforms.base import PlatformDefinition, register_platform

CATEGORY = "Social and feed networks"

ROWS = [
    dict(
        key="mastodon", label="Mastodon",
        pipelines=frozenset({"text", "image", "video"}),
        native_formats={"text": "native", "image": "native", "video": "native"},
        mode="code_driven", integration_pattern="api_publish",
        rate_limits="Free API per server (verify).",
        policy_constraints=["Each server sets its own rules; 500-character default post length."],
        access_notes="Federated microblog across independent servers.",
        confidence="unverified",
    ),
    dict(
        key="reddit", label="Reddit",
        pipelines=frozenset({"text", "image", "video"}),
        native_formats={"text": "native", "image": "native", "video": "native"},
        mode="config_driven", integration_pattern="manual_handoff",
        # validate_reddit already exists in app/pipelines/publish/validators.py's
        # VALIDATORS dict with no publisher behind it — the live bug Stage 0
        # flagged. Pointing at it here is honest about what's real (a validator
        # function) vs. not (no publisher, no registry.PUBLISHERS entry).
        validator_fn="app.pipelines.publish.validators.validate_reddit",
        audit_required=True,
        rate_limits="Requires approval before any API access (Responsible Builder Policy, updated 2026-06-05).",
        policy_constraints=[
            "Commercial use needs explicit written approval via Reddit's commercial request form, not the developer one.",
            "Automated posting of substantially similar content across subreddits is prohibited — adapt content per subreddit.",
            "Submitting multiple requests for the same use case is prohibited.",
            "A title is required for every post; many communities restrict self-promotion.",
        ],
        access_notes="Blocked pending Reddit's approval; manual handoff for now, same pattern as X — moves to API publish once approved.",
        confidence="verified",
    ),
    dict(
        key="tumblr", label="Tumblr",
        pipelines=frozenset({"text", "image", "video"}),
        native_formats={"text": "native", "image": "native", "video": "native"},
        mode="code_driven", integration_pattern="api_publish",
        rate_limits="Free API (verify).",
        policy_constraints=["Aesthetic and fandom-driven audience; posts keep circulating via reblogs."],
        confidence="unverified",
    ),
    dict(
        key="pinterest", label="Pinterest",
        pipelines=frozenset({"text", "image", "video", "audio"}),
        native_formats={"image": "native", "video": "native", "text": "card", "audio": "audiogram"},
        mode="code_driven", integration_pattern="api_publish",
        audit_required=True,
        rate_limits="Trial access in days, Standard access in 1-4 weeks (Pinterest access tiers page, checked 2026-09-20).",
        policy_constraints=[
            "Trial Pins/boards are visible only to their creator.",
            "Standard access requires a demo video showing the OAuth flow.",
            "Trial requests can be denied if the privacy policy link doesn't load publicly.",
        ],
        confidence="verified",
    ),
    dict(
        key="snapchat", label="Snapchat",
        pipelines=frozenset({"video"}),
        native_formats={"video": "manual"},
        mode="config_driven", integration_pattern="manual_handoff",
        policy_constraints=["Young audience; no open publishing API — vertical video export only."],
        access_notes="Manual only — no publish tracking possible.",
        confidence="unverified",
    ),
    dict(
        key="nostr", label="Nostr",
        pipelines=frozenset({"text", "image"}),
        native_formats={"text": "native", "image": "link"},
        mode="code_driven", integration_pattern="api_publish",
        rate_limits="Free; no approval, but client library choice is unverified.",
        policy_constraints=["Decentralised protocol; posts signed with a key — no company, no approval, censorship-resistant."],
        confidence="unverified",
    ),
    dict(
        key="farcaster", label="Farcaster",
        pipelines=frozenset({"text", "image"}),
        native_formats={"text": "native", "image": "native"},
        mode="code_driven", integration_pattern="api_publish",
        rate_limits="Free via third-party providers (verify).",
        policy_constraints=["Crypto-native web3 audience; posts are called 'casts'."],
        confidence="unverified",
    ),
]

for _row in ROWS:
    register_platform(PlatformDefinition(category=CATEGORY, status="planned", **_row))
