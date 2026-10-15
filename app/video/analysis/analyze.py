"""Reading a whole transcript: each window is read once (a cached, paced model call), the readings are joined into an outline and
chapters, and moments worth clipping are found and checked.

What keeps this honest: the model only ever answers with sentence numbers. Every start time, end time and quote shown to a member
is read back from the real transcript, an answer that points at a sentence that does not exist is dropped, and a second look at
the same video with the same prompts costs no model call.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional
from uuid import uuid4

from app.core.config import settings
from app.db.mongo import get_db
from app.prompts.registry import load_prompt, template_version
from app.video.analysis import windows as W

logger = logging.getLogger(__name__)

MODEL_TAG = "balanced"
ALLOWED_RISK = {"needs_context", "claims_numbers", "names_people", "sensitive_topic", "cut_off_ending"}
MAX_PARTS_FULL = 24          # beyond this many parts the outline reads shorter summaries
Caller = Callable[[str], Awaitable[dict[str, Any]]]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clean(text: Any, limit: int) -> str:
    out = " ".join(str(text or "").replace("—", ", ").replace("–", "-").split())
    return out[:limit].strip()


# ── pacing and caching ───────────────────────────────────────────────────────────────────────────────────────────────────

class Pacer:
    """Keeps the model calls under the free per-minute ceiling by waiting just long enough, using the prompt's size."""

    def __init__(self, tokens_per_minute: Optional[int] = None):
        self.limit = int(tokens_per_minute or settings.GROQ_TPM_LIMIT * 0.75)
        self._seen: deque[tuple[float, int]] = deque()

    async def wait(self, tokens: int) -> None:
        tokens = min(tokens, self.limit)
        while True:
            now = time.monotonic()
            while self._seen and now - self._seen[0][0] > 60:
                self._seen.popleft()
            if sum(t for _, t in self._seen) + tokens <= self.limit:
                self._seen.append((now, tokens))
                return
            await asyncio.sleep(max(0.5, 60 - (now - self._seen[0][0]) + 0.2))


async def _default_caller(prompt: str) -> dict[str, Any]:
    from app.shared.llm import call_llm_structured, member_footage

    async with member_footage():
        return await call_llm_structured(prompt, max_tokens=1500)


async def cached_call(name: str, variables: dict[str, Any], *, caller: Caller, pacer: Optional[Pacer] = None) -> tuple[dict[str, Any], bool]:
    """(answer, from_cache). Keyed by the exact prompt text, the template's version and the model, so changing a prompt or the
    model never serves an old answer, and nothing is read twice."""
    prompt = load_prompt(name, **variables)
    key = hashlib.sha256(f"{template_version(name)}|{MODEL_TAG}|{prompt}".encode("utf-8")).hexdigest()
    col = get_db()["video_analysis_cache"]
    row = await col.find_one({"_id": key})
    if row and isinstance(row.get("value"), dict):
        return row["value"], True
    if pacer is not None:
        await pacer.wait(W.estimate_tokens(prompt) + 800)
    answer = await caller(prompt)
    if isinstance(answer, dict) and answer:
        await col.update_one({"_id": key}, {"$set": {"value": answer, "name": name, "at": _now()}}, upsert=True)
    return (answer if isinstance(answer, dict) else {}), False


# ── reading one window ───────────────────────────────────────────────────────────────────────────────────────────────────

def validate_reading(raw: dict[str, Any], win: dict[str, Any]) -> dict[str, Any]:
    topics = [t for t in (_clean(x, 40) for x in (raw.get("topics") or [])[:5]) if t]
    points = []
    for p in (raw.get("points") or [])[:5]:
        try:
            n = int(p.get("sentence"))
        except (TypeError, ValueError, AttributeError):
            continue
        text = _clean(p.get("point"), 200)
        if win["from"] <= n <= win["to"] and text:
            points.append({"sentence": n, "point": text})
    return {"summary": _clean(raw.get("summary"), 700), "topics": topics, "points": points}


