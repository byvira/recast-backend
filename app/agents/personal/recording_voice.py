"""Remy learning a member's voice from their own recordings, on request.

It is opt-in per member and off by default. It reads only the transcripts that already exist for videos and recordings the member
uploaded themselves, so nothing is transcribed again. From each source it takes a few evenly spaced passages, embeds them
(a small, bounded number of embedding calls) and keeps one averaged voice and a short style summary. The recordings and their words
are not stored. The learned voice lives in its own record, so Forget removes everything it learned and nothing else, and it is used
only for the video voice check.

Limits: at most MAX_SOURCES sources and MAX_PASSAGES passages per run, new sources only, and one run an hour."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from app.agents.personal import style as style_mod
from app.agents.personal import thresholds as T
from app.db.mongo import audio_assets, get_db, media_assets

logger = logging.getLogger(__name__)

MAX_SOURCES = 10
MAX_PASSAGES = 12
PASSAGES_PER_SOURCE = 3
PASSAGE_WORDS = 120
MIN_PASSAGE_WORDS = 40
MIN_PASSAGES_TO_USE = 3
RUN_EVERY = timedelta(hours=1)

Embed = Callable[[str], Awaitable[list[float]]]


def _store():
    return get_db()["remy_recording_voice"]


def _id(workspace_id: str, user_id: str) -> str:
    return f"{workspace_id}:{user_id}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


def passages(words: list[str], per_source: int = PASSAGES_PER_SOURCE, size: int = PASSAGE_WORDS) -> list[str]:
    """Up to `per_source` passages of about `size` words, spread evenly through the whole recording. A source that is too short for
    a useful passage gives none."""
    if len(words) < MIN_PASSAGE_WORDS:
        return []
    count = min(per_source, max(1, len(words) // size))
    step = len(words) / count
    out = []
    for i in range(count):
        chunk = words[int(i * step): int(i * step) + size]
        if len(chunk) >= MIN_PASSAGE_WORDS:
            out.append(" ".join(chunk))
    return out


def _mean(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    n = len(vectors[0])
    total = [0.0] * n
    for v in vectors:
        if len(v) == n:
            for i, x in enumerate(v):
                total[i] += x
    norm = sum(x * x for x in total) ** 0.5 or 1.0
    return [x / norm for x in total]


async def status(workspace_id: str, user_id: str) -> dict[str, Any]:
    doc = await _store().find_one({"_id": _id(workspace_id, user_id)}) or {}
    ready = len(doc.get("embedding") or []) > 0 and int(doc.get("passages", 0)) >= MIN_PASSAGES_TO_USE
    last = _aware(doc.get("learned_at"))
    return {
        "enabled": bool(doc.get("enabled")), "ready": ready, "sources": len(doc.get("source_ids") or []), "passages": int(doc.get("passages", 0)),
        "learned_at": last.isoformat().replace("+00:00", "Z") if last else None,
        "next_run_ok": not last or _now() - last >= RUN_EVERY,
    }


async def set_enabled(workspace_id: str, user_id: str, enabled: bool) -> dict[str, Any]:
    await _store().update_one(
        {"_id": _id(workspace_id, user_id)},
        {"$set": {"enabled": bool(enabled), "workspace_id": workspace_id, "user_id": user_id, "updated_at": _now()},
         "$setOnInsert": {"embedding": [], "passages": 0, "source_ids": []}},
        upsert=True,
    )
    return await status(workspace_id, user_id)


async def forget(workspace_id: str, user_id: str) -> dict[str, Any]:
    """Everything learned is deleted, and the switch is left off."""
    await _store().delete_one({"_id": _id(workspace_id, user_id)})
    return await status(workspace_id, user_id)


async def _own_sources(workspace_id: str, user_id: str, skip: set[str]) -> list[tuple[str, list[str]]]:
    """(source id, words) for the member's own transcribed videos and recordings, newest first, new ones only."""
    out: list[tuple[str, list[str]]] = []
    mine = await media_assets.find(
        {"workspace_id": workspace_id, "created_by": user_id, "kind": "video"}, {"id": 1},
    ).sort("created_at", -1).to_list(length=60)
    from app.video import transcribe

    for m in mine:
        sid = f"video:{m['id']}"
        if sid in skip or len(out) >= MAX_SOURCES:
            continue
        doc = await get_db()["video_transcripts"].find_one({"workspace_id": workspace_id, "media_id": m["id"], "status": {"$in": ["done", "partial"]}})
        if doc:
            words = [str(w.get("w") or "") for w in await transcribe.all_words(doc["id"], limit=20_000)]
            if words:
                out.append((sid, words))
    async for a in audio_assets.find(
        {"workspace_id": workspace_id, "created_by": user_id, "deleted": {"$ne": True}, "transcript.0": {"$exists": True}},
        {"id": 1, "transcript": 1},
    ).sort("created_at", -1).limit(60):
        sid = f"audio:{a['id']}"
        if sid in skip or len(out) >= MAX_SOURCES:
            continue
        words = [str(w.get("word") or "") for w in a.get("transcript") or []]
        if words:
            out.append((sid, words))
    return out


