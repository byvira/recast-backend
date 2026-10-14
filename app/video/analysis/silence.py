"""The silence and filler cut list: where a talk can be tightened. Always a preview; nothing is cut until the member applies it.

Pauses come from the gaps between words in the real transcript, so they need no extra audio work. Fillers are found by word lists
per language; words that are sometimes filler and sometimes meaning ("like", "so") go to a judge that reads the sentence around them.
"""
from __future__ import annotations

import re
from typing import Any, Awaitable, Callable, Optional

#: Always a filler when they stand alone.
CERTAIN: dict[str, set[str]] = {
    "en": {"um", "uh", "umm", "uhh", "erm", "er", "uhm", "hmm", "mm", "mmm", "ah"},
    "hi": {"उम", "अह", "हम", "अरे"},
    "ta": {"ஆம்", "இம்", "எம்"},
}
#: Sometimes filler, sometimes meaning: a judge decides from the sentence.
AMBIGUOUS: dict[str, set[str]] = {
    "en": {"like", "so", "actually", "basically", "right", "well", "literally"},
    "hi": {"मतलब", "यानी", "तो"},
    "ta": {"அதாவது", "சரி"},
}
PHRASES: dict[str, list[tuple[str, ...]]] = {"en": [("you", "know"), ("i", "mean"), ("sort", "of"), ("kind", "of")]}

_TOKEN = re.compile(r"[^\w']+", re.UNICODE)
KEEP_MS = 250                    # breathing room left on each side of a cut pause

Judge = Callable[[list[dict[str, Any]]], Awaitable[dict[int, bool]]]


def _base(lang: Optional[str]) -> str:
    return (lang or "en").lower().split("-")[0][:2]


def _key(word: str) -> str:
    return _TOKEN.sub("", word).casefold()


def pauses(words: list[dict[str, Any]], *, pause_ms: int = 600, keep_ms: int = KEEP_MS) -> list[dict[str, Any]]:
    """Gaps between words long enough to tighten. `start_ms` and `end_ms` are what would be removed; `keep_ms` is left on each side."""
    out: list[dict[str, Any]] = []
    for a, b in zip(words, words[1:]):
        gap = b["s_ms"] - a["e_ms"]
        if gap >= pause_ms and gap - 2 * keep_ms > 80:
            out.append({
                "kind": "pause", "start_ms": a["e_ms"] + keep_ms, "end_ms": b["s_ms"] - keep_ms, "gap_ms": gap,
                "reason": f"A pause of {gap / 1000:.1f} seconds.", "confidence": 0.9 if gap >= 1000 else 0.7,
            })
    return out


def filler_candidates(words: list[dict[str, Any]], language: Optional[str]) -> list[dict[str, Any]]:
    lang = _base(language)
    certain, ambiguous, phrases = CERTAIN.get(lang, set()), AMBIGUOUS.get(lang, set()), PHRASES.get(lang, [])
    keys = [_key(w["w"]) for w in words]
    out: list[dict[str, Any]] = []
    i = 0
    while i < len(words):
        hit = None
        for phrase in phrases:
            n = len(phrase)
            if tuple(keys[i:i + n]) == phrase:
                hit = (i, i + n - 1, "phrase")
                break
        if hit is None and keys[i] in certain:
            hit = (i, i, "certain")
        elif hit is None and keys[i] in ambiguous:
            hit = (i, i, "ambiguous")
        if hit:
            a, b, kind = hit
            out.append({"first": a, "last": b, "kind": kind, "word": " ".join(w["w"] for w in words[a:b + 1])})
            i = b + 1
        else:
            i += 1
    return out


async def fillers(
    words: list[dict[str, Any]], language: Optional[str], *, judge: Optional[Judge] = None,
) -> list[dict[str, Any]]:
    """Fillers to remove. Certain ones are always listed; ambiguous ones only when the judge says the sentence is better
    without them, and with no judge they are left alone (never guessed)."""
    cands = filler_candidates(words, language)
    ambiguous = [(n, c) for n, c in enumerate(cands) if c["kind"] == "ambiguous"]
    verdicts: dict[int, bool] = {}
    if ambiguous and judge is not None:
        items = []
        for n, c in ambiguous:
            lo, hi = max(0, c["first"] - 6), min(len(words), c["last"] + 7)
            items.append({"id": n, "word": c["word"], "context": " ".join(w["w"] for w in words[lo:hi])})
        try:
            verdicts = await judge(items)
        except Exception:  # noqa: BLE001 - an unsure judge removes nothing
            verdicts = {}
    out: list[dict[str, Any]] = []
    for n, c in enumerate(cands):
        if c["kind"] == "ambiguous" and not verdicts.get(n):
            continue
        a, b = words[c["first"]], words[c["last"]]
        out.append({
            "kind": "filler", "start_ms": a["s_ms"], "end_ms": b["e_ms"], "word": c["word"],
            "reason": f"“{c['word']}” is a filler here.", "confidence": 0.95 if c["kind"] != "ambiguous" else 0.7,
        })
    return out


def cut_list(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pauses and fillers as one ordered list with overlaps merged, and how much time it would save."""
    ordered = sorted(items, key=lambda x: (x["start_ms"], x["end_ms"]))
    merged: list[dict[str, Any]] = []
    for it in ordered:
        if merged and it["start_ms"] <= merged[-1]["end_ms"]:
            top = merged[-1]
            top["end_ms"] = max(top["end_ms"], it["end_ms"])
            top["reason"] = f"{top['reason']} {it['reason']}"
            top["kind"] = "mixed" if top["kind"] != it["kind"] else top["kind"]
            continue
        merged.append(dict(it))
    for n, m in enumerate(merged):
        m["id"] = f"cut{n + 1}"
    return merged


def saved_ms(cuts: list[dict[str, Any]]) -> int:
    return sum(max(0, c["end_ms"] - c["start_ms"]) for c in cuts)


def silences_from_peaks(peaks: list[int], per_second: int, *, threshold: int = 6, min_ms: int = 700) -> list[dict[str, Any]]:
    """Quiet stretches read from the waveform peaks (0 to 255): runs below `threshold` lasting at least `min_ms`. Catches silences
    the speech service left no words around (music, a long break)."""
    if not peaks or per_second <= 0:
        return []
    out: list[dict[str, Any]] = []
    run_start: Optional[int] = None
    for i, p in enumerate(peaks + [255]):
        quiet = p <= threshold
        if quiet and run_start is None:
            run_start = i
        elif not quiet and run_start is not None:
            length_ms = (i - run_start) * 1000 // per_second
            if length_ms >= min_ms:
                out.append({"kind": "silence", "start_ms": run_start * 1000 // per_second, "end_ms": i * 1000 // per_second,
                            "reason": f"{length_ms / 1000:.1f} seconds of silence.", "confidence": 0.9})
            run_start = None
    return out


def shots(scenes: list[dict[str, Any]], duration_s: float) -> list[dict[str, Any]]:
    """The shot log: consecutive shots between the detected cuts, in time order, with their length."""
    cuts = sorted({round(float(s["t"]), 2) for s in scenes if 0 < float(s["t"]) < duration_s})
    edges = [0.0] + cuts + [round(duration_s, 2)]
    out = []
    for n, (a, b) in enumerate(zip(edges, edges[1:])):
        if b - a < 0.2:
            continue
        out.append({"index": len(out) + 1, "start_s": a, "end_s": b, "duration_s": round(b - a, 2)})
    return out
