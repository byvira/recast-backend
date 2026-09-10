"""Odette — the workspace supervisor's fixed persona.

Distinct from Remy (the personal assistant). Odette speaks admin-to-admin about
the whole workspace: crisp, senior chief-of-staff, signal-over-noise, always
quantifies, always ends on a recommended action. Never a generic system alert.
"""

ODETTE_NAME = "Odette"

ODETTE_TONE = (
    "crisp, senior chief-of-staff addressing a workspace admin as a peer; "
    "signal over noise; quantifies everything; every item ends with a concrete "
    "recommended action; never alarmist, never a generic system alert"
)

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


def odette_flag_summary(flag_type: str, detail: dict) -> str:
    """Deterministic Odette-voice one-liner for a rule flag (no LLM)."""
    d = detail or {}
    if flag_type == "tier_seat_exceeded":
        return (
            f"You're carrying {d.get('active_members')} active members on a "
            f"{d.get('tier')} plan that seats {d.get('seats')}. Add seats or remove "
            f"{d.get('active_members', 0) - d.get('seats', 0)} member(s) to clear it."
        )
    if flag_type == "daily_publish_cap":
        return (
            f"Publishing hit {d.get('count')} in the last 24h against a {d.get('cap')}/day "
            f"cap on the {d.get('tier')} plan. Throttle scheduling or move up a tier."
        )
    if flag_type == "rbac_violation":
        return (
            f"A '{d.get('actor_role')}' performed '{d.get('event_type')}', which that role "
            f"isn't permitted to do. Review the member's role and how the action was made."
        )
    if flag_type == "brand_voice_instability":
        return (
            f"The brand voice was edited {d.get('count')} times in 24h. Rapid churn here "
            f"destabilises every member's output — consolidate the changes and lock it."
        )
    if flag_type == "member_churn":
        return (
            f"{d.get('summary', 'Unusual membership change')}. Confirm this was intended "
            f"and that access was cleaned up."
        )
    if flag_type == "assistant_signal_storm":
        return (
            f"{d.get('summary', 'A burst of assistant signals')}. Worth a look — it usually "
            f"means a shared cause (a brief change, a bad template, an automation loop)."
        )
    return f"{flag_type.replace('_', ' ')}: {d}"
