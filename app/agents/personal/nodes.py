"""Personal-assistant graph nodes.

Flow (one piece per run):

    load_persona → embed_piece → compute_signals → aux_signals
        → route_drift ─ in_voice ─→ persist_persona
                      ─ soft ─────→ emit_soft ──→ persist_persona
                      ─ judge ────→ judge_drift ─→ persist_persona

Only ``persist_persona`` writes: it folds the piece into the persona doc and
then emits every pending signal (personal_signals row + assistant.signal event).
"""

from __future__ import annotations

import logging
from statistics import mean as _mean, pstdev as _pstdev
from typing import Any

from app.agents.personal import persona_store, signals
from app.agents.personal import style as style_mod
from app.agents.personal import thresholds as T
from app.agents.personal.history import iter_member_content, known_pipeline
from app.agents.personal.state import PersonaState
from app.shared.llm import GroqModel, call_llm_structured, cosine_similarity, embed_text

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 1. load_persona
# ─────────────────────────────────────────────────────────────────────────────

async def load_persona_node(state: PersonaState) -> dict:
    ws, uid, pt, now = state["workspace_id"], state["user_id"], state["pipeline_type"], state["now"]

    existing = await persona_store.load(ws, uid)
    is_new = existing is None
    persona = existing or persona_store.new_persona(ws, uid, now)

    # Recent history for the volume/topic/quality baselines. Cross-pipeline
    # (pipeline_type=None) so the persona spans every medium the member uses.
    try:
        history = await iter_member_content(ws, uid, pipeline_type=None, limit=T.TOPIC_BASELINE_N)
    except Exception as exc:  # noqa: BLE001
        logger.error("load_persona: history fetch failed for %s/%s: %s", ws, uid, exc)
        history = []

    if not known_pipeline(pt):
        logger.info("personal graph: unknown pipeline_type=%r — persona counters still update", pt)

    return {"persona": persona, "is_new": is_new, "history": history}


# ─────────────────────────────────────────────────────────────────────────────
# 2. embed_piece
# ─────────────────────────────────────────────────────────────────────────────

async def embed_piece_node(state: PersonaState) -> dict:
    text = state["content_text"]
    if not text.strip():
        return {"embedding": [], "errors": state["errors"] + ["empty content_text — skipping drift scoring"]}
    vec = await embed_text(text)
    if not vec:
        return {"embedding": [], "errors": state["errors"] + ["embedding unavailable — skipping drift scoring"]}
    return {"embedding": vec}


# ─────────────────────────────────────────────────────────────────────────────
# 3. compute_signals — voice drift (single piece + trend)
# ─────────────────────────────────────────────────────────────────────────────

