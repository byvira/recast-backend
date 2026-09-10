"""The single reader/writer for ``member_personas``.

No other module in the package writes this collection. ``apply_piece`` folds one
observed piece into a persona dict (EWMA voice centroid, style EWMA, topic
histogram, volume/quality stats, drift-history log); ``persist`` upserts it.
Keeping all mutation here is what makes the hybrid "growing profile + bounded
recency baseline" storage model easy to reason about and tune.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.agents.personal import style as style_mod
from app.agents.personal import thresholds as T
from app.db.mongo import member_personas
from app.models.persona import MemberPersona


def persona_id(workspace_id: str, user_id: str) -> str:
    return MemberPersona.make_id(workspace_id, user_id)


def new_persona(workspace_id: str, user_id: str, now: datetime) -> dict:
    """A blank persona skeleton — same shape the graph will keep upserting."""
    return {
        "_id": persona_id(workspace_id, user_id),
        "workspace_id": workspace_id,
        "user_id": user_id,
        "persona_name": "Remy",
        "lifetime": {
            "pieces_observed": 0,
            "first_seen_at": now,
            "last_seen_at": now,
            "by_pipeline": {},
        },
        "voice": {
            "baseline_embedding": [],
            "baseline_sample_ids": [],
            "ewma_lambda": 0.9,          # provisional
            "refreshed_at": None,
            "refreshed_after_piece": 0,
            "recent_similarities": [],
        },
        "style_fingerprint": {
            "avg_sentence_len": 0.0, "avg_word_len": 0.0,
            "opener_patterns": [], "closer_patterns": [],
            "emoji_rate": 0.0, "question_rate": 0.0, "list_rate": 0.0,
            "reading_grade": 0.0,
        },
        "topics": {"keyword_histogram": {}, "top_30_window_ids": []},
        "volume_stats": {"daily_counts": {}, "mean": 0.0, "stddev": 0.0},
        "quality_stats": {"trailing_10_flag_rate": 0.0, "baseline_flag_rate": 0.0},
        "drift_history": [],
        "schema_version": 1,
        "created_at": now,
        "updated_at": now,
    }


async def load(workspace_id: str, user_id: str) -> Optional[dict]:
    return await member_personas.find_one({"_id": persona_id(workspace_id, user_id)})


async def persist(doc: dict) -> None:
    doc["updated_at"] = datetime.now(timezone.utc)
    await member_personas.replace_one({"_id": doc["_id"]}, doc, upsert=True)


# ─────────────────────────────────────────────────────────────────────────────
# Folding one piece into the persona
# ─────────────────────────────────────────────────────────────────────────────

def _ewma_vec(old: list[float], new: list[float], lam: float) -> list[float]:
    if not old:
        return list(new)
    if not new or len(old) != len(new):
        return old
    return [lam * o + (1.0 - lam) * n for o, n in zip(old, new)]


def _ewma_scalar(old: float, new: float, lam: float, *, bootstrap: bool) -> float:
    if bootstrap or old == 0.0:
        return round(float(new), 4)
    return round(lam * old + (1.0 - lam) * new, 4)


def _recompute_volume(daily_counts: dict[str, int]) -> tuple[dict[str, int], float, float]:
    """Trim to the trailing window and recompute mean/stddev of daily counts."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=T.VOLUME_WINDOW_DAYS)).strftime("%Y-%m-%d")
    trimmed = {d: c for d, c in daily_counts.items() if d >= cutoff}
    vals = list(trimmed.values())
    mean = round(sum(vals) / len(vals), 3) if vals else 0.0
    stdev = round(statistics.pstdev(vals), 3) if len(vals) > 1 else 0.0
    return trimmed, mean, stdev