async def learn(workspace_id: str, user_id: str, *, embed: Embed, now: Optional[datetime] = None) -> dict[str, Any]:
    """Learn from the member's own sources that have not been used yet. Refuses when switched off or run less than an hour ago."""
    when = now or _now()
    doc = await _store().find_one({"_id": _id(workspace_id, user_id)})
    if not doc or not doc.get("enabled"):
        return {"result": "off", **await status(workspace_id, user_id)}
    last = _aware(doc.get("learned_at"))
    if last and when - last < RUN_EVERY:
        return {"result": "too_soon", **await status(workspace_id, user_id)}
    done = set(doc.get("source_ids") or [])
    chunks: list[tuple[str, str]] = []
    for sid, words in await _own_sources(workspace_id, user_id, done):
        for p in passages(words):
            if len(chunks) < MAX_PASSAGES:
                chunks.append((sid, p))
    if not chunks:
        return {"result": "nothing_new", **await status(workspace_id, user_id)}
    vectors: list[list[float]] = []
    used: set[str] = set()
    for sid, text in chunks:
        vec = await embed(text)
        if vec:
            vectors.append(vec)
            used.add(sid)
    if not vectors:
        return {"result": "unavailable", **await status(workspace_id, user_id)}
    old = doc.get("embedding") or []
    old_n = int(doc.get("passages", 0))
    merged = _mean([[x * old_n for x in old]] + vectors) if old and old_n else _mean(vectors)
    fingerprint = style_mod.fingerprint(" ".join(t for _, t in chunks))
    await _store().update_one({"_id": doc["_id"]}, {"$set": {
        "embedding": merged, "passages": old_n + len(vectors), "source_ids": sorted(done | used), "learned_at": when,
        "style": {k: fingerprint.get(k) for k in ("avg_sentence_len", "avg_word_len", "question_rate", "emoji_rate")}, "updated_at": when,
    }})
    return {"result": "learned", **await status(workspace_id, user_id)}


async def compare(workspace_id: str, user_id: str, text: str, *, embed: Embed) -> dict[str, Any]:
    """How close `text` is to the voice learned from the member's recordings. status is off, learning (not enough yet) or ok."""
    from app.shared.llm import cosine_similarity

    doc = await _store().find_one({"_id": _id(workspace_id, user_id)})
    if not doc or not doc.get("enabled"):
        return {"status": "off"}
    if len(doc.get("embedding") or []) == 0 or int(doc.get("passages", 0)) < MIN_PASSAGES_TO_USE:
        return {"status": "learning"}
    vec = await embed(text)
    if not vec:
        return {"status": "unavailable"}
    sim = cosine_similarity(vec, doc["embedding"])
    return {"status": "ok", "similarity": round(sim, 4), "in_voice": sim >= T.SIM_SOFT_FLOOR}
