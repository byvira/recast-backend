"""Planning a dub or a narration: which lines, how long each has, whether the translation fits, where each spoken line is placed and
how the finished edit list looks. Everything here is pure, so timing rules are tested without a voice or a video.

Time rules, in the order they are tried for every line:
1. the translation is judged by words against the seconds the original line had;
2. a line that is too long is shortened by asking again (once);
3. what is still long is sped up a little, never more than MAX_SPEEDUP;
4. what still does not fit may run into the silence before the next line, and is flagged either way so a person can decide.
"""
from __future__ import annotations

from typing import Any, Optional

from app.video import edl
from app.video.create import validate as V

FPS = 30
MAX_SPEEDUP = 1.12                # beyond this a voice sounds hurried
FIT_TOLERANCE = 1.05              # up to 5 percent over is not worth a change
MIN_SEGMENT_S = 2.5
MAX_SEGMENT_S = 14.0
ORIGINAL_DUCK_DB = -18.0          # the original voice, lowered under the dub


def segments_from_words(words: list[dict[str, Any]], *, min_s: float = MIN_SEGMENT_S, max_s: float = MAX_SEGMENT_S) -> list[dict[str, Any]]:
    """Spoken lines of the original: sentences, with very short ones joined to the next and very long ones cut at a pause. Each has
    `start_ms`, `end_ms`, `text` and `seconds` (the time it has before the next line starts, capped at its own length plus the
    silence that follows)."""
    from app.video.analysis import windows as W

    sents = W.sentences(words)
    merged: list[dict[str, Any]] = []
    for s in sents:
        if merged and (merged[-1]["end_ms"] - merged[-1]["start_ms"]) / 1000 < min_s and (s["end_ms"] - merged[-1]["start_ms"]) / 1000 <= max_s:
            top = merged[-1]
            top["end_ms"] = s["end_ms"]
            top["text"] = f"{top['text']} {s['text']}".strip()
            continue
        merged.append({"start_ms": s["start_ms"], "end_ms": s["end_ms"], "text": s["text"]})
    if len(merged) > 1 and (merged[-1]["end_ms"] - merged[-1]["start_ms"]) / 1000 < min_s:
        last = merged.pop()
        merged[-1]["end_ms"] = last["end_ms"]
        merged[-1]["text"] = f"{merged[-1]['text']} {last['text']}".strip()
    for n, seg in enumerate(merged):
        nxt = merged[n + 1]["start_ms"] if n + 1 < len(merged) else seg["end_ms"] + 1500
        seg["i"] = n + 1
        seg["slot_s"] = round((seg["end_ms"] - seg["start_ms"]) / 1000, 2)
        seg["room_s"] = round(max(seg["slot_s"], (nxt - seg["start_ms"]) / 1000 - 0.1), 2)
    return merged


def fit_status(text: str, slot_s: float, language: str) -> dict[str, Any]:
    """How a translated line sits in the seconds the original had: `ratio` is spoken time over available time."""
    spoken = V.spoken_seconds(text, language)
    ratio = round(spoken / max(0.5, slot_s), 3)
    if ratio <= FIT_TOLERANCE:
        status = "ok"
    elif ratio <= MAX_SPEEDUP:
        status = "speed_up"
    else:
        status = "too_long"
    return {"spoken_s": spoken, "ratio": ratio, "status": status}


def words_per_second(language: str) -> float:
    base = (language or "en").split("+")[0].split("-")[0].lower()
    return round(V.WPM.get(base, V.WPM["default"]) / 60.0, 2)


def place(segments: list[dict[str, Any]], audio_seconds: list[Optional[float]]) -> list[dict[str, Any]]:
    """Where each spoken line goes, from the real length of its audio. A line longer than its slot is sped up to at most
    MAX_SPEEDUP; if it is still longer than the time up to the next line it keeps playing into the silence and is flagged
    `overruns` (it is never cut off, and never overlaps the next line because the next line is placed after it)."""
    placed: list[dict[str, Any]] = []
    cursor_ms = 0
    for seg, secs in zip(segments, audio_seconds):
        if secs is None or secs <= 0:
            placed.append({**seg, "flag": "missing", "at_ms": seg["start_ms"], "dur_ms": 0, "speed": 1.0})
            continue
        speed = 1.0
        slot = seg["slot_s"]
        flag = "ok"
        if secs > slot * FIT_TOLERANCE:
            speed = min(MAX_SPEEDUP, secs / slot)
            flag = "sped_up"
        dur = secs / speed
        at = max(seg["start_ms"], cursor_ms)
        if dur > seg["room_s"] + 0.05:
            flag = "overruns"
        cursor_ms = at + int(round(dur * 1000)) + 40
        placed.append({**seg, "flag": flag, "at_ms": at, "dur_ms": int(round(dur * 1000)), "speed": round(speed, 3),
                       "late_ms": max(0, at - seg["start_ms"])})
    return placed


