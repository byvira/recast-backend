"""Active assistance — Remy helping a member *while they create*.

``align_draft`` is user-initiated (an explicit request), so a Groq call for
rewrite suggestions is fine here. ``cached_nudge`` is the opposite: a single
indexed ``find_one``, no LLM, wired into pipeline responses behind a 150 ms
timeout so it can never add latency.
"""

from __future__ import annotations

import logging
from typing import Optional

from app.agents.personal import persona_store
from app.agents.personal import style as style_mod
from app.agents.personal import thresholds as T
from app.core.tracing import traced_agent
from app.db.mongo import personal_signals
from app.shared.llm import GroqModel, call_llm_structured, cosine_similarity, embed_text

logger = logging.getLogger(__name__)


async def align_draft(
    *,
    workspace_id: str,
    user_id: str,
    pipeline_type: Optional[str],
    draft_text: str,
    target: str = "",
) -> dict:
    """Compare a work-in-progress draft to the member's established voice.

    Traced to LangSmith as ``agent:assist`` / ``ws:<workspace_id>`` — the
    ``embed_text`` and ``call_llm_structured`` calls inside show up as child runs.
    """
    with traced_agent("assist", workspace_id=workspace_id, user_id=user_id,
                      pipeline_type=pipeline_type, target=target or None):
        return await _align_draft_impl(
            workspace_id=workspace_id, user_id=user_id,
            pipeline_type=pipeline_type, draft_text=draft_text, target=target,
        )


async def _align_draft_impl(
    *,
    workspace_id: str,
    user_id: str,
    pipeline_type: Optional[str],
    draft_text: str,
    target: str = "",
) -> dict:
    persona = await persona_store.load(workspace_id, user_id)
    pieces = int((persona or {}).get("lifetime", {}).get("pieces_observed", 0))

    if not persona or pieces < T.ASSIST_MIN_PIECES:
        return {
            "status": "learning",
            "persona_name": "Remy",
            "in_voice": None,
            "similarity": None,
            "deltas": [],
            "remy_message": (
                "i'm still learning how you write — give me a few more pieces and i'll be "
                "able to tell you when a draft drifts from your usual voice."
            ),
            "suggested_openers": [],
            "rewrite_hint": "",
            "pieces_observed": pieces,
        }

    baseline_vec = persona.get("voice", {}).get("baseline_embedding") or []
    draft_vec = await embed_text(draft_text)
    sim = cosine_similarity(draft_vec, baseline_vec) if (draft_vec and baseline_vec) else None
    in_voice = (sim is not None) and (sim >= T.SIM_SOFT_FLOOR)

    deltas = style_mod.style_deltas(style_mod.fingerprint(draft_text), persona.get("style_fingerprint", {}))

    suggested_openers: list[str] = []
    rewrite_hint = ""
    try:
        known_openers = persona.get("style_fingerprint", {}).get("opener_patterns", [])[-5:]
        prompt = (
            "You help an author keep a draft in their own established voice. Return JSON only.\n\n"
            f"THEIR TYPICAL OPENERS: {known_openers}\n"
            f"THEIR STYLE: avg sentence {persona['style_fingerprint'].get('avg_sentence_len')} words, "
            f"emoji/word {persona['style_fingerprint'].get('emoji_rate')}, "
            f"questions/sentence {persona['style_fingerprint'].get('question_rate')}\n"
            f"OBSERVED DIFFERENCES IN THIS DRAFT: {deltas or 'none detected'}\n\n"
            f"DRAFT ({target or 'general'}):\n{draft_text[:1800]}\n\n"
            'Return: {"suggested_openers": ["...","...","..."], '
            '"rewrite_hint": "<=40 words, specific, how to pull it back toward their voice"}'
        )
        res = await call_llm_structured(prompt=prompt, model=GroqModel.BALANCED)
        if isinstance(res, dict):
            suggested_openers = [str(s) for s in (res.get("suggested_openers") or [])][:3]
            rewrite_hint = str(res.get("rewrite_hint", ""))[:400]
    except Exception as exc:  # noqa: BLE001
        logger.error("align_draft: suggestion LLM call failed: %s", exc)

    if in_voice:
        msg = "this reads like you — nothing i'd change on the voice front. go with it."
    else:
        lead = "this is drifting a bit from how you usually sound"
        if deltas:
            lead += f" — {deltas[0]}"
        msg = lead + ". " + (rewrite_hint or "tighten it back toward your normal rhythm and opener.")

    return {
        "status": "ok",
        "persona_name": "Remy",
        "in_voice": in_voice,
        "similarity": round(sim, 4) if sim is not None else None,
        "deltas": deltas,
        "remy_message": msg,
        "suggested_openers": suggested_openers,
        "rewrite_hint": rewrite_hint,
        "pieces_observed": pieces,
    }


async def cached_nudge(workspace_id: str, user_id: str) -> Optional[dict]:
    """Tiny, LLM-free persona read for inline pipeline nudges. Returns ``None``
    if the member has no persona yet (or on any error — callers fall back to
    no nudge)."""
    try:
        persona = await persona_store.load(workspace_id, user_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("cached_nudge: persona load failed: %s", exc)
        return None
    if not persona:
        return None

    voice = persona.get("voice", {})
    sims = voice.get("recent_similarities", [])
    drift_hist = persona.get("drift_history", [])
    sf = persona.get("style_fingerprint", {})

    return {
        "persona_name": persona.get("persona_name", "Remy"),
        "pieces_observed": persona.get("lifetime", {}).get("pieces_observed", 0),
        "recent_similarity_avg": round(sum(sims) / len(sims), 4) if sims else None,
        "last_drift": (
            {
                "at": drift_hist[-1]["at"],
                "signal_type": drift_hist[-1]["signal_type"],
                "severity": drift_hist[-1]["severity"],
            }
            if drift_hist else None
        ),
        "baseline_style": {
            "avg_sentence_len": sf.get("avg_sentence_len"),
            "emoji_rate": sf.get("emoji_rate"),
            "question_rate": sf.get("question_rate"),
            "reading_grade": sf.get("reading_grade"),
        },
    }


async def latest_signal_for_piece(workspace_id: str, user_id: str, piece_id: str) -> Optional[dict]:
    """Most recent signal whose evidence points at *piece_id* — for /assistant/nudge."""
    doc = await personal_signals.find_one(
        {
            "workspace_id": workspace_id,
            "user_id": user_id,
            "evidence_refs.id": piece_id,
        },
        sort=[("created_at", -1)],
    )
    if not doc:
        return None
    doc.pop("_id", None)
    return doc
