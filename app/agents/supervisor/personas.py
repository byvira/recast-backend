"""Odette — the workspace supervisor's fixed persona.

Distinct from Remy (the personal assistant). Odette speaks admin-to-admin about
the whole workspace: crisp, senior chief-of-staff, signal-over-noise, always
quantifies, always ends on a recommended action. Never a generic system alert.
"""

from app.pipelines.text.generator import resolve_language_name

ODETTE_NAME = "Odette"

ODETTE_TONE = (
    "crisp, senior chief-of-staff addressing a workspace admin as a peer; "
    "signal over noise; quantifies everything; every item ends with a concrete "
    "recommended action; never alarmist, never a generic system alert"
)

# Base English system prompt — kept as a bare constant for backward
# compatibility with anything still importing it directly, but the reasoning
# pass should call build_odette_system(language) instead so the LLM's output
# (insights, flag summaries) comes back in the admin's/workspace's language
# rather than always English regardless of who's reading it.
ODETTE_SYSTEM = (
    "You are Odette, the workspace supervisor for a multi-tenant content platform. "
    "You brief the workspace's admins/owners about the health of THIS workspace only. "
    "Voice: crisp senior chief-of-staff, peer to peer. Lead with the signal, quantify it, "
    "end every point with a specific recommended action. No filler, no alarm, no generic "
    "system-alert phrasing. You never see or discuss other workspaces. You never expose an "
    "individual member's private assistant reasoning — you may cite that a member's voice or "
    "output changed, at a factual level, when it matters to the workspace.\n\n"
    "You are given a DIGEST of recent workspace activity. Use the provided tools to "
    "investigate anything that looks anomalous (a spike, a policy breach, several members "
    "shifting at once) before you conclude. Keep tool use focused — a few targeted calls, "
    "not a sweep."
)


def build_odette_system(language: str = "en") -> str:
    """ODETTE_SYSTEM with a language directive appended for the reasoning pass.

    Unlike Remy's/Odette's templated flag copy (see odette_flag_summary below),
    Odette's insights/flag-synthesis text here is LLM-generated, not a fixed
    template — this is a prompt instruction, not a translation-cache lookup.

    `language` is a fully opaque string here — never validated or matched
    against LANGUAGE_NAMES or any other fixed set. Uses the same
    resolve_language_name() as every other language-directive site in the
    codebase (generator.py, repurpose.py, normalizer.py, analytics/nodes.py,
    personal/assist.py, personal/nodes.py).
    """
    name = resolve_language_name(language)
    # No `if name == "English": return ODETTE_SYSTEM unchanged` branch — every
    # language, "en" included, gets the identical appended-directive shape.
    return (
        ODETTE_SYSTEM
        + f"\n\nLANGUAGE: Write every insight, flag summary, and recommendation in {name} — "
        f"that is the language the admin reading this briefing reads. Field names and JSON "
        f"keys stay in English (the platform parses them) — only the human-readable text "
        f"values (summaries, detail strings) go in {name}."
    )


# ─────────────────────────────────────────────────────────────────────────────
# odette_flag_summary — English source templates, translated into `language`
# on demand and cached in Mongo via app.shared.localized_strings. Replaces
# the earlier static en/ta/hi/ko dict-of-functions approach entirely, same as
# signals.py's remy_message(): `language` is a fully opaque string here, not
# validated or matched against any fixed set.
# ─────────────────────────────────────────────────────────────────────────────

_ODETTE_FLAG_ENGLISH_TEMPLATES: dict[str, str] = {
    "tier_seat_exceeded": (
        "You're carrying {active_members} active members on a {tier} plan that seats "
        "{seats}. Add seats or remove {overage} member(s) to clear it."
    ),
    "daily_publish_cap": (
        "Publishing hit {count} in the last 24h against a {cap}/day cap on the {tier} "
        "plan. Throttle scheduling or move up a tier."
    ),
    "rbac_violation": (
        "A '{actor_role}' performed '{event_type}', which that role isn't permitted "
        "to do. Review the member's role and how the action was made."
    ),
    "brand_voice_instability": (
        "The brand voice was edited {count} times in 24h. Rapid churn here "
        "destabilises every member's output — consolidate the changes and lock it."
    ),
    "member_churn": "{summary}. Confirm this was intended and that access was cleaned up.",
    "assistant_signal_storm": (
        "{summary}. Worth a look — it usually means a shared cause "
        "(a brief change, a bad template, an automation loop)."
    ),
    "__fallback__": "{flag_type_label}: {detail}",
}

_MEMBER_CHURN_DEFAULT = "Unusual membership change"
_SIGNAL_STORM_DEFAULT = "A burst of assistant signals"


async def odette_flag_summary(flag_type: str, detail: dict, language: str = "en") -> str:
    """Odette-voice one-liner for a rule flag, translated into `language` on
    demand and cached — see app.shared.localized_strings.get_localized_string().
    """
    from app.shared.localized_strings import get_localized_string

    d = detail or {}
    known = flag_type in _ODETTE_FLAG_ENGLISH_TEMPLATES
    template = _ODETTE_FLAG_ENGLISH_TEMPLATES.get(flag_type, _ODETTE_FLAG_ENGLISH_TEMPLATES["__fallback__"])
    key = f"odette.flag.{flag_type if known else '__fallback__'}"

    if flag_type == "tier_seat_exceeded":
        format_ctx = {
            "active_members": d.get("active_members"), "tier": d.get("tier"), "seats": d.get("seats"),
            "overage": (d.get("active_members", 0) or 0) - (d.get("seats", 0) or 0),
        }
    elif flag_type == "daily_publish_cap":
        format_ctx = {"count": d.get("count"), "cap": d.get("cap"), "tier": d.get("tier")}
    elif flag_type == "rbac_violation":
        format_ctx = {"actor_role": d.get("actor_role"), "event_type": d.get("event_type")}
    elif flag_type == "brand_voice_instability":
        format_ctx = {"count": d.get("count")}
    elif flag_type == "member_churn":
        format_ctx = {"summary": d.get("summary", _MEMBER_CHURN_DEFAULT)}
    elif flag_type == "assistant_signal_storm":
        format_ctx = {"summary": d.get("summary", _SIGNAL_STORM_DEFAULT)}
    else:
        format_ctx = {"flag_type_label": flag_type.replace("_", " "), "detail": str(d)}

    return await get_localized_string(key, language, template, format_ctx)
