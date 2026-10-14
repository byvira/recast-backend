"""The starting edit for a new project: from an uploaded video, from a recording, or blank. Each builds a valid edit list that
opens straight in the editor and renders as it stands."""
from __future__ import annotations

from typing import Any, Optional

from app.video import edl

MAX_LONG_SIDE = 1920


def _even(value: float) -> int:
    return max(16, int(round(value / 2.0)) * 2)


def canvas_for_video(width: int, height: int) -> tuple[int, int]:
    """The source's shown size, scaled down so the long side is at most 1920, kept to even numbers."""
    long_side = max(width, height)
    scale = min(1.0, MAX_LONG_SIDE / long_side) if long_side else 1.0
    return _even(width * scale), _even(height * scale)


def from_video(media: dict[str, Any], *, size: Optional[str] = None) -> dict[str, Any]:
    """One clip, the whole video, with its own sound. A chosen canvas that does not match the video's shape fills the sides with
    a blurred copy instead of cropping the picture."""
    probe = media.get("probe") or {}
    video = probe.get("video") or {}
    fps = edl.fps_from_float(video.get("fps") or video.get("tbr"))
    shown_w = video.get("shown_width") or media.get("width") or 1920
    shown_h = video.get("shown_height") or media.get("height") or 1080
    if size:
        width, height = edl.CANVAS_SIZES[size]
    else:
        width, height = canvas_for_video(int(shown_w), int(shown_h))
    seconds = float(probe.get("duration_s") or media.get("duration_s") or 0)
    if seconds <= 0:
        raise edl.EditError("This video's length is not known yet, so a project can't be made from it.")
    frames = max(1, edl.seconds_to_frames(seconds, fps))
    same_shape = abs((width / height) - (shown_w / shown_h)) < 0.02
    edit = edl.blank("landscape", fps)
    edit.update({"width": width, "height": height})
    edit["video"] = [{
        "id": "v1", "media_id": media["id"], "at_f": 0, "dur_f": frames, "src_in_f": 0, "speed": 1.0,
        "fit": "cover" if same_shape else "blur", "fade_in_f": 0, "fade_out_f": 0, "audio": True, "gain_db": 0.0,
    }]
    edit["duration_f"] = frames
    return edl.validate(edit)


def from_recording(
    *, voice_media_id: str, seconds: float, words: list[dict[str, Any]], size: str, title: str = "",
    cover_media_id: Optional[str] = None, accent: str = "#38bdf8", background: str = "#0f172a", start_s: float = 0.0,
) -> dict[str, Any]:
    """A recording as a video: its voice, the brand's cover (or a plain fill), the title and word-by-word captions, as pieces
    that can each be edited. `words` are {word, start_s, end_s} in the recording's own time."""
    fps = {"num": 30, "den": 1}
    if seconds <= 0:
        raise edl.EditError("The recording's length is not known yet, so a project can't be made from it.")
    frames = max(1, edl.seconds_to_frames(seconds, fps))
    edit = edl.blank(size, fps)
    edit["background"] = background if len(background) == 7 and background.startswith("#") else "#0f172a"
    if cover_media_id:
        edit["video"] = [{"id": "cover", "media_id": cover_media_id, "at_f": 0, "dur_f": frames, "src_in_f": 0, "speed": 1.0,
                          "fit": "cover", "fade_in_f": 0, "fade_out_f": 0, "audio": False, "gain_db": 0.0}]
    edit["audio"] = [{
        "id": "voice", "media_id": voice_media_id, "at_f": 0, "dur_f": frames, "src_in_f": edl.seconds_to_frames(start_s, fps),
        "speed": 1.0, "gain_db": 0.0, "role": "voice", "fade_in_f": 0, "fade_out_f": 0, "duck": False,
    }]
    if title.strip():
        edit["text"] = [{"id": "title", "text": title.strip()[:200], "at_f": 0, "dur_f": frames, "x": 0.5, "y": 0.12,
                         "size": 64, "color": "#FFFFFF", "bold": True}]
    end_ms = int(seconds * 1000)
    shifted = []
    for w in words:
        s = int(round((float(w["start_s"]) - start_s) * 1000))
        e = int(round((float(w["end_s"]) - start_s) * 1000))
        if e <= 0 or s >= end_ms:
            continue
        shifted.append({"w": str(w["word"]), "s_ms": max(0, s), "e_ms": min(end_ms, max(s, e))})
    edit["captions"] = {"enabled": bool(shifted), "accent": accent if len(accent) == 7 else "#38bdf8", "words": shifted}
    edit["duration_f"] = frames
    return edl.validate(edit)


def blank_project(size: str = "landscape") -> dict[str, Any]:
    return edl.validate(edl.blank(size))