async def read_windows(
    sents: list[dict[str, Any]], wins: list[dict[str, Any]], language: str, *, caller: Caller, pacer: Pacer,
    on_step: Optional[Callable[[int, int], Awaitable[None]]] = None,
) -> tuple[list[dict[str, Any]], int]:
    """(readings, how many came from the cache). One per window, in order."""
    out: list[dict[str, Any]] = []
    hits = 0
    for n, win in enumerate(wins):
        raw, hit = await cached_call(
            "video/understand_window",
            {"language": language, "part": n + 1, "parts": len(wins), "lines": W.window_text(sents, win)},
            caller=caller, pacer=pacer,
        )
        hits += 1 if hit else 0
        out.append({**validate_reading(raw, win), "window": win["index"]})
        if on_step:
            await on_step(n + 1, len(wins))
    return out, hits


# ── the outline and chapters ─────────────────────────────────────────────────────────────────────────────────────────────

def _own_start(sents: list[dict[str, Any]], win: dict[str, Any]) -> int:
    """Where a window's own sentences begin: the shared sentences at its start belong to the window before."""
    skip = W.SHARED_SENTENCES if win["index"] > 0 and (win["to"] - win["from"]) > W.SHARED_SENTENCES else 0
    return sents[min(win["from"] + skip, win["to"])]["start_ms"]


