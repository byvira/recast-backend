from app.platforms.base import PlatformDefinition, register_platform

CATEGORY = "Fediverse extras"

ROWS = [
    dict(
        key="lemmy", label="Lemmy",
        pipelines=frozenset({"text", "image"}),
        native_formats={"text": "native", "image": "native"},
        mode="code_driven", integration_pattern="api_publish",
        rate_limits="Free API (verify).",
        policy_constraints=["Federated link aggregator similar to Reddit; communities live on independent servers."],
        confidence="unverified",
    ),
    dict(
        key="pixelfed", label="Pixelfed",
        pipelines=frozenset({"image"}),
        native_formats={"image": "native"},
        mode="code_driven", integration_pattern="api_publish",
        rate_limits="Free API (verify).",
        policy_constraints=["Ad-free and chronological photo sharing; Mastodon-compatible API."],
        confidence="unverified",
    ),
    dict(
        key="peertube", label="PeerTube",
        pipelines=frozenset({"video", "audio"}),
        native_formats={"video": "native", "audio": "audiogram"},
        mode="code_driven", integration_pattern="api_publish",
        rate_limits="Free API (verify).",
        policy_constraints=["Federated video hosting on independent instances; open API, no audit."],
        confidence="unverified",
    ),
]

for _row in ROWS:
    register_platform(PlatformDefinition(category=CATEGORY, status="planned", **_row))
