"""Translating a video's spoken lines so they can be said in the time the original had.

One prompt, run in small batches. Each answer must hold exactly the lines asked for, keep the brand's glossary terms and every
figure, and not be cut short or padded. Lines that still do not fit their seconds are sent back once with the exact lines to
shorten; whatever still does not fit is left as it is and flagged by the planner, never silently trimmed.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from app.video.create import generate as G
from app.video.create import validate as V
from app.video.narrate import plan as P

BATCH = 20


def validate_dub(raw: Any, count: int, sources: list[str], glossary: list[str]) -> tuple[dict[int, str], list[str]]:
    """{line number: translated text} for the lines that pass, and what was wrong with the rest. Numbers are 1 to `count`."""
    items = (raw.get("lines") if isinstance(raw, dict) else None) or []
    out: dict[int, str] = {}
    problems: list[str] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        n = it.get("i")
        if not isinstance(n, int) or isinstance(n, bool) or not (1 <= n <= count):
            problems.append(f"Line {n} is not one of the lines.")
            continue
        text = V.clean(it.get("text"), 900)
        if not text:
            problems.append(f"Line {n} is empty.")
            continue
        src = sources[n - 1]
        ratio = len(V.words_of(text)) / max(1, len(V.words_of(src)))
        if not (0.25 <= ratio <= 3.5):
            problems.append(f"Line {n} is {ratio:.1f} times the length of the original.")
            continue
        missing = [g for g in glossary if g and g.casefold() in src.casefold() and g.casefold() not in text.casefold()]
        if missing:
            problems.append(f"Line {n} must keep the term {', '.join(missing)} exactly as written.")
            continue
        if V.numbers_in(src) - V.numbers_in(text):
            problems.append(f"Line {n} drops a figure from the original.")
            continue
        out[n] = text
    for n in range(1, count + 1):
        if n not in out and not any(f"Line {n} " in p for p in problems):
            problems.append(f"Line {n} is missing.")
    return out, problems


async def translate(
    segments: list[dict[str, Any]], *, target_name: str, target_code: str, register_note: str = "", glossary: Optional[list[str]] = None,
    caller: Optional[G.Caller] = None, tighten: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Translated lines in the segments' order, each `{"i", "text", "fit"}`; and a report. A line that could not be translated
    keeps `text` as None and is listed in `failed`."""
    glossary = [g for g in (glossary or []) if g][:60]
    wps = P.words_per_second(target_code)
    lines: dict[int, Optional[str]] = {s["i"]: None for s in segments}
    problems: list[str] = []
    calls = 0

    async def pass_over(chunk: list[dict[str, Any]], shorten: list[int]) -> None:
        nonlocal calls
        listing = "\n".join(f"{n + 1} ({s['slot_s']:.1f} s) {s['text']}" for n, s in enumerate(chunk))
        sources = [s["text"] for s in chunk]
        got, meta = await G.run(
            "dub_translate",
            {"lines": listing, "count": len(chunk), "target_language": target_name, "register_note": register_note[:300], "glossary": glossary,
             "words_per_second": wps, "example_words": max(2, int(4 * wps)), "shorten": shorten},
            lambda raw: validate_dub(raw, len(chunk), sources, glossary), language=target_code,
            text_of=lambda c: " ".join(c.values()), caller=caller,
        )
        calls += meta["tries"]
        for n, text in got.items():
            lines[chunk[n - 1]["i"]] = text
        problems.extend(meta["problems"])

    for k in range(0, len(segments), BATCH):
        await pass_over(segments[k:k + BATCH], [])

    by_i = {s["i"]: s for s in segments}
    tightened: set[int] = set()
    if tighten:
        too_long = [i for i, t in lines.items() if t and P.fit_status(t, by_i[i]["slot_s"], target_code)["status"] == "too_long"]
        if too_long:
            for k in range(0, len(too_long), BATCH):
                ids = too_long[k:k + BATCH]
                chunk = [by_i[i] for i in ids]
                before = {i: lines[i] for i in ids}
                await pass_over(chunk, list(range(1, len(chunk) + 1)))
                for i in ids:
                    new = lines[i]
                    if new and before[i] and len(new) < len(before[i]):
                        tightened.add(i)
                    else:
                        lines[i] = before[i]
    out = []
    for s in segments:
        text = lines[s["i"]]
        out.append({"i": s["i"], "text": text, "fit": P.fit_status(text, s["slot_s"], target_code) if text else None,
                    "tightened": s["i"] in tightened})
    failed = [x["i"] for x in out if not x["text"]]
    return out, {"calls": calls, "problems": problems[:12], "failed": failed, "tightened": sorted(tightened)}


def roundtrip_similarity(script: str, heard: str) -> float:
    """How closely what a listener (or a transcription) hears matches the script the voice was given: the share of the script's
    words found in the same order in what was heard, 0 to 1."""
    def norm(t: str) -> list[str]:
        return [w.casefold() for w in re.findall(r"[\w']+", t, re.UNICODE)]

    a, b = norm(script), norm(heard)
    if not a:
        return 1.0
    # longest common subsequence length
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0]
        for j, y in enumerate(b, 1):
            cur.append(prev[j - 1] + 1 if x == y else max(prev[j], cur[j - 1]))
        prev = cur
    return round(prev[-1] / len(a), 3)