def _label(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def validate_chapters(items: Any, starts_by_part: dict[int, int], duration_ms: int) -> list[dict[str, Any]]:
    """Chapters whose times come from the parts they start in. First at 0:00, strictly increasing, at least 10 seconds long."""
    chapters: list[dict[str, Any]] = []
    for item in items or []:
        try:
            part = int(item.get("part"))
        except (TypeError, ValueError, AttributeError):
            continue
        title = _clean(item.get("title"), 80).strip("\"'")
        if part in starts_by_part and title:
            chapters.append({"title": title, "start_ms": starts_by_part[part], "part": part})
    chapters.sort(key=lambda c: c["start_ms"])
    kept: list[dict[str, Any]] = []
    for c in chapters:
        if kept and c["start_ms"] - kept[-1]["start_ms"] < 10_000:
            continue
        if c["start_ms"] >= duration_ms:
            continue
        kept.append(c)
    if len(kept) < 2:
        return []
    kept[0]["start_ms"] = 0
    for i, c in enumerate(kept):
        c["id"] = f"ch{i + 1}"
    return kept


async def build_outline(
    sents: list[dict[str, Any]], wins: list[dict[str, Any]], readings: list[dict[str, Any]], language: str, duration_s: float,
    *, caller: Caller, pacer: Pacer,
) -> dict[str, Any]:
    if not readings:
        return {"title": "", "summary": "", "topics": [], "chapters": []}
    short = len(readings) > MAX_PARTS_FULL
    lines = []
    for r in readings:
        summary = r["summary"] if not short else " ".join(r["summary"].split()[:40])
        lines.append(f"Part {r['window'] + 1}: {summary}" + (f" (topics: {', '.join(r['topics'])})" if r["topics"] and not short else ""))
    minutes = duration_s / 60
    most = max(3, min(14, int(minutes // 4) + 2))
    raw, _ = await cached_call(
        "video/outline",
        {"duration_label": _label(duration_s), "language": language, "min_chapters": 2 if minutes < 6 else 3, "max_chapters": most,
         "parts_text": "\n".join(lines)},
        caller=caller, pacer=pacer,
    )
    starts = {w["index"] + 1: _own_start(sents, w) for w in wins}
    return {
        "title": _clean(raw.get("title"), 120), "summary": _clean(raw.get("summary"), 1500),
        "topics": [t for t in (_clean(x, 40) for x in (raw.get("topics") or [])[:8]) if t],
        "chapters": validate_chapters(raw.get("chapters"), starts, int(duration_s * 1000)),
    }


# ── moments ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

def validate_moments(
    raw: dict[str, Any], win: dict[str, Any], sents: list[dict[str, Any]], *, min_s: float, max_s: float,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in (raw.get("moments") or [])[:12]:
        try:
            a, b = int(m.get("start")), int(m.get("end"))
        except (TypeError, ValueError, AttributeError):
            continue
        if not (win["from"] <= a <= b <= win["to"]):
            continue
        start_ms, end_ms = sents[a]["start_ms"], sents[b]["end_ms"]
        length = (end_ms - start_ms) / 1000.0
        if length < min_s * 0.8 or length > max_s * 1.25:
            continue
        title, reason = _clean(m.get("title"), 80), _clean(m.get("reason"), 220)
        if not title or not reason:
            continue

        def score(v: Any) -> float:
            try:
                return max(0.0, min(1.0, float(v)))
            except (TypeError, ValueError):
                return 0.0

        text = " ".join(s["text"] for s in sents[a:b + 1])
        risk = [r for r in (m.get("risk") or []) if r in ALLOWED_RISK]
        try:
            from app.agents.content_guard.agent import rule_screen

            if not rule_screen(text).ok and "sensitive_topic" not in risk:
                risk.append("sensitive_topic")
        except Exception:  # noqa: BLE001 - the screen is an extra, never a reason to lose a moment
            pass
        if not text.rstrip().endswith((".", "!", "?", "।", "…")) and "cut_off_ending" not in risk:
            risk.append("cut_off_ending")
        hook, stand = score(m.get("hook")), score(m.get("standalone"))
        out.append({
            "sentence_start": a, "sentence_end": b, "start_ms": start_ms, "end_ms": end_ms, "title": title, "reason": reason,
            "hook": round(hook, 3), "standalone": round(stand, 3), "score": round(0.6 * hook + 0.4 * stand, 3), "risk": risk,
            "quote": _clean(text, 240),
        })
    return out


def rank_moments(candidates: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    """Best first, with no two moments sharing time (the better one wins), and ids and a starting state for each."""
    picked: list[dict[str, Any]] = []
    for c in sorted(candidates, key=lambda x: x["score"], reverse=True):
        if any(c["start_ms"] < p["end_ms"] and c["end_ms"] > p["start_ms"] for p in picked):
            continue
        picked.append(c)
        if len(picked) >= count:
            break
    picked.sort(key=lambda x: x["start_ms"])
    return [{**c, "id": f"m{i + 1}", "state": "suggested"} for i, c in enumerate(picked)]


async def find_moments(
    sents: list[dict[str, Any]], wins: list[dict[str, Any]], language: str, *, min_s: float = 20.0, max_s: float = 60.0,
    count: int = 8, platform: Optional[str] = None, caller: Caller, pacer: Pacer,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for win in wins:
        raw, _ = await cached_call(
            "video/moments",
            {"language": language, "min_seconds": int(min_s), "max_seconds": int(max_s), "platform": platform or "", "count": min(6, count),
             "lines": W.window_text(sents, win)},
            caller=caller, pacer=pacer,
        )
        candidates.extend(validate_moments(raw, win, sents, min_s=min_s, max_s=max_s))
    return rank_moments(candidates, count)


# ── one call for a whole video ───────────────────────────────────────────────────────────────────────────────────────────

async def analyze(
    words: list[dict[str, Any]], *, language: str = "en", duration_s: float, parts: tuple[str, ...] = ("outline", "moments"),
    min_s: float = 20.0, max_s: float = 60.0, count: int = 8, platform: Optional[str] = None,
    caller: Optional[Caller] = None, reporter: Any = None,
) -> dict[str, Any]:
    caller = caller or _default_caller
    pacer = Pacer()
    sents = W.sentences(words)
    if len(sents) < 3:
        return {"id": str(uuid4()), "sentences": len(sents), "windows": 0, "outline": None, "chapters": [], "moments": [],
                "note": "There is too little speech in this video to find chapters or moments."}
    wins = W.windows(sents)

    async def step(done: int, total: int) -> None:
        if reporter is not None:
            await reporter.step(f"Reading the video ({done} of {total})")

    readings, hits = await read_windows(sents, wins, language, caller=caller, pacer=pacer, on_step=step)
    result: dict[str, Any] = {"sentences": len(sents), "windows": len(wins), "cache_hits": hits, "outline": None, "chapters": [], "moments": [],
                              "note": None}
    if "outline" in parts:
        outline = await build_outline(sents, wins, readings, language, duration_s, caller=caller, pacer=pacer)
        result["chapters"] = outline.pop("chapters")
        result["outline"] = outline
    if "moments" in parts:
        if reporter is not None:
            await reporter.step("Finding moments worth clipping")
        result["moments"] = await find_moments(sents, wins, language, min_s=min_s, max_s=max_s, count=count, platform=platform,
                                               caller=caller, pacer=pacer)
    return result