def caption_words(text: str, at_ms: int, dur_ms: int) -> list[dict[str, Any]]:
    """Word timing for a spoken line, spread across its real audio length by how long each word is. Captions from a dub use
    this, so they follow the dub's own timing and not the original's."""
    words = text.split()
    if not words or dur_ms <= 0:
        return []
    weights = [max(1, len(w)) for w in words]
    total = sum(weights)
    out = []
    t = float(at_ms)
    for w, wt in zip(words, weights):
        span = dur_ms * wt / total
        out.append({"w": w, "s_ms": int(round(t)), "e_ms": int(round(t + span * 0.92))})
        t += span
    return out


def build_dub_edit(
    *, width: int, height: int, duration_s: float, placed: list[dict[str, Any]], original_db: float = ORIGINAL_DUCK_DB,
    captions: bool = True, accent: str = "#38bdf8",
) -> dict[str, Any]:
    """The edit list of a dubbed video in the worker's own form: the original picture with its sound lowered, and each spoken line
    as a voice clip at its place. Inputs are named `src` (the video) and `d<i>` (each line's audio)."""
    frames = max(1, edl.seconds_to_frames(duration_s, {"num": FPS, "den": 1}))
    audio = []
    caption_list: list[dict[str, Any]] = []
    for p in placed:
        if p.get("flag") == "missing" or p["dur_ms"] <= 0:
            continue
        at_f = int(round(p["at_ms"] / 1000 * FPS))
        dur_f = max(1, int(round(p["dur_ms"] / 1000 * FPS)))
        audio.append({"id": f"d{p['i']}", "src": f"d{p['i']}", "at_f": at_f, "dur_f": dur_f, "src_in_f": 0,
                      "speed": float(p["speed"]) if p["speed"] != 1.0 else 1.0, "role": "voice", "gain_db": 0})
        if captions:
            caption_list.extend(caption_words(p["text_out"], p["at_ms"], p["dur_ms"]))
    return {
        "version": 1, "fps": {"num": FPS, "den": 1}, "width": width, "height": height, "duration_f": frames, "background": "#000000",
        "video": [{"id": "orig", "src": "src", "at_f": 0, "dur_f": frames, "src_in_f": 0, "fit": "cover", "audio": True,
                   "gain_db": float(original_db)}],
        "audio": audio, "text": [],
        "captions": {"enabled": bool(caption_list), "accent": accent if len(accent) == 7 else "#38bdf8", "words": caption_list},
        "effects": {},
    }


def build_narration_edit(
    *, width: int, height: int, duration_s: float, narration_seconds: float, has_music: bool = False, fit: str = "cover",
    captions: Optional[list[dict[str, Any]]] = None, original_audio_db: Optional[float] = None, accent: str = "#38bdf8",
) -> dict[str, Any]:
    """Narration over a video that has no speech: the video (its own sound lowered or off), one narration clip from the start,
    and optionally a music bed that ducks under the voice. The video loops no further than its own length: the output is as long
    as the longer of the video and the narration, and after the video ends the screen shows the background colour."""
    total = max(duration_s, narration_seconds)
    frames = max(1, edl.seconds_to_frames(total, {"num": FPS, "den": 1}))
    video_frames = max(1, edl.seconds_to_frames(duration_s, {"num": FPS, "den": 1}))
    audio = [{"id": "narration", "src": "voice", "at_f": 0, "dur_f": max(1, edl.seconds_to_frames(narration_seconds, {"num": FPS, "den": 1})),
              "src_in_f": 0, "role": "voice", "gain_db": 0}]
    if has_music:
        audio.append({"id": "music", "src": "music", "at_f": 0, "dur_f": frames, "src_in_f": 0, "role": "music", "gain_db": -12, "duck": True})
    cap = captions or []
    return {
        "version": 1, "fps": {"num": FPS, "den": 1}, "width": width, "height": height, "duration_f": frames, "background": "#000000",
        "video": [{"id": "pic", "src": "src", "at_f": 0, "dur_f": video_frames, "src_in_f": 0, "fit": fit,
                   "audio": original_audio_db is not None, "gain_db": float(original_audio_db or 0.0)}],
        "audio": audio, "text": [],
        "captions": {"enabled": bool(cap), "accent": accent if len(accent) == 7 else "#38bdf8", "words": cap},
        "effects": {},
    }


def summary(placed: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts a person reads: how many lines fit as they are, how many were sped up, how many overrun, how many are missing."""
    counts = {"ok": 0, "sped_up": 0, "overruns": 0, "missing": 0}
    for p in placed:
        counts[p.get("flag", "ok")] = counts.get(p.get("flag", "ok"), 0) + 1
    return {"lines": len(placed), **counts,
            "needs_review": [p["i"] for p in placed if p.get("flag") in ("overruns", "missing")]}
