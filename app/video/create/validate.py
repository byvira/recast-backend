"""What a model's answer must satisfy before it is used: one validator per creative prompt.

Every validator takes the raw answer and the facts it must agree with (the clip's own words, the real chapter list, the real
duration) and returns `(clean, problems)`. `clean` holds only the items that passed, `problems` says in plain words what failed, so
a repair prompt can quote the exact reason. Nothing here calls a model, and nothing is guessed: a time, a figure or a name that is
not in the source is a problem, never accepted.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Iterable, Optional

_SPACE = re.compile(r"\s+")
_WORD = re.compile(r"[\w']+", re.UNICODE)
_NUMBER = re.compile(r"\d[\d,.]*")
_STAMP = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")

#: Words per minute a narrator speaks at, by language family. A line is judged by words, never by the model's own estimate.
WPM = {"en": 150, "default": 140, "ta": 120, "hi": 135}

MOTIONS = ("none", "slow_zoom_in", "slow_zoom_out", "pan_left", "pan_right")
TRANSITIONS = ("cut", "fade", "dissolve")
EDIT_KINDS = ("cut", "tighten_pause", "zoom", "emphasis")


def clean(text: Any, limit: int = 400) -> str:
    return _SPACE.sub(" ", str(text or "")).strip()[:limit].strip()


def words_of(text: str) -> list[str]:
    return _WORD.findall(text or "")


def _content_words(text: str) -> set[str]:
    return {w.casefold() for w in words_of(text) if len(w) >= 4}


def numbers_in(text: str) -> set[str]:
    return {n.strip(".,") for n in _NUMBER.findall(text or "") if n.strip(".,")}


def spoken_seconds(text: str, language: str = "en") -> float:
    base = (language or "en").split("+")[0].split("-")[0].lower()
    wpm = WPM.get(base, WPM["default"])
    return round(len(words_of(text)) / wpm * 60.0, 1)


def _banned_hit(text: str, banned: Iterable[str]) -> Optional[str]:
    low = text.casefold()
    for b in banned:
        b = (b or "").strip().casefold()
        if b and re.search(rf"(?<!\w){re.escape(b)}(?!\w)", low):
            return b
    return None


# ── 4 hook_writer ────────────────────────────────────────────────────────────────────────────────────────────────────────

def validate_hooks(raw: Any, clip_text: str, *, banned: Iterable[str] = (), max_words: int = 14) -> tuple[list[dict[str, Any]], list[str]]:
    """Opening lines for the first three seconds. Short, free of banned words, and grounded in the clip: a figure must be in the
    clip, and the line must share a content word with it."""
    items = (raw.get("hooks") if isinstance(raw, dict) else None) or []
    base = _content_words(clip_text)
    figures = numbers_in(clip_text)
    out: list[dict[str, Any]] = []
    problems: list[str] = []
    seen: set[str] = set()
    for item in items[:6]:
        text = clean(item.get("text") if isinstance(item, dict) else item, 160)
        if not text:
            continue
        n = len(words_of(text))
        if n > max_words:
            problems.append(f"“{text[:40]}…” is {n} words; an opening line must be {max_words} words or fewer.")
            continue
        hit = _banned_hit(text, banned)
        if hit:
            problems.append(f"“{text[:40]}…” uses the banned word “{hit}”.")
            continue
        extra = numbers_in(text) - figures
        if extra:
            problems.append(f"“{text[:40]}…” states {', '.join(sorted(extra))}, which is not in the clip.")
            continue
        if base and not (_content_words(text) & base):
            problems.append(f"“{text[:40]}…” says nothing that is in the clip.")
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append({"text": text, "words": n, "seconds": spoken_seconds(text)})
    return out[:3], problems


# ── 5 script_from_text ───────────────────────────────────────────────────────────────────────────────────────────────────

def validate_script(
    raw: Any, source_text: str, *, target_seconds: Optional[float], language: str = "en", tolerance: float = 0.15,
) -> tuple[dict[str, Any], list[str]]:
    """Beats of narration with the on-screen text and a visual idea. Each beat's seconds are worked out here from its words, the
    total must be near the target when one is asked for, and a figure not in the source is refused."""
    beats_in = (raw.get("beats") if isinstance(raw, dict) else None) or []
    figures = numbers_in(source_text)
    beats: list[dict[str, Any]] = []
    problems: list[str] = []
    for i, b in enumerate(beats_in[:40]):
        if not isinstance(b, dict):
            continue
        narration = clean(b.get("narration"), 1200)
        if not narration:
            continue
        extra = numbers_in(narration) - figures
        if extra:
            problems.append(f"Beat {i + 1} states {', '.join(sorted(extra))}, which is not in the source.")
            continue
        beats.append({"narration": narration, "on_screen": clean(b.get("on_screen"), 80), "visual": clean(b.get("visual"), 200),
                      "seconds": max(1.0, spoken_seconds(narration, language))})
    total = round(sum(b["seconds"] for b in beats), 1)
    if not beats:
        problems.append("The script has no beats.")
    if target_seconds and beats:
        low, high = target_seconds * (1 - tolerance), target_seconds * (1 + tolerance)
        if not (low <= total <= high):
            problems.append(f"The script runs about {total:.0f} seconds when read aloud; it must be between {low:.0f} and {high:.0f}.")
    return {"beats": beats, "total_seconds": total, "target_seconds": target_seconds}, problems


# ── 6 script_from_images ─────────────────────────────────────────────────────────────────────────────────────────────────

def validate_picture_script(raw: Any, slide_ids: list[str], slide_text: dict[str, str]) -> tuple[list[dict[str, Any]], list[str]]:
    """One entry per picture, each used once, with motion and transition from fixed lists. Text shown must come from the picture's
    own words."""
    items = (raw.get("slides") if isinstance(raw, dict) else None) or []
    seen: list[str] = []
    out: list[dict[str, Any]] = []
    problems: list[str] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        sid = str(it.get("id", ""))
        if sid not in slide_ids:
            problems.append(f"“{sid}” is not one of the pictures.")
            continue
        if sid in seen:
            problems.append(f"Picture {sid} is used twice.")
            continue
        seen.append(sid)
        on_screen = clean(it.get("on_screen"), 100)
        source = slide_text.get(sid, "")
        if on_screen and source and not (_content_words(on_screen) <= _content_words(source)):
            on_screen = ""                                                       # words that are not the picture's own are dropped
        motion = it.get("motion") if it.get("motion") in MOTIONS else "slow_zoom_in"
        transition = it.get("transition") if it.get("transition") in TRANSITIONS else "cut"
        out.append({"id": sid, "narration": clean(it.get("narration"), 400), "on_screen": on_screen, "motion": motion, "transition": transition})
    for sid in slide_ids:
        if sid not in seen:
            problems.append(f"Picture {sid} is not used.")
    return out, problems


# ── 7 broll_queries ─────────────────────────────────────────────────────────────────────────────────────────────────────

def validate_broll(raw: Any, *, brand_names: Iterable[str] = ()) -> tuple[dict[str, Any], list[str]]:
    items = (raw.get("queries") if isinstance(raw, dict) else None) or []
    names = [n.casefold() for n in brand_names if n]
    queries: list[str] = []
    problems: list[str] = []
    for q in items[:6]:
        text = clean(q, 80).lower()
        if not text:
            continue
        if len(words_of(text)) > 6:
            problems.append(f"“{text}” is longer than six words.")
            continue
        if any(n in text for n in names):
            problems.append(f"“{text}” names the brand.")
            continue
        if re.search(r"\b[A-Z][a-z]+ [A-Z][a-z]+\b", clean(q, 80)):
            problems.append(f"“{text}” names a person.")
            continue
        if text not in queries:
            queries.append(text)
    avoid = [clean(a, 30).lower() for a in (raw.get("avoid") if isinstance(raw, dict) else None) or [] if clean(a, 30)][:6]
    if not queries:
        problems.append("There are no usable search queries.")
    return {"queries": queries[:3], "avoid": avoid}, problems


# ── 8 caption_polish ────────────────────────────────────────────────────────────────────────────────────────────────────

def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a.casefold(), b.casefold()).ratio()


def validate_caption_polish(raw: Any, original: list[dict[str, Any]], *, max_line_words: int = 7) -> tuple[dict[str, Any], list[str]]:
    """Corrected words with the same count and the original timing (timing is copied from the original, never taken from the
    model), line breaks, and at most two emphasis words per line. A correction that changes a word beyond recognition is
    refused and the original word kept."""
    problems: list[str] = []
    new_words = (raw.get("words") if isinstance(raw, dict) else None) or []
    if len(new_words) != len(original):
        problems.append(f"The answer has {len(new_words)} words; there must be exactly {len(original)}.")
        return {"words": [dict(w) for w in original], "lines": [], "changed": 0}, problems
    out: list[dict[str, Any]] = []
    changed = 0
    for old, new in zip(original, new_words):
        text = clean(new.get("w") if isinstance(new, dict) else new, 60) or old["w"]
        if text != old["w"] and _similar(text, old["w"]) < 0.6:
            problems.append(f"“{old['w']}” was replaced by “{text}”, which is a different word.")
            text = old["w"]
        if text != old["w"]:
            changed += 1
        out.append({"w": text, "s_ms": old["s_ms"], "e_ms": old["e_ms"]})
    breaks = sorted({int(b) for b in (raw.get("breaks") or []) if isinstance(b, int) and 0 < b < len(out)})
    lines: list[list[int]] = []
    start = 0
    for stop in [*breaks, len(out)]:
        # a line is never longer than max_line_words: a long one is split evenly
        idx = list(range(start, stop))
        while len(idx) > max_line_words:
            lines.append(idx[:max_line_words])
            idx = idx[max_line_words:]
        if idx:
            lines.append(idx)
        start = stop
    emphasis: list[int] = []
    for i in (raw.get("emphasis") or []):
        if isinstance(i, int) and 0 <= i < len(out):
            line = next(ln for ln in lines if i in ln) if lines else []
            if sum(1 for e in emphasis if e in line) < 2 and i not in emphasis:
                emphasis.append(i)
    for w in out:
        w["emphasis"] = False
    for i in emphasis:
        out[i]["emphasis"] = True
    return {"words": out, "lines": lines, "changed": changed}, problems


# ── 9 title_description_tags ────────────────────────────────────────────────────────────────────────────────────────────

LIMITS: dict[str, dict[str, int]] = {
    "youtube": {"title": 100, "description": 5000, "tags_total": 500, "tags_max": 15},
    "youtube_shorts": {"title": 100, "description": 5000, "tags_total": 500, "tags_max": 10},
    "instagram": {"title": 125, "description": 2200, "tags_total": 300, "tags_max": 30},
    "tiktok": {"title": 150, "description": 2200, "tags_total": 200, "tags_max": 8},
    "linkedin": {"title": 150, "description": 3000, "tags_total": 200, "tags_max": 5},
    "facebook": {"title": 100, "description": 5000, "tags_total": 200, "tags_max": 10},
    "x": {"title": 100, "description": 280, "tags_total": 60, "tags_max": 3},
}


def validate_publish_text(raw: Any, platform: str, real_stamps: Iterable[str] = (), *, banned: Iterable[str] = ()) -> tuple[dict[str, Any], list[str]]:
    lim = LIMITS.get(platform, LIMITS["youtube"])
    raw = raw if isinstance(raw, dict) else {}
    problems: list[str] = []
    title = clean(raw.get("title"), 300)
    if len(title) > lim["title"]:
        problems.append(f"The title is {len(title)} characters; the limit is {lim['title']}.")
        title = title[: lim["title"]].rsplit(" ", 1)[0]
    desc = str(raw.get("description") or "").strip()
    if len(desc) > lim["description"]:
        problems.append(f"The description is {len(desc)} characters; the limit is {lim['description']}.")
        desc = desc[: lim["description"]].rsplit(" ", 1)[0]
    real = {s for s in real_stamps}
    bad = [s for s in _STAMP.findall(desc) if s not in real]
    if bad:
        problems.append(f"The description has times that are not chapters of the video: {', '.join(sorted(set(bad)))}.")
        for s in set(bad):
            desc = "\n".join(line for line in desc.splitlines() if s not in line)
    tags: list[str] = []
    total = 0
    for t in raw.get("tags") or []:
        tag = clean(t, 200).lstrip("#")
        if not tag or len(tag) > 30 or tag.casefold() in {x.casefold() for x in tags}:
            continue
        if total + len(tag) > lim["tags_total"] or len(tags) >= lim["tags_max"]:
            break
        tags.append(tag)
        total += len(tag)
    hit = _banned_hit(f"{title} {desc}", banned)
    if hit:
        problems.append(f"The text uses the banned word “{hit}”.")
    if not title:
        problems.append("There is no title.")
    return {"title": title, "description": desc, "tags": tags, "platform": platform}, problems


# ── 11 edit_plan ────────────────────────────────────────────────────────────────────────────────────────────────────────

def validate_edit_plan(
    raw: Any, duration_ms: int, *, pauses: Optional[list[tuple[int, int]]] = None, max_cut_share: float = 0.4, max_zooms: int = 12,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Edits from the allowed set, inside the media, in time order. A cut may remove at most `max_cut_share` of the video in all
    and a tighten_pause must sit on a real silence. The list is compiled into timeline operations by code, never by the model."""
    items = (raw.get("edits") if isinstance(raw, dict) else None) or []
    out: list[dict[str, Any]] = []
    problems: list[str] = []
    zooms = 0
    for it in items[:200]:
        if not isinstance(it, dict):
            continue
        kind = it.get("kind")
        s, e = it.get("start_ms"), it.get("end_ms")
        if kind not in EDIT_KINDS:
            problems.append(f"“{kind}” is not an edit that can be made.")
            continue
        if not (isinstance(s, int) and isinstance(e, int)) or isinstance(s, bool) or isinstance(e, bool) or not (0 <= s < e <= duration_ms):
            problems.append(f"An edit of kind {kind} is outside the video.")
            continue
        if kind == "zoom":
            if e - s > 8000 or zooms >= max_zooms:
                problems.append("A zoom is longer than 8 seconds, or there are too many zooms.")
                continue
            zooms += 1
        if kind == "tighten_pause" and pauses is not None:
            if not any(s >= ps - 50 and e <= pe + 50 for ps, pe in pauses):
                problems.append("A pause to tighten is not on a real silence.")
                continue
        out.append({"kind": kind, "start_ms": s, "end_ms": e, "reason": clean(it.get("reason"), 140)})
    out.sort(key=lambda x: (x["start_ms"], x["end_ms"]))
    cuts = [x for x in out if x["kind"] == "cut"]
    merged: list[dict[str, Any]] = []
    for c in cuts:
        if merged and c["start_ms"] < merged[-1]["end_ms"]:
            problems.append("Two cuts overlap.")
            out = [x for x in out if x is not c]
            continue
        merged.append(c)
    removed = sum(c["end_ms"] - c["start_ms"] for c in merged)
    if duration_ms and removed / duration_ms > max_cut_share:
        problems.append(f"The cuts remove {removed / duration_ms:.0%} of the video; the most allowed is {max_cut_share:.0%}.")
        out = [x for x in out if x["kind"] != "cut"]
    return out, problems


