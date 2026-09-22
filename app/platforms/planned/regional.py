from app.platforms.base import PlatformDefinition, register_platform

# Flagged in the source directory for a later phase — each needs language,
# legal and account-verification review before any build starts.
CATEGORY = "Regional platforms"

ROWS = [
    dict(
        key="wechat", label="WeChat Official Accounts",
        pipelines=frozenset({"text", "image", "video", "audio"}),
        native_formats={"text": "native", "image": "native", "video": "native", "audio": "native"},
        mode="code_driven", integration_pattern="api_publish",
        audit_required=True,
        rate_limits="Restricted; approval required (verify).",
        policy_constraints=[
            "Needs a China-verified business account; article-based publishing model.",
            "Regional/legal review required before any build.",
        ],
        confidence="unverified",
    ),
    dict(
        key="weibo", label="Weibo",
        pipelines=frozenset({"text", "image", "video"}),
        native_formats={"text": "native", "image": "native", "video": "native"},
        mode="code_driven", integration_pattern="api_publish",
        audit_required=True,
        rate_limits="Restricted — API limited to approved developers (verify).",
        policy_constraints=[
            "Chinese microblog, similar to X; developer approval required.",
            "Regional/legal review required before any build.",
        ],
        confidence="unverified",
    ),
    dict(
        key="vk", label="VK",
        pipelines=frozenset({"text", "image", "video", "audio"}),
        native_formats={"text": "native", "image": "native", "video": "native", "audio": "native"},
        mode="code_driven", integration_pattern="api_publish",
        rate_limits="Free API — confirm current status before relying on it.",
        policy_constraints=[
            "Russian-language network used across the CIS.",
            "Check sanctions and compliance rules before use — legal review required first.",
        ],
        confidence="unverified",
    ),
]

for _row in ROWS:
    register_platform(PlatformDefinition(category=CATEGORY, status="planned", **_row))
