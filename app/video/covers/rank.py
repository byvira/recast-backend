"""Choosing cover frames: which moments to take pictures of, and which of the pictures make the best covers.

All of it works from the measurements the worker took (sharpness, brightness, contrast, how busy the top and bottom thirds are, a
fingerprint, the average colour and, when a detector was installed, face boxes), so ranking is quick, free and repeatable. A
picture that is nearly the same as a better one is dropped, and every kept picture carries plain reasons it was chosen.
"""
from __future__ import annotations

from typing import Any, Optional

MIN_GAP_S = 1.5


def candidate_times(duration_s: float, scenes: list[dict[str, Any]], moments: list[dict[str, Any]], *, limit: int = 24) -> list[float]:
    """Moments worth a picture: the middle of each shot (a frame right at a cut is often a blend), where each clip-worthy moment
    begins, and an even spread so a video with no cuts still gets candidates. Spaced at least MIN_GAP_S apart, and never the
    first or last half second."""
    if duration_s <= 0:
        return []
    lo, hi = 0.5, max(0.5, duration_s - 0.5)
    cuts = sorted({round(float(s["t"]), 2) for s in scenes if isinstance(s, dict) and 0 < float(s.get("t", 0)) < duration_s})
    edges = [0.0, *cuts, duration_s]
    picks: list[tuple[int, float]] = []                         # (priority, time): lower is kept first
    for a, b in zip(edges, edges[1:]):
        if b - a >= 1.0:
            picks.append((2, a + (b - a) / 2))
    for m in moments or []:
        if isinstance(m, dict) and isinstance(m.get("start_ms"), int) and m.get("state") != "rejected":
            picks.append((1, m["start_ms"] / 1000 + 1.0))
    even = max(4, min(12, int(duration_s // 20) + 4))
    for i in range(even):
        picks.append((3, duration_s * (i + 0.5) / even))
    chosen: list[float] = []
    for _, t in sorted(picks, key=lambda p: p[0]):
        t = round(min(max(t, lo), hi), 2)
        if all(abs(t - c) >= MIN_GAP_S for c in chosen):
            chosen.append(t)
        if len(chosen) >= limit:
            break
    return sorted(chosen)


def _ideal(value: float, centre: float, spread: float) -> float:
    return max(0.0, 1.0 - abs(value - centre) / spread)


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def _hue_distance(rgb: list[int], target: str) -> Optional[float]:
    """0 (same colour family) to 1 (opposite), by hue; None for a grey picture, which has no hue to compare."""
    import colorsys

    try:
        t = tuple(int(target.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except (ValueError, IndexError):
        return None
    h1, s1, _ = colorsys.rgb_to_hsv(*[c / 255 for c in rgb])
    h2, s2, _ = colorsys.rgb_to_hsv(*t)
    if s1 < 0.15 or s2 < 0.15:
        return None
    d = abs(h1 - h2)
    return min(d, 1 - d) * 2


def score_frame(f: dict[str, Any], *, text_room: str = "bottom", brand_hex: Optional[str] = None, faces_checked: bool = False) -> dict[str, Any]:
    """A 0 to 100 score and the reasons. Detail and exposure matter most; a face of good size and room for words add to it."""
    reasons: list[str] = []
    notes: list[str] = []
    sharp = min(1.0, f.get("sharpness", 0.0) / 0.12)             # about 0.12 is a crisp, detailed frame at the measuring size
    expo = _ideal(f.get("brightness", 0.5), 0.5, 0.5)
    contrast = min(1.0, f.get("contrast", 0.0) / 0.35)
    busy = f.get("bottom_busy" if text_room == "bottom" else "top_busy", 0.5)
    room = max(0.0, 1.0 - min(1.0, busy / 0.12))
    score = 0.34 * sharp + 0.24 * expo + 0.14 * contrast + 0.12 * room
    weight = 0.84
    if sharp >= 0.6:
        reasons.append("Sharp and detailed")
    elif sharp < 0.25:
        notes.append("A little soft or flat")
    if expo >= 0.7:
        reasons.append("Well lit")
    elif f.get("brightness", 0.5) < 0.18:
        notes.append("Quite dark")
    elif f.get("brightness", 0.5) > 0.85:
        notes.append("Very bright")
    if room >= 0.6:
        reasons.append("Clear space for words")
    faces = f.get("faces")
    face_size = 0.0
    if isinstance(faces, list) and faces:
        face_size = max(fc["w"] * fc["h"] for fc in faces)
        fit = _ideal(face_size, 0.08, 0.08)
        score += 0.16 * max(0.35, fit)
        weight += 0.16
        reasons.append("A face you can see" if face_size >= 0.012 else "A small face")
        if face_size < 0.006:
            notes.append("The face is very small")
    elif isinstance(faces, list):
        score += 0.04
        weight += 0.16
    if brand_hex and isinstance(f.get("rgb"), list):
        d = _hue_distance(f["rgb"], brand_hex)
        if d is not None and d <= 0.2:
            score += 0.05
            reasons.append("Matches the brand colour")
    final = int(round(100 * min(1.0, score / max(weight, 0.01))))
    return {"score": final, "reasons": reasons[:4], "notes": notes[:3], "faces_checked": faces_checked or isinstance(faces, list),
            "face_count": len(faces) if isinstance(faces, list) else None}


def rank_frames(
    frames: list[dict[str, Any]], *, text_room: str = "bottom", brand_hex: Optional[str] = None, faces_checked: bool = False,
    near_duplicate_bits: int = 6, dark_floor: float = 0.08,
) -> list[dict[str, Any]]:
    """Frames best first. A frame nearly the same as a better one is marked `duplicate_of` and listed last; an almost black
    frame is never a candidate."""
    scored = []
    for f in frames:
        if f.get("brightness", 0.5) < dark_floor:
            continue
        scored.append({**f, **score_frame(f, text_room=text_room, brand_hex=brand_hex, faces_checked=faces_checked)})
    scored.sort(key=lambda x: (-x["score"], x["t"]))
    kept: list[dict[str, Any]] = []
    dupes: list[dict[str, Any]] = []
    for f in scored:
        twin = next((k for k in kept if f.get("hash") and k.get("hash") and hamming(f["hash"], k["hash"]) <= near_duplicate_bits), None)
        if twin:
            dupes.append({**f, "duplicate_of": twin["id"]})
        else:
            kept.append({**f, "duplicate_of": None})
    return kept + dupes


def pick_distinct(ranked: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    """The first `count` frames that are not duplicates of an earlier pick, best first."""
    return [f for f in ranked if not f.get("duplicate_of")][:max(1, count)]
