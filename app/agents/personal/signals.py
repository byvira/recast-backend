"""Emitting a personal-assistant signal.

One signal = one row in ``personal_signals`` (the member reads these) + one
``assistant.signal`` event on the bus (the supervisor consumes these). Both are
written here so the two never diverge.

``member_message`` is in Remy's voice - warm, first-person peer, lowercase-
comfortable, candid but never corporate. ``supervisor_note`` is the opposite:
one terse factual line, no persona, for Odette's digest.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from app.db.mongo import personal_signals
from app.models.agent_events import (
    AssistantSignalPayload,
    ContentRef,
    EventType,
    SignalMetric,
    SignalWindow,
)
from app.shared.events import emit_event

logger = logging.getLogger(__name__)


async def emit_signal(
    *,
    workspace_id: str,
    user_id: str,
    pipeline_type: Optional[str],
    signal_type: str,
    severity: str,
    metric: dict,
    window: dict,
    evidence_refs: list[dict],
    member_message: str,
    supervisor_note: str,
) -> str:
    """Persist the signal and put it on the bus. Returns the signal id.

    Never raises - a telemetry failure must not break the consumer loop.
    """
    signal_id = str(uuid4())
    now = datetime.now(timezone.utc)
    doc = {
        "_id": signal_id,
        "workspace_id": workspace_id,
        "user_id": user_id,                 # the member the signal is ABOUT
        "pipeline_type": pipeline_type,
        "signal_type": signal_type,
        "severity": severity,
        "metric": metric,
        "window": window,
        "evidence_refs": evidence_refs,
        "member_message": member_message,
        "supervisor_note": supervisor_note,
        "status": "open",
        "created_at": now,
        "resolved_at": None,
    }
    try:
        await personal_signals.insert_one(doc)
    except Exception as exc:  # noqa: BLE001
        logger.error("emit_signal: personal_signals insert failed: %s", exc)

    try:
        payload = AssistantSignalPayload(
            signal_type=signal_type,
            severity=severity,
            metric=SignalMetric(**metric),
            window=SignalWindow(**window),
            evidence_refs=[ContentRef(**r) for r in evidence_refs],
            member_message=member_message,
            supervisor_note=supervisor_note,
        )
        await emit_event(
            event_type=EventType.ASSISTANT_SIGNAL,
            pipeline_type=pipeline_type,     # pipeline of the observed content, or None
            workspace_id=workspace_id,
            actor_user_id=user_id,           # subject of the signal
            actor_role="",
            payload=payload,
            idempotency_key=f"assistant.signal:{signal_id}",
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("emit_signal: bus emit failed for signal %s: %s", signal_id, exc)

    logger.info(
        "personal signal: ws=%s user=%s type=%s severity=%s",
        workspace_id, user_id, signal_type, severity,
    )
    return signal_id


# ─────────────────────────────────────────────────────────────────────────────
# Remy-voice copy. Templated (no LLM) for the cheap paths; the drift-judge path
# folds the model's one-line "why" into `extra`.
# ─────────────────────────────────────────────────────────────────────────────

def remy_message(signal_type: str, *, ctx: dict) -> str:
    sim = ctx.get("similarity")
    if signal_type == "voice_drift":
        why = ctx.get("why")
        base = (
            "hey - this one reads a little off from how you usually sound"
            + (f" ({why})" if why else "")
            + ". want me to pull it back toward your normal voice, or is the shift on purpose here?"
        )
        return base
    if signal_type == "voice_drift_trend":
        return (
            "small heads-up: your last few pieces have been drifting away from your "
            "established voice bit by bit - nothing dramatic in any single one, but the "
            "trend's there. worth a look before it settles in."
        )
    if signal_type == "volume_spike":
        return (
            f"you've published a lot more than usual today ({ctx.get('today')} vs your "
            f"~{ctx.get('mean')}/day average). all good if it's intentional - just flagging "
            "in case something's firing on repeat."
        )
    if signal_type == "volume_drop":
        return (
            "noticed you've gone quiet the last few days after a steady stretch. no pressure "
            "— just here when you want to pick it back up."
        )
    if signal_type == "topic_shift":
        return (
            "your recent pieces have moved onto pretty different topics than what you'd been "
            "covering. if you're deliberately pivoting, ignore this - otherwise you might be "
            "drifting off your usual lane."
        )
    if signal_type == "quality_regression":
        return (
            "a higher share of your recent drafts got flagged for review than normal. might be "
            "worth slowing down a touch on the next few."
        )
    return "flagging something worth a look on your recent content."


def supervisor_note(signal_type: str, *, ctx: dict) -> str:
    if signal_type == "voice_drift":
        return f"voice drift: cosine {ctx.get('similarity'):.2f} vs baseline {ctx.get('baseline'):.2f}"
    if signal_type == "voice_drift_trend":
        return (
            f"voice drift trend: recent avg {ctx.get('recent_avg'):.2f} vs "
            f"baseline avg {ctx.get('baseline_avg'):.2f}"
        )
    if signal_type == "volume_spike":
        return f"volume spike: {ctx.get('today')} today vs mean {ctx.get('mean')} (+{ctx.get('sigma')}σ)"
    if signal_type == "volume_drop":
        return f"volume drop: 0 pieces for {ctx.get('zero_days')}d after ~{ctx.get('baseline_per_day')}/day"
    if signal_type == "topic_shift":
        return f"topic shift: keyword Jaccard {ctx.get('jaccard'):.2f} (floor {ctx.get('floor')})"
    if signal_type == "quality_regression":
        return (
            f"quality regression: recent flag rate {ctx.get('recent_rate'):.2f} vs "
            f"baseline {ctx.get('baseline_rate'):.2f}"
        )
    return f"{signal_type}"
