"""Deterministic pre-aggregation of a workspace's recent activity.

The reasoning pass gets ONE compact digest, not a raw event stream — this keeps
the Groq call cheap and bounded. Everything here is generic over
``pipeline_type`` (``by_pipeline`` is just a Counter), so a new pipeline shows
up automatically with no change.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any


def build_digest(
    workspace_id: str,
    *,
    events: list[dict],
    signals: list[dict],
    workspace: dict,
    active_members: int,
    open_flags: list[dict],
) -> dict:
    by_type: Counter = Counter(e.get("event_type") for e in events)
    by_pipeline: Counter = Counter((e.get("pipeline_type") or "none") for e in events)
    by_actor: Counter = Counter(e.get("actor_user_id") for e in events if e.get("actor_user_id"))

    brand_edits = [
        (e.get("payload") or {}).get("diff_summary") or "(no summary)"
        for e in events if e.get("event_type") == "brand.voice_updated"
    ]
    role_changes = [
        {
            "subject": (e.get("payload") or {}).get("subject_user_id"),
            "from": (e.get("payload") or {}).get("from_role"),
            "to": (e.get("payload") or {}).get("to_role"),
        }
        for e in events if e.get("event_type") == "role.changed"
    ]

    signal_rollup: dict[str, Counter] = {}
    signal_severity: Counter = Counter()
    for s in signals:
        signal_rollup.setdefault(s.get("user_id", "?"), Counter())[s.get("signal_type", "?")] += 1
        signal_severity[s.get("severity", "?")] += 1

    tier = workspace.get("tier", "")
    seats = int((workspace.get("tier_config") or {}).get("seats", 0) or 0)

    return {
        "workspace_id": workspace_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_events": len(events),
        "event_counts": dict(by_type),
        "by_pipeline": dict(by_pipeline),
        "top_actors": by_actor.most_common(5),
        "publishes_last_24h": by_type.get("content.published", 0),
        "brand_voice_edits": brand_edits[:6],
        "role_changes": role_changes[:6],
        "tier": tier,
        "seats": seats,
        "active_members": active_members,
        "seat_headroom": (seats - active_members) if seats else None,
        "open_flags": [
            {"type": f.get("flag_type"), "severity": f.get("severity"), "detection": f.get("detection")}
            for f in open_flags
        ],
        "assistant_signal_total": len(signals),
        "assistant_signal_by_severity": dict(signal_severity),
        "assistant_signals_by_member": {
            uid: dict(c) for uid, c in list(signal_rollup.items())[:15]
        },
        "member_supervisor_notes": [
            s.get("supervisor_note") for s in signals if s.get("supervisor_note")
        ][:20],
    }


def digest_is_quiet(digest: dict) -> bool:
    """True when there's genuinely nothing for the reasoning pass to chew on."""
    return (
        digest.get("window_events", 0) == 0
        and digest.get("assistant_signal_total", 0) == 0
        and not digest.get("open_flags")
    )