def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def compute_signals_node(state: PersonaState) -> dict:
    """Score voice drift two ways (adaptive embedding + style divergence) and
    route on the stronger of the two."""
    persona = state["persona"]
    voice = persona.get("voice", {})
    baseline_vec = voice.get("baseline_embedding") or []
    pieces_observed = int(persona.get("lifetime", {}).get("pieces_observed", 0))
    emb = state["embedding"]

    baseline_ready = bool(baseline_vec) and pieces_observed >= T.ASSIST_MIN_PIECES
    if not baseline_ready:
        return {"similarity": 1.0, "baseline_present": False, "drift_route": "in_voice",
                "drift_score": 0.0, "style_divergence": 0.0}

    sim = cosine_similarity(emb, baseline_vec) if emb else 1.0

    # ── (1) adaptive embedding component: stddevs below the member's OWN norm ──
    recent = [s for s in voice.get("recent_similarities", []) if s < 0.9999]  # drop bootstrap sentinels
    emb_comp = 0.0
    if len(recent) >= T.EMB_Z_MIN_SAMPLES:
        mu = _mean(recent)
        sd = max(_pstdev(recent) if len(recent) > 1 else 0.0, T.EMB_Z_MIN_STD)
        emb_comp = _clamp((mu - sim) / sd / T.EMB_Z_FULL_SIGMA, 0.0, 2.0)
    elif emb:
        # no personal norm yet — fall back to the absolute-cosine backstops,
        # and stay conservative (only the clearly-low range contributes).
        if sim < T.SIM_SOFT_FLOOR:
            emb_comp = 1.0 + _clamp(
                (T.SIM_SOFT_FLOOR - sim) / max(T.SIM_SOFT_FLOOR - T.SIM_HARD_DRIFT, 1e-6), 0.0, 1.0
            )
        elif sim < T.SIM_IN_VOICE:
            emb_comp = 0.5 * (T.SIM_IN_VOICE - sim) / (T.SIM_IN_VOICE - T.SIM_SOFT_FLOOR)

    # ── (2) style / register divergence ────────────────────────────────
    sty_div = style_mod.style_divergence(
        style_mod.fingerprint(state["content_text"]), persona.get("style_fingerprint", {})
    )
    sty_comp = _clamp(sty_div / T.STYLE_DIV_SOFT, 0.0, 3.0)

    drift_score = max(emb_comp, sty_comp)
    if emb and sim < T.SIM_HARD_DRIFT:
        drift_score = max(drift_score, 2.0)

    route = "in_voice" if drift_score < 1.0 else ("soft" if drift_score < 2.0 else "judge")

    pending = list(state["pending_signals"])

    # ── trend: slow drift that never trips a single-piece threshold ────
    recent_hist = recent + [sim]
    if len(recent_hist) >= T.TREND_BASELINE_N:
        baseline_avg = _mean(recent_hist[-T.TREND_BASELINE_N:])
        recent_avg = _mean(recent_hist[-T.TREND_RECENT_N:])
        if baseline_avg - recent_avg > T.TREND_DROP:
            ctx = {"recent_avg": recent_avg, "baseline_avg": baseline_avg}
            pending.append({
                "signal_type": "voice_drift_trend",
                "severity": "low",
                "metric": {"name": "recent_avg_cosine", "value": round(recent_avg, 4),
                           "baseline": round(baseline_avg, 4), "threshold": T.TREND_DROP},
                "window": {"kind": "trend", "n": T.TREND_RECENT_N},
                "evidence_refs": _evidence(state),
                "member_message": signals.remy_message("voice_drift_trend", ctx=ctx),
                "supervisor_note": signals.supervisor_note("voice_drift_trend", ctx=ctx),
                "is_drift": True,
            })

    logger.debug(
        "compute_signals: sim=%.4f emb_comp=%.2f sty_div=%.2f sty_comp=%.2f drift_score=%.2f route=%s",
        sim, emb_comp, sty_div, sty_comp, drift_score, route,
    )
    return {
        "similarity": round(sim, 4),
        "baseline_present": True,
        "drift_route": route,
        "drift_score": round(drift_score, 3),
        "style_divergence": sty_div,
        "pending_signals": pending,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 4. aux_signals — volume / topic / quality
# ─────────────────────────────────────────────────────────────────────────────

def aux_signals_node(state: PersonaState) -> dict:
    persona = state["persona"]
    history = state["history"]
    now = state["now"]
    pending = list(state["pending_signals"])

    pending += _volume_signals(persona, now, state)
    pending += _topic_signals(persona, history, state)
    pending += _quality_signals(persona, history, state)

    return {"pending_signals": pending}


def _volume_signals(persona: dict, now, state: PersonaState) -> list[dict]:
    """Per-piece volume check. Only ``volume_spike`` lives here.

    ``volume_drop`` is deliberately NOT emitted from this graph: it means "a
    member who used to post regularly has gone quiet", which is the absence of
    events — you cannot observe it from a content.created event (the member is
    posting *right now*). It needs a periodic sweep over every persona. That
    sweep is a follow-up (a natural fit for the supervisor tick added in
    Stage 2, or a dedicated personal cron); the threshold constants for it stay
    in thresholds.py so the logic can move without a rewrite.
    """
    vs = persona.get("volume_stats", {})
    daily = dict(vs.get("daily_counts", {}))
    today = now.strftime("%Y-%m-%d")
    today_count = daily.get(today, 0) + 1                      # include the piece we're processing
    mean_v = float(vs.get("mean", 0.0) or 0.0)
    stddev_v = float(vs.get("stddev", 0.0) or 0.0)
    out: list[dict] = []

    # spike — needs a real baseline (mean and some spread) to avoid noise on
    # a persona that has only ever been active on one or two days.
    if (
        stddev_v > 0.0
        and mean_v >= 1.0
        and len(daily) >= 3
        and today_count >= T.VOLUME_SPIKE_MIN_COUNT
        and today_count > mean_v + T.VOLUME_SPIKE_SIGMA * stddev_v
    ):
        ctx = {"today": today_count, "mean": round(mean_v, 1), "sigma": T.VOLUME_SPIKE_SIGMA}
        out.append(_sig(
            state, "volume_spike", "medium",
            metric={"name": "pieces_today", "value": float(today_count),
                    "baseline": round(mean_v, 3), "threshold": round(mean_v + T.VOLUME_SPIKE_SIGMA * stddev_v, 3)},
            window={"kind": "rolling", "n": T.VOLUME_WINDOW_DAYS}, ctx=ctx,
        ))
    return out


def _topic_signals(persona: dict, history: list[dict], state: PersonaState) -> list[dict]:
    if len(history) < max(T.TOPIC_RECENT_N * 2, 10):
        return []
    recent_texts = [state["content_text"]] + [h["text"] for h in history[: T.TOPIC_RECENT_N - 1]]
    base_texts = [h["text"] for h in history[: T.TOPIC_BASELINE_N]]
    recent_kw = set(style_mod.keywords(" ".join(recent_texts), T.TOPIC_KEYWORDS_PER_PIECE * 3))
    base_kw = set(style_mod.keywords(" ".join(base_texts), T.TOPIC_KEYWORDS_PER_PIECE * 5))
    j = style_mod.jaccard(recent_kw, base_kw)
    if j < T.TOPIC_JACCARD_FLOOR:
        ctx = {"jaccard": j, "floor": T.TOPIC_JACCARD_FLOOR}
        return [_sig(
            state, "topic_shift", "low",
            metric={"name": "keyword_jaccard", "value": round(j, 4), "baseline": 1.0,
                    "threshold": T.TOPIC_JACCARD_FLOOR},
            window={"kind": "rolling", "n": T.TOPIC_RECENT_N}, ctx=ctx,
        )]
    return []


def _quality_signals(persona: dict, history: list[dict], state: PersonaState) -> list[dict]:
    trailing = history[: T.QUALITY_TRAILING_N - 1]
    if len(trailing) < max(T.QUALITY_TRAILING_N // 2, 4):
        return []
    flags = [1 if h.get("flagged_for_review") else 0 for h in trailing]
    flags.append(1 if state["flagged_for_review"] else 0)
    recent_rate = sum(flags) / len(flags)
    baseline_rate = float(persona.get("quality_stats", {}).get("baseline_flag_rate", 0.0) or 0.0)
    if recent_rate >= T.QUALITY_FLAG_RATE_TRIGGER and baseline_rate < T.QUALITY_BASELINE_FLAG_RATE_MAX:
        ctx = {"recent_rate": recent_rate, "baseline_rate": baseline_rate}
        return [_sig(
            state, "quality_regression", "medium",
            metric={"name": "trailing_flag_rate", "value": round(recent_rate, 4),
                    "baseline": round(baseline_rate, 4), "threshold": T.QUALITY_FLAG_RATE_TRIGGER},
            window={"kind": "rolling", "n": T.QUALITY_TRAILING_N}, ctx=ctx,
        )]
    return []


# ─────────────────────────────────────────────────────────────────────────────
# 5. routing + drift-signal nodes
# ─────────────────────────────────────────────────────────────────────────────

def route_drift(state: PersonaState) -> str:
    return state.get("drift_route", "in_voice")


def _drift_metric(state: PersonaState) -> dict:
    return {
        "name": "cosine_similarity",
        "value": state["similarity"],
        "baseline": _baseline_sim_ref(state),
        "threshold": T.SIM_SOFT_FLOOR,
        # extra context — kept on the stored signal, ignored by the event's SignalMetric
        "style_divergence": state.get("style_divergence", 0.0),
        "drift_score": state.get("drift_score", 0.0),
    }


def emit_soft_node(state: PersonaState) -> dict:
    ctx = {"similarity": state["similarity"], "baseline": _baseline_sim_ref(state),
           "why": _register_hint(state)}
    sig = _sig(
        state, "voice_drift", "low",
        metric=_drift_metric(state),
        window={"kind": "rolling", "n": T.BASELINE_MAX_SAMPLES}, ctx=ctx,
    )
    return {"pending_signals": state["pending_signals"] + [sig]}


_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}
_SEVERITY_NAME = {0: "low", 1: "medium", 2: "high"}


def _max_sev(a: str, b: str) -> str:
    return _SEVERITY_NAME[max(_SEVERITY_RANK.get(a, 1), _SEVERITY_RANK.get(b, 1))]


async def judge_drift_node(state: PersonaState) -> dict:
    """Escalation path: the combined drift score put this in the >=2 band.

    The detector (adaptive embedding + style divergence) has ALREADY decided
    this is drift — a ``voice_drift`` signal is always emitted here. The Groq
    call does not get a veto; it only *describes* the difference and proposes a
    severity, which is then floored by the hard embedding/style evidence.
    """
    exemplars = [h["text"] for h in state["history"][:3] if h.get("text")]
    sim = state["similarity"]
    sty_div = state.get("style_divergence", 0.0)
    llm_sev = "medium"
    why = ""

    if exemplars:
        prompt = (
            "A voice-consistency check flagged the NEW PIECE as off the author's usual "
            "voice. Using their recent pieces as the baseline, describe the difference.\n\n"
            "BASELINE PIECES:\n"
            + "\n---\n".join(t[:1200] for t in exemplars)
            + "\n\nNEW PIECE:\n" + state["content_text"][:1800]
            + '\n\nReturn JSON only: {"severity": "low"|"medium"|"high", '
              '"why": "<=15 words, concrete, e.g. \'far more formal, much longer sentences\'"}'
        )
        try:
            res = await call_llm_structured(prompt=prompt, model=GroqModel.BALANCED)
            if isinstance(res, dict) and res:
                why = str(res.get("why", ""))[:160]
                sev = str(res.get("severity", "")).lower()
                if sev in _SEVERITY_RANK:
                    llm_sev = sev
        except Exception as exc:  # noqa: BLE001
            logger.error("judge_drift: LLM enrichment failed, using deterministic fallback: %s", exc)

    # Floor the severity by the strength of the hard evidence.
    severity = llm_sev
    if sim < T.SIM_SOFT_FLOOR or sty_div >= T.STYLE_DIV_SOFT * 2:
        severity = _max_sev(severity, "medium")
    if sim < T.SIM_HARD_DRIFT or sty_div >= T.STYLE_DIV_SOFT * 3:
        severity = "high"

    # The Groq "why" is unreliable on this comparison. When the drift is
    # style-driven, the deterministic fingerprint hint is more trustworthy and
    # specific; only fall back to the model's phrasing for embedding-only drift.
    style_hint = _register_hint(state)
    if style_hint and sty_div >= T.STYLE_DIV_SOFT:
        why = style_hint
    else:
        why = why or style_hint or "reads differently from your usual voice"
    ctx = {"similarity": sim, "baseline": _baseline_sim_ref(state), "why": why}
    sig = _sig(
        state, "voice_drift", severity,
        metric=_drift_metric(state),
        window={"kind": "rolling", "n": T.BASELINE_MAX_SAMPLES}, ctx=ctx,
    )
    return {"judge_why": why, "pending_signals": state["pending_signals"] + [sig]}


def _register_hint(state: PersonaState) -> str:
    """A short, deterministic 'why' when the LLM gives nothing usable — derived
    from the style fingerprint so the member still gets something concrete."""
    fp = style_mod.fingerprint(state["content_text"])
    base = state["persona"].get("style_fingerprint", {})
    bits = []
    bg, pg = float(base.get("reading_grade", 0) or 0), fp["reading_grade"]
    if bg and abs(pg - bg) >= 4:
        bits.append("more formal" if pg > bg else "more casual")
    bl, pl = float(base.get("avg_sentence_len", 0) or 0), fp["avg_sentence_len"]
    if bl and pl >= bl * 1.6:
        bits.append("much longer sentences")
    elif bl and pl <= bl * 0.6:
        bits.append("much shorter sentences")
    return ", ".join(bits)


# ─────────────────────────────────────────────────────────────────────────────
# 6. persist_persona — the only writer
# ─────────────────────────────────────────────────────────────────────────────

async def persist_persona_node(state: PersonaState) -> dict:
    persona = state["persona"]
    now = state["now"]

    persona_store.apply_piece(
        persona,
        embedding=state["embedding"],
        text=state["content_text"],
        piece_id=state["piece_id"],
        pipeline_type=state["pipeline_type"],
        quality_passed=state["quality_passed"],
        flagged=state["flagged_for_review"],
        # only record a real per-piece cosine; None while the baseline is still
        # forming so the running mean/stddev isn't poisoned by the 1.0 sentinel.
        similarity=state["similarity"] if state["baseline_present"] else None,
        recent_history_rows=state["history"],
        now=now,
    )

    for sig in state["pending_signals"]:
        if sig.get("is_drift"):
            persona_store.append_drift_history(
                persona,
                signal_type=sig["signal_type"],
                similarity=sig["metric"].get("value", state["similarity"]),
                severity=sig["severity"],
                now=now,
            )

    await persona_store.persist(persona)

    emitted = []
    for sig in state["pending_signals"]:
        sid = await signals.emit_signal(
            workspace_id=state["workspace_id"],
            user_id=state["user_id"],
            pipeline_type=state["pipeline_type"],
            signal_type=sig["signal_type"],
            severity=sig["severity"],
            metric=sig["metric"],
            window=sig["window"],
            evidence_refs=sig["evidence_refs"],
            member_message=sig["member_message"],
            supervisor_note=sig["supervisor_note"],
        )
        emitted.append(sid)

    logger.info(
        "personal graph done: ws=%s user=%s piece=%s sim=%s route=%s signals=%d new_persona=%s",
        state["workspace_id"], state["user_id"], state["piece_id"],
        state["similarity"], state["drift_route"], len(emitted), state["is_new"],
    )
    # LangGraph requires a non-empty update from every node.
    return {"emitted_signal_ids": emitted}


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────

def _evidence(state: PersonaState) -> list[dict]:
    ref = (state["event"].get("payload") or {}).get("content_ref")
    if isinstance(ref, dict) and ref.get("id"):
        return [ref]
    if state["piece_id"]:
        return [{"collection": "content_pieces", "id": state["piece_id"]}]
    return []


def _baseline_sim_ref(state: PersonaState) -> float:
    """A representative 'in-voice' similarity for the metric baseline field —
    the mean of the member's recent per-piece cosines, or the in-voice cutoff."""
    sims = state["persona"].get("voice", {}).get("recent_similarities", [])
    return round(_mean(sims), 4) if sims else T.SIM_IN_VOICE


def _sig(state: PersonaState, signal_type: str, severity: str, *, metric: dict, window: dict, ctx: dict) -> dict:
    return {
        "signal_type": signal_type,
        "severity": severity,
        "metric": metric,
        "window": window,
        "evidence_refs": _evidence(state),
        "member_message": signals.remy_message(signal_type, ctx=ctx),
        "supervisor_note": signals.supervisor_note(signal_type, ctx=ctx),
        "is_drift": signal_type in ("voice_drift", "voice_drift_trend"),
    }
