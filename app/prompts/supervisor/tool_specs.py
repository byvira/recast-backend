"""OpenAI/Groq function-calling schemas for the workspace supervisor's ReAct
loop (app.agents.supervisor.nodes::reason_node). Kept as typed Python, not a
.jinja template — these are structured tool definitions, not prose — but
co-located under app/prompts/ with the rest of Odette's LLM-facing surface
for review purposes, since they shape model behavior exactly like a system
prompt does. See app.agents.supervisor.tools for the workspace_id-bound
implementations these schemas describe.
"""

# ── OpenAI/Groq tool schemas (no workspace_id anywhere) ──────────────────────
TOOL_SPECS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_member_recent_content",
            "description": "Recent content pieces authored by one workspace member, newest first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 25},
                },
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_member_persona_summary",
            "description": (
                "A member's voice persona summary: style fingerprint, topics, volume/quality "
                "stats, and recent drift history. Never returns raw embeddings or private "
                "assistant reasoning."
            ),
            "parameters": {
                "type": "object",
                "properties": {"user_id": {"type": "string"}},
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_workspace_tier_history",
            "description": "This workspace's tier and any recent tier.changed events.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_publishes",
            "description": "content.published events in the workspace over the last N hours.",
            "parameters": {
                "type": "object",
                "properties": {"hours": {"type": "integer", "minimum": 1, "maximum": 168}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_open_flags",
            "description": "Currently-open supervisor flags for this workspace.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_signal_history",
            "description": "Personal-assistant signals in this workspace; optionally filter to one member.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "days": {"type": "integer", "minimum": 1, "maximum": 30},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_brand_voice_versions",
            "description": "Brand profiles in this workspace and recent brand.voice_updated events.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]
