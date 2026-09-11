"""Deterministic hard-limit rules — no LLM. Run every minute by
``supervisor_rules_tick``.

Each rule reads ``workspace_events`` (and a little membership/tier state) over a
lookback window and returns zero or more *flag descriptors*. Persisting +
de-duping against already-open flags is the caller's job (``ticks.py``).

All numeric thresholds live in ``thresholds.py`` and are provisional.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

from app.agents.supervisor import thresholds as T
from app.agents.supervisor.personas import odette_flag_summary
from app.core.rbac import ROLE_PERMISSIONS
from app.db.mongo import personal_signals, workspace_events, workspace_members, workspaces

logger = logging.getLogger(__name__)

# event_type → the permission the acting role must hold to have done it legitimately
EVENT_REQUIRED_PERMISSION: dict[str, str] = {
    "content.created": "create_content",
    "content.updated": "edit_content",
    "content.published": "publish_content",
    "brand.voice_updated": "edit_brand_voice",
    "member.added": "invite_members",
    "member.removed": "remove_members",
    "role.changed": "manage_roles",
    "tier.changed": "manage_billing",
}


async def _flag(flag_type: str, severity: str, detail: dict, metric: dict, language: str = "en") -> dict:
    return {
        "flag_type": flag_type,
        "detection": "rule",
        "severity": severity,
        "summary_persona": await odette_flag_summary(flag_type, detail, language=language),
        "detail": detail,
        "metric": metric,
    }


async def evaluate_rules(workspace_id: str, language: str = "en") -> list[dict]:
    """Return every rule flag that currently applies to *workspace_id*.

    The caller drops any whose (flag_type) already has an OPEN flag.

    ``language`` — was previously never threaded here at all (odette_flag_summary
    always got the "en" default even though the function itself was already
    language-keyed); found via audit and fixed here, at every flag() call site
    below, in _signal_storm(), and in ticks.py's caller (run_rules_for_workspace).
    """
    # Local closure so the 8 call sites below don't each need language=language
    # appended by hand. Named distinctly from the module-level _flag to avoid
    # the self-reference bug an earlier pass of this fix introduced (a lambda
    # named `flag` calling a module function also named `flag` shadows itself).
    async def flag(flag_type: str, severity: str, detail: dict, metric: dict) -> dict:
        return await _flag(flag_type, severity, detail, metric, language=language)

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=T.RULE_LOOKBACK_HOURS)

    ws = await workspaces.find_one({"id": workspace_id}) or {}
    tier = ws.get("tier", "")
    seats = int((ws.get("tier_config") or {}).get("seats", 0) or 0)
    active_members = await workspace_members.count_documents(
        {"workspace_id": workspace_id, "status": "active"}
    )

    events = await workspace_events.find(
        {"workspace_id": workspace_id, "occurred_at": {"$gte": since.isoformat()}}
    ).to_list(length=5000)

    by_type: Counter = Counter(e.get("event_type") for e in events)
    flags: list[dict] = []

    # ── 1. tier_seat_exceeded (critical) ──────────────────────────────
    if seats and active_members > seats:
        flags.append(await flag(
            "tier_seat_exceeded", "critical",
            {"tier": tier, "seats": seats, "active_members": active_members},
            {"name": "active_members", "value": float(active_members), "limit": float(seats)},
        ))

    # ── 2. daily_publish_cap (warning) ───────────────────────────────
    cap = T.DAILY_PUBLISH_CAP.get(tier, T.DEFAULT_PUBLISH_CAP)
    published = by_type.get("content.published", 0)
    if published > cap:
        flags.append(await flag(
            "daily_publish_cap", "warning",
            {"tier": tier, "cap": cap, "count": published, "window_hours": T.RULE_LOOKBACK_HOURS},
            {"name": "publishes_24h", "value": float(published), "limit": float(cap)},
        ))

    # ── 3. rbac_violation (critical) — any event a role couldn't legitimately do ──
    for e in events:
        role = e.get("actor_role") or ""
        etype = e.get("event_type") or ""
        needed = EVENT_REQUIRED_PERMISSION.get(etype)
        if not role or not needed:
            continue
        if needed not in ROLE_PERMISSIONS.get(role, set()):
            flags.append(await flag(
                "rbac_violation", "critical",
                {"actor_role": role, "event_type": etype,
                 "actor_user_id": e.get("actor_user_id"), "event_id": e.get("event_id"),
                 "required_permission": needed},
                {"name": "violations", "value": 1.0, "limit": 0.0},
            ))
            break  # one rbac_violation flag per cycle is enough to prompt a review

    # ── 4. brand_voice_instability (warning) ─────────────────────────
    bv = by_type.get("brand.voice_updated", 0)
    if bv >= T.BRAND_VOICE_EDITS_24H:
        flags.append(await flag(
            "brand_voice_instability", "warning",
            {"count": bv, "window_hours": T.RULE_LOOKBACK_HOURS},
            {"name": "brand_voice_edits_24h", "value": float(bv), "limit": float(T.BRAND_VOICE_EDITS_24H)},
        ))

    # ── 5. member_churn (warning) ───────────────────────────────────
    removals = by_type.get("member.removed", 0)
    elevations = [
        e for e in events
        if e.get("event_type") == "role.changed"
        and (e.get("payload") or {}).get("to_role") in T.ELEVATED_ROLES
    ]
    if removals >= T.MEMBER_REMOVALS_24H:
        flags.append(await flag(
            "member_churn", "warning",
            {"summary": f"{removals} members removed in {T.RULE_LOOKBACK_HOURS}h", "removals": removals},
            {"name": "member_removals_24h", "value": float(removals), "limit": float(T.MEMBER_REMOVALS_24H)},
        ))
    elif elevations:
        e0 = elevations[0]
        flags.append(await flag(
            "member_churn", "warning",
            {"summary": f"{len(elevations)} role elevation(s) to owner/admin",
             "subject_user_id": (e0.get("payload") or {}).get("subject_user_id"),
             "to_role": (e0.get("payload") or {}).get("to_role")},
            {"name": "role_elevations_24h", "value": float(len(elevations)), "limit": 0.0},
        ))

    # ── 6. assistant_signal_storm (warning) ─────────────────────────
    storm = await _signal_storm(workspace_id, now, language=language)
    if storm:
        flags.append(storm)

    return flags


async def _signal_storm(workspace_id: str, now: datetime, language: str = "en") -> dict | None:
    since_24h = now - timedelta(hours=24)
    since_short = now - timedelta(hours=T.SIGNAL_STORM_SHARED_TYPE_HOURS)
    sigs = await personal_signals.find(
        {"workspace_id": workspace_id, "created_at": {"$gte": since_24h}}
    ).to_list(length=5000)
    if not sigs:
        return None

    per_member: Counter = Counter(s.get("user_id") for s in sigs)
    worst_member, worst_count = (per_member.most_common(1) or [(None, 0)])[0]
    if worst_count >= T.SIGNAL_STORM_PER_MEMBER_24H:
        return await _flag(
            "assistant_signal_storm", "warning",
            {"summary": f"{worst_count} assistant signals from one member in 24h",
             "member_user_id": worst_member, "count": worst_count},
            {"name": "signals_one_member_24h", "value": float(worst_count),
             "limit": float(T.SIGNAL_STORM_PER_MEMBER_24H)},
            language=language,
        )

    by_type_members: dict[str, set] = {}
    for s in sigs:
        if s.get("created_at") and s["created_at"] >= since_short:
            by_type_members.setdefault(s.get("signal_type"), set()).add(s.get("user_id"))
    for stype, members in by_type_members.items():
        if len(members) >= T.SIGNAL_STORM_SHARED_TYPE_MEMBERS:
            return await _flag(
                "assistant_signal_storm", "warning",
                {"summary": f"'{stype}' signal from {len(members)} members in "
                            f"{T.SIGNAL_STORM_SHARED_TYPE_HOURS}h",
                 "signal_type": stype, "member_count": len(members)},
                {"name": "members_shared_signal", "value": float(len(members)),
                 "limit": float(T.SIGNAL_STORM_SHARED_TYPE_MEMBERS)},
                language=language,
            )
    return None
