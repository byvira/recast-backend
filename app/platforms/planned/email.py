from app.platforms.base import PlatformDefinition, register_platform

CATEGORY = "Email newsletters"

ROWS = [
    dict(
        key="resend", label="Resend",
        pipelines=frozenset({"text", "image", "video", "audio"}),
        native_formats={"text": "native", "image": "native", "video": "link", "audio": "link"},
        mode="code_driven", integration_pattern="api_publish",
        rate_limits="Free tier; verify the sending domain to send beyond your own address.",
        policy_constraints=[],
        access_notes=(
            "Resend is already integrated in this codebase for transactional email "
            "(OTP, notifications) — the natural first choice for a real newsletter "
            "integration, but no content-broadcast path exists yet; this entry covers "
            "that unbuilt broadcast use, not the existing transactional one."
        ),
        confidence="unverified",
    ),
    dict(
        key="mailchimp", label="Mailchimp",
        pipelines=frozenset({"text", "image"}),
        native_formats={"text": "native", "image": "native"},
        mode="code_driven", integration_pattern="api_publish",
        rate_limits="Free plan has real limits (verify).",
        policy_constraints=[],
        access_notes=(
            "Large template library and audience tools. A dead placeholder client exists at "
            "app/integrations/mailchimp.py with zero imports anywhere (see docs/STATUS.md) — "
            "a real integration would be a fresh build against Mailchimp's actual API, not a "
            "revival of that file."
        ),
        confidence="unverified",
    ),
    dict(
        key="kit", label="Kit (ConvertKit)",
        pipelines=frozenset({"text", "image"}),
        native_formats={"text": "native", "image": "native"},
        mode="code_driven", integration_pattern="api_publish",
        rate_limits="API access depends on plan (verify).",
        policy_constraints=["Built for creator sequences and subscriber tagging."],
        confidence="unverified",
    ),
    dict(
        key="beehiiv", label="Beehiiv",
        pipelines=frozenset({"text", "image"}),
        native_formats={"text": "native", "image": "native"},
        mode="code_driven", integration_pattern="api_publish",
        rate_limits="Plan-dependent API access (verify).",
        policy_constraints=["Newsletter platform with growth/referral tools."],
        confidence="unverified",
    ),
    dict(
        key="buttondown", label="Buttondown",
        pipelines=frozenset({"text"}),
        native_formats={"text": "native"},
        mode="code_driven", integration_pattern="api_publish",
        rate_limits="API access (verify).",
        policy_constraints=["Markdown-first, simple indie newsletter tool."],
        confidence="unverified",
    ),
]

for _row in ROWS:
    register_platform(PlatformDefinition(category=CATEGORY, status="planned", **_row))
