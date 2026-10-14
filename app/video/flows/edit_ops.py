"""Whole-edit operations the one-click flows use: shift an edit later, insert a gap, add a music bed, add a brand intro, outro or sponsor
read. Each takes a valid edit list and returns a new valid one; nothing is changed in place."""
from __future__ import annotations

import copy
import uuid
from typing import Any, Optional

from app.video import edl

MUSIC_GAIN_DB = -14.0


def _fps(edit: dict[str, Any]) -> dict[str, int]:
    return edit["fps"]


def _ms_per_frame(edit: dict[str, Any]) -> float:
    return 1000.0 * _fps(edit)["den"] / _fps(edit)["num"]


def _uid(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


def shift(edit: dict[str, Any], frames: int) -> dict[str, Any]:
    """Everything starts `frames` later (clips, text, caption words and markers). Used to make room before the content."""
    out = copy.deepcopy(edit)
    if frames <= 0:
        return out
    for key in ("video", "audio", "text", "markers"):
        for c in out.get(key, []):
            c["at_f"] += frames
    delta_ms = int(round(frames * _ms_per_frame(edit)))
    for w in out["captions"]["words"]:
        w["s_ms"] += delta_ms
        w["e_ms"] += delta_ms
    out["duration_f"] = edit["duration_f"] + frames
    return edl.validate(out)


def insert_gap(edit: dict[str, Any], at_f: int, gap_f: int) -> dict[str, Any]:
    """A silent gap of `gap_f` frames opened at `at_f`: clips that span the point are split in two and everything after moves
    later, so nothing is cut or lost. Captions and markers after the point move too."""
    if gap_f <= 0:
        return copy.deepcopy(edit)
    out = copy.deepcopy(edit)

    def split_all(items: list[dict[str, Any]], timed_source: bool) -> list[dict[str, Any]]:
        result = []
        for c in items:
            end = c["at_f"] + c["dur_f"]
            if c["at_f"] < at_f < end:
                left = {**c, "dur_f": at_f - c["at_f"], "fade_out_f": 0}
                right = {**c, "id": _uid("s"), "at_f": at_f + gap_f, "dur_f": end - at_f, "fade_in_f": 0}
                if timed_source:
                    right["src_in_f"] = c.get("src_in_f", 0) + int(round((at_f - c["at_f"]) * c.get("speed", 1.0)))
                result.extend([left, right])
            elif c["at_f"] >= at_f:
                result.append({**c, "at_f": c["at_f"] + gap_f})
            else:
                result.append(c)
        return result

    out["video"] = split_all(out["video"], True)
    out["audio"] = split_all(out["audio"], True)
    out["text"] = split_all(out["text"], False)
    for m in out.get("markers", []):
        if m["at_f"] >= at_f:
            m["at_f"] += gap_f
    at_ms = at_f * _ms_per_frame(edit)
    delta_ms = int(round(gap_f * _ms_per_frame(edit)))
    for w in out["captions"]["words"]:
        if w["s_ms"] >= at_ms:
            w["s_ms"] += delta_ms
            w["e_ms"] += delta_ms
    out["duration_f"] = edit["duration_f"] + gap_f
    return edl.validate(out)


def with_music_bed(edit: dict[str, Any], media_id: str, *, gain_db: float = MUSIC_GAIN_DB) -> dict[str, Any]:
    """A music bed across the whole video that ducks under any voice. Does nothing to a video that has no length yet."""
    out = copy.deepcopy(edit)
    total = max(1, edl.end_frame(out) or edit["duration_f"])
    out["audio"] = [*out.get("audio", []), {
        "id": _uid("mus"), "media_id": media_id, "at_f": 0, "dur_f": total, "src_in_f": 0, "speed": 1.0, "gain_db": gain_db, "role": "music",
        "fade_in_f": min(15, total // 4), "fade_out_f": min(30, total // 4), "duck": True,
    }]
    return edl.validate(out)


def with_intro(edit: dict[str, Any], *, media_id: str, seconds: float, title: str = "", color: Optional[str] = None) -> dict[str, Any]:
    """A short brand intro before the content: its sound at the start and, over the opening, the title on the background colour."""
    fps = _fps(edit)
    frames = max(1, edl.seconds_to_frames(seconds, fps))
    out = shift(edit, frames)
    if color and len(color) == 7:
        out["background"] = color
    out["audio"] = [*out["audio"], {
        "id": _uid("intro"), "media_id": media_id, "at_f": 0, "dur_f": frames, "src_in_f": 0, "speed": 1.0, "gain_db": 0.0, "role": "voice",
        "fade_in_f": 0, "fade_out_f": 0, "duck": False,
    }]
    if title.strip():
        out["text"] = [*out["text"], {"id": _uid("introt"), "text": title.strip()[:200], "at_f": 0, "dur_f": frames, "x": 0.5, "y": 0.5,
                                       "size": 72, "color": "#FFFFFF", "bold": True}]
    return edl.validate(out)


def with_outro(edit: dict[str, Any], *, media_id: str, seconds: float, title: str = "") -> dict[str, Any]:
    """A short brand outro after the content: its sound, and the title over it."""
    fps = _fps(edit)
    frames = max(1, edl.seconds_to_frames(seconds, fps))
    out = copy.deepcopy(edit)
    start = edl.end_frame(out) or edit["duration_f"]
    out["audio"] = [*out["audio"], {
        "id": _uid("outro"), "media_id": media_id, "at_f": start, "dur_f": frames, "src_in_f": 0, "speed": 1.0, "gain_db": 0.0, "role": "voice",
        "fade_in_f": 0, "fade_out_f": 0, "duck": False,
    }]
    if title.strip():
        out["text"] = [*out["text"], {"id": _uid("outrot"), "text": title.strip()[:200], "at_f": start, "dur_f": frames, "x": 0.5, "y": 0.5,
                                       "size": 64, "color": "#FFFFFF", "bold": True}]
    out["duration_f"] = start + frames
    return edl.validate(out)


def with_sponsor(edit: dict[str, Any], *, media_id: str, seconds: float, at_ms: int, label: str = "") -> dict[str, Any]:
    """A sponsor read placed at a moment: a gap is opened there so the content continues after it, and the read fills the gap."""
    fps = _fps(edit)
    at_f = min(edit["duration_f"], max(0, edl.seconds_to_frames(at_ms / 1000, fps)))
    frames = max(1, edl.seconds_to_frames(seconds, fps))
    out = insert_gap(edit, at_f, frames)
    out["audio"] = [*out["audio"], {
        "id": _uid("spon"), "media_id": media_id, "at_f": at_f, "dur_f": frames, "src_in_f": 0, "speed": 1.0, "gain_db": 0.0, "role": "voice",
        "fade_in_f": 0, "fade_out_f": 0, "duck": False,
    }]
    if label.strip():
        out["text"] = [*out["text"], {"id": _uid("spont"), "text": label.strip()[:120], "at_f": at_f, "dur_f": frames, "x": 0.5, "y": 0.85,
                                       "size": 48, "color": "#FFFFFF", "bold": True}]
    return edl.validate(out)