def apply_piece(
    persona: dict,
    *,
    embedding: list[float],
    text: str,
    piece_id: str,
    pipeline_type: Optional[str],
    quality_passed: bool,
    flagged: bool,
    similarity: Optional[float],
    recent_history_rows: list[dict],
    now: datetime,
) -> dict:
    """Return ``persona`` mutated in place with this piece folded in.

    ``recent_history_rows`` is the member's recent pieces (already fetched by
    the graph via the history adapter) — used to recompute the trailing quality
    flag rate without another DB round-trip.
    """
    lam = float(persona["voice"].get("ewma_lambda", 0.9))
    lifetime = persona["lifetime"]
    pieces_before = int(lifetime.get("pieces_observed", 0))
    bootstrap = pieces_before < T.BASELINE_MAX_SAMPLES

    # ── lifetime counters ───────────────────────────────────────────────
    lifetime["pieces_observed"] = pieces_before + 1
    lifetime["last_seen_at"] = now
    lifetime.setdefault("first_seen_at", now)
    if pipeline_type:
        bp = lifetime.setdefault("by_pipeline", {})
        bp[pipeline_type] = bp.get(pipeline_type, 0) + 1

    # ── voice baseline (EWMA centroid, bounded-recency sample list) ─────
    voice = persona["voice"]
    if embedding:
        refreshed_after = int(voice.get("refreshed_after_piece", 0))
        due = (
            not voice.get("baseline_embedding")
            or bootstrap
            or (lifetime["pieces_observed"] - refreshed_after) >= T.BASELINE_REFRESH_EVERY
        )
        if due:
            voice["baseline_embedding"] = _ewma_vec(voice.get("baseline_embedding", []), embedding, lam)
            voice["refreshed_at"] = now
            voice["refreshed_after_piece"] = lifetime["pieces_observed"]
        samples = voice.get("baseline_sample_ids", [])
        samples.append({"collection": _collection_for(pipeline_type), "id": piece_id})
        voice["baseline_sample_ids"] = samples[-T.BASELINE_MAX_SAMPLES:]
        if similarity is not None:
            sims = voice.get("recent_similarities", [])
            sims.append(round(float(similarity), 4))
            voice["recent_similarities"] = sims[-T.RECENT_SIM_KEEP:]

    # ── style EWMA + opener/closer patterns ────────────────────────────
    fp = style_mod.fingerprint(text)
    sf = persona["style_fingerprint"]
    for k in ("avg_sentence_len", "avg_word_len", "emoji_rate", "question_rate", "list_rate", "reading_grade"):
        sf[k] = _ewma_scalar(float(sf.get(k, 0.0) or 0.0), fp[k], lam, bootstrap=bootstrap)
    sf["opener_patterns"] = _bump_pattern(sf.get("opener_patterns", []), style_mod.opener(text))
    sf["closer_patterns"] = _bump_pattern(sf.get("closer_patterns", []), style_mod.closer(text))

    # ── topics ────────────────────────────────────────────────────────
    topics = persona["topics"]
    hist = topics.setdefault("keyword_histogram", {})
    for kw in style_mod.keywords(text, T.TOPIC_KEYWORDS_PER_PIECE):
        hist[kw] = hist.get(kw, 0) + 1
    win = topics.setdefault("top_30_window_ids", [])
    win.append(piece_id)
    topics["top_30_window_ids"] = win[-T.TOPIC_BASELINE_N:]

    # ── volume stats ─────────────────────────────────────────────────
    vs = persona["volume_stats"]
    daily = vs.setdefault("daily_counts", {})
    today = now.strftime("%Y-%m-%d")
    daily[today] = daily.get(today, 0) + 1
    daily, mean, stdev = _recompute_volume(daily)
    vs["daily_counts"], vs["mean"], vs["stddev"] = daily, mean, stdev

    # ── quality stats ───────────────────────────────────────────────
    qs = persona["quality_stats"]
    recent_flags = [1 if f else 0 for f in _recent_flag_series(recent_history_rows)]
    recent_flags.append(1 if flagged else 0)
    recent_flags = recent_flags[-T.QUALITY_TRAILING_N:]
    qs["trailing_10_flag_rate"] = round(sum(recent_flags) / len(recent_flags), 4) if recent_flags else 0.0
    prev_baseline = float(qs.get("baseline_flag_rate", 0.0) or 0.0)
    n = lifetime["pieces_observed"]
    qs["baseline_flag_rate"] = round(
        (prev_baseline * (n - 1) + (1 if flagged else 0)) / n, 4
    )

    persona["updated_at"] = now
    return persona


def append_drift_history(persona: dict, *, signal_type: str, similarity: float, severity: str, now: datetime) -> None:
    hist = persona.setdefault("drift_history", [])
    hist.append({
        "at": now, "signal_type": signal_type,
        "similarity": round(float(similarity), 4), "severity": severity, "resolved": False,
    })
    persona["drift_history"] = hist[-T.DRIFT_HISTORY_CAP:]


# ── small helpers ──────────────────────────────────────────────────────

def _collection_for(pipeline_type: Optional[str]) -> str:
    from app.agents.personal.history import PIPELINE_SOURCES
    src = PIPELINE_SOURCES.get(pipeline_type or "")
    return getattr(src.collection, "name", "content_pieces") if src else "content_pieces"


def _bump_pattern(patterns: list[str], value: str, keep: int = 8) -> list[str]:
    """Track recurring opener/closer phrases as a most-recent-wins short list."""
    if not value:
        return patterns
    out = [p for p in patterns if p != value]
    out.append(value)
    return out[-keep:]


def _recent_flag_series(rows: list[dict]) -> list[bool]:
    return [bool(r.get("flagged_for_review", False)) for r in rows if isinstance(r, dict)]