# ── 13 qa_critic ────────────────────────────────────────────────────────────────────────────────────────────────────────

def validate_critique(raw: Any) -> tuple[list[dict[str, str]], list[str]]:
    items = (raw.get("fixes") if isinstance(raw, dict) else None) or []
    out = []
    for it in items[:8]:
        if isinstance(it, dict) and clean(it.get("problem")) and clean(it.get("fix")):
            out.append({"where": clean(it.get("where"), 60) or "text", "problem": clean(it.get("problem"), 200), "fix": clean(it.get("fix"), 300)})
    return out, []


# ── 24 remy_voice_check_video ───────────────────────────────────────────────────────────────────────────────────────────

def validate_voice_check(raw: Any, lines: list[str]) -> tuple[dict[str, Any], list[str]]:
    """A verdict, which lines drift (by number), and a rewrite for each that keeps the meaning (every figure kept) and stays within
    a third of the original length."""
    raw = raw if isinstance(raw, dict) else {}
    problems: list[str] = []
    verdict = raw.get("verdict") if raw.get("verdict") in ("in_voice", "drifts") else None
    drifting: list[dict[str, Any]] = []
    for it in (raw.get("lines") or [])[:30]:
        if not isinstance(it, dict):
            continue
        n = it.get("line")
        if not isinstance(n, int) or isinstance(n, bool) or not (1 <= n <= len(lines)):
            problems.append(f"Line {n} does not exist.")
            continue
        original = lines[n - 1]
        rewrite = clean(it.get("rewrite"), 600)
        if not rewrite:
            continue
        ratio = len(rewrite) / max(1, len(original))
        if not (0.67 <= ratio <= 1.34):
            problems.append(f"The rewrite of line {n} changes the length too much.")
            continue
        if numbers_in(original) - numbers_in(rewrite):
            problems.append(f"The rewrite of line {n} drops a figure.")
            continue
        drifting.append({"line": n, "original": original, "rewrite": rewrite, "why": clean(it.get("why"), 160)})
    if verdict is None:
        verdict = "drifts" if drifting else "in_voice"
    return {"verdict": verdict, "lines": drifting}, problems
