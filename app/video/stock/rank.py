"""Choosing stock clips for a line of the video, and finding the lines worth footage.

Ranking is plain and repeatable: how many of the line's key words the clip's tags and query share, whether it runs long enough for
the beat, whether its shape suits the video, and its quality. An optional meaning-based re-rank may reorder the best few; if it is
unavailable the lexical order stands. The keyword rail picks the sentences of a transcript that are concrete enough to show.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any, Awaitable, Callable, Optional

_WORD = re.compile(r"[A-Za-z][A-Za-z'-]{2,}")
STOP = frozenset(
    "the and for that this with have from your you are was were will would could should about into over under more most very just "
    "than then them they their there here what when where which while who whom why how also been being does did doing not but can "
    "its it's our out one two get got let like make made many much some such only other another each every any all too own same "
    "because through during before after again further once these those both few off above below between against until".split()
)


def keywords(text: str, limit: int = 8) -> list[str]:
    seen: list[str] = []
    for w in _WORD.findall(text):
        k = w.lower()
        if k in STOP or k in seen:
            continue
        seen.append(k)
    return seen[:limit]


def orientation_of(width: int, height: int) -> str:
    if not width or not height:
        return "unknown"
    r = width / height
    return "tall" if r < 0.85 else "wide" if r > 1.2 else "square"


def score_clip(clip: dict[str, Any], line: str, *, want: str = "wide", min_seconds: float = 3.0) -> dict[str, Any]:
    words = set(keywords(line, 12))
    have = {t.lower() for tag in clip.get("tags") or [] for t in _WORD.findall(tag)} | {w.lower() for w in _WORD.findall(clip.get("_query", ""))}
    overlap = len(words & have)
    reasons: list[str] = []
    score = 0.0
    if words:
        share = overlap / min(len(words), 4)
        score += 0.6 * min(1.0, share)
        if overlap:
            reasons.append(f"Matches {', '.join(sorted(words & have)[:3])}")
    dur = float(clip.get("duration_s") or 0)
    if dur >= min_seconds:
        score += 0.10
    else:
        reasons.append("Shorter than the beat")
    shape = orientation_of(int(clip.get("width") or 0), int(clip.get("height") or 0))
    if shape == want:
        score += 0.15
        reasons.append("Fits the video's shape")
    px = int(clip.get("width") or 0) * int(clip.get("height") or 0)
    score += 0.15 * min(1.0, px / (1280 * 720))
    return {"score": round(score, 3), "reasons": reasons[:3], "shape": shape}


def rank(clips: list[dict[str, Any]], line: str, *, want: str = "wide", min_seconds: float = 3.0, query: str = "") -> list[dict[str, Any]]:
    out = []
    for c in clips:
        info = score_clip({**c, "_query": query}, line, want=want, min_seconds=min_seconds)
        out.append({**c, **info})
    out.sort(key=lambda c: (-c["score"], -c.get("duration_s", 0), c["id"]))
    seen: set[tuple[str, str]] = set()
    unique = []
    for c in out:
        k = (c["provider"], c["id"])
        if k not in seen:
            seen.add(k)
            unique.append(c)
    return unique


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


async def rerank_by_meaning(
    ranked: list[dict[str, Any]], line: str, embed: Callable[[str], Awaitable[list[float]]], *, top: int = 6,
) -> tuple[list[dict[str, Any]], bool]:
    """Reorders the best few by how close their tags are in meaning to the line. Returns (list, used). Any failure leaves the
    lexical order as it was."""
    head, tail = ranked[:top], ranked[top:]
    try:
        qv = await embed(line)
        if not qv:
            return ranked, False
        scored = []
        for c in head:
            text = " ".join(c.get("tags") or []) or c.get("_query", "") or c.get("page_url", "")
            v = await embed(text) if text else []
            scored.append((0.6 * cosine(qv, v) + 0.4 * c["score"], c))
        scored.sort(key=lambda x: -x[0])
        return [c for _, c in scored] + tail, True
    except Exception:  # noqa: BLE001 - ranking by meaning is a bonus, never a blocker
        return ranked, False


def rail(words: list[dict[str, Any]], *, spacing_s: float = 8.0, limit: int = 12, min_words: int = 6) -> list[dict[str, Any]]:
    """Sentences of the transcript worth showing footage for: concrete (rare words, not filler), spaced out, with a search
    query made of their most telling words. Pure and free: no model is used."""
    from app.video.analysis import windows as W

    sents = [s for s in W.sentences(words) if len(s["text"].split()) >= min_words]
    if not sents:
        return []
    df: Counter[str] = Counter()
    per = []
    for s in sents:
        ks = set(keywords(s["text"], 20))
        per.append(ks)
        df.update(ks)
    n = len(sents)
    scored = []
    for s, ks in zip(sents, per):
        if not ks:
            continue
        score = sum(math.log(1 + n / df[k]) for k in ks) / math.sqrt(len(ks))
        scored.append((score, s, ks))
    scored.sort(key=lambda x: -x[0])
    chosen: list[tuple[float, dict[str, Any], set[str]]] = []
    for sc, s, ks in scored:
        if all(abs(s["start_ms"] - c[1]["start_ms"]) / 1000 >= spacing_s for c in chosen):
            chosen.append((sc, s, ks))
        if len(chosen) >= limit:
            break
    chosen.sort(key=lambda x: x[1]["start_ms"])
    out = []
    for sc, s, ks in chosen:
        ordered = sorted(ks, key=lambda k: (df[k], -len(k)))[:3]
        out.append({"index": len(out), "start_ms": s["start_ms"], "end_ms": s["end_ms"], "line": s["text"], "query": " ".join(ordered),
                    "keywords": ordered, "seconds": round((s["end_ms"] - s["start_ms"]) / 1000, 1)})
    return out
