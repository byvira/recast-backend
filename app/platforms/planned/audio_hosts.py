from app.platforms.base import PlatformDefinition, register_platform

# The upgrade path beyond serving Recast's own RSS feed — hosts give users
# their own analytics and one-click distribution.
CATEGORY = "Audio hosts and direct upload"

ROWS = [
    dict(
        key="soundcloud", label="SoundCloud",
        pipelines=frozenset({"audio"}),
        native_formats={"audio": "native"},
        mode="code_driven", integration_pattern="api_publish",
        rate_limits="New API apps may be restricted — check before planning (verify).",
        policy_constraints=["Direct uploads plus a social feed for artists and creators."],
        confidence="unverified",
    ),
    dict(
        key="mixcloud", label="Mixcloud",
        pipelines=frozenset({"audio"}),
        native_formats={"audio": "native"},
        mode="code_driven", integration_pattern="api_publish",
        rate_limits="Free API (verify).",
        policy_constraints=["Long-form audio for DJs/talk shows; music licensing rules apply."],
        confidence="unverified",
    ),
    dict(
        key="buzzsprout", label="Buzzsprout",
        pipelines=frozenset({"audio", "image"}),
        native_formats={"audio": "native", "image": "cover_art"},
        mode="code_driven", integration_pattern="api_publish",
        rate_limits="Free plan; API available (verify pricing tiers).",
        policy_constraints=["Beginner-friendly host; auto-distributes to the main directories."],
        confidence="third_party",
    ),
    dict(
        key="transistor", label="Transistor",
        pipelines=frozenset({"audio", "video"}),
        native_formats={"audio": "native", "video": "native"},
        mode="code_driven", integration_pattern="api_publish",
        rate_limits="Paid plans; API available (verify).",
        policy_constraints=["One account hosts unlimited shows — multi-show workspaces map naturally to Recast workspaces."],
        confidence="third_party",
    ),
    dict(
        key="podbean", label="Podbean",
        pipelines=frozenset({"audio"}),
        native_formats={"audio": "native"},
        mode="code_driven", integration_pattern="api_publish",
        rate_limits="Free plan; API available (verify).",
        policy_constraints=["Built-in listener app and ad marketplace — alternative host for users who monetise."],
        confidence="unverified",
    ),
    dict(
        key="libsyn", label="Libsyn",
        pipelines=frozenset({"audio"}),
        native_formats={"audio": "native"},
        mode="code_driven", integration_pattern="api_publish",
        rate_limits="Paid; API available (verify).",
        policy_constraints=["Long-established host with detailed stats and network tools."],
        confidence="unverified",
    ),
]

for _row in ROWS:
    register_platform(PlatformDefinition(category=CATEGORY, status="planned", **_row))
