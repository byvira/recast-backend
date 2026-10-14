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


MIN_SECONDS_PER_PICTURE = 1.0
MAX_SECONDS_PER_PICTURE = 30.0
MAX_PICTURES = 60


def from_pictures(
    *, picture_media_ids: list[str], size: str = "vertical", seconds_per_picture: float = 3.0, title: str = "",
    voice_media_id: Optional[str] = None, voice_seconds: Optional[float] = None, music_media_id: Optional[str] = None,
    words: Optional[list[dict[str, Any]]] = None, accent: str = "#38bdf8", background: str = "#0f172a", motion: bool = True,
) -> dict[str, Any]:
    """A slideshow: each picture on screen in turn, with a slow zoom so nothing is frozen, optionally over a voice and a music bed.
    With a voice, the pictures share the voice's length evenly (so the last picture is not cut off and the video ends with the
    voice); without one, each stays for `seconds_per_picture`. Caption words are the voice's own timing."""
    if not picture_media_ids:
        raise edl.EditError("Choose at least one picture.")
    if len(picture_media_ids) > MAX_PICTURES:
        raise edl.EditError(f"A slideshow can have at most {MAX_PICTURES} pictures.")
    fps = {"num": 30, "den": 1}
    if voice_media_id:
        if not voice_seconds or voice_seconds <= 0:
            raise edl.EditError("The voice's length is not known yet, so a slideshow can't be timed to it.")
        total_f = max(len(picture_media_ids), edl.seconds_to_frames(voice_seconds, fps))
        base, extra = divmod(total_f, len(picture_media_ids))
        lengths = [base + (1 if i < extra else 0) for i in range(len(picture_media_ids))]
    else:
        if not (MIN_SECONDS_PER_PICTURE <= seconds_per_picture <= MAX_SECONDS_PER_PICTURE):
            raise edl.EditError(f"Each picture can stay on screen for {MIN_SECONDS_PER_PICTURE:g} to {MAX_SECONDS_PER_PICTURE:g} seconds.")
        each = edl.seconds_to_frames(seconds_per_picture, fps)
        lengths = [each] * len(picture_media_ids)
        total_f = each * len(picture_media_ids)

    edit = edl.blank(size, fps)
    edit["background"] = background if len(background) == 7 and background.startswith("#") else "#0f172a"
    at = 0
    clips = []
    for i, (mid, dur) in enumerate(zip(picture_media_ids, lengths)):
        clips.append({
            "id": f"p{i + 1}", "media_id": mid, "at_f": at, "dur_f": dur, "src_in_f": 0, "speed": 1.0, "fit": "cover",
            "fade_in_f": min(8, dur // 4) if i else 0, "fade_out_f": 0, "audio": False, "gain_db": 0.0, "motion": bool(motion),
        })
        at += dur
    edit["video"] = clips
    audio = []
    if voice_media_id:
        audio.append({"id": "voice", "media_id": voice_media_id, "at_f": 0, "dur_f": total_f, "src_in_f": 0, "speed": 1.0, "gain_db": 0.0,
                      "role": "voice", "fade_in_f": 0, "fade_out_f": 0, "duck": False})
    if music_media_id:
        audio.append({"id": "music", "media_id": music_media_id, "at_f": 0, "dur_f": total_f, "src_in_f": 0, "speed": 1.0,
                      "gain_db": -12.0 if voice_media_id else -4.0, "role": "music", "fade_in_f": 15, "fade_out_f": 30,
                      "duck": bool(voice_media_id)})
    edit["audio"] = audio
    if title.strip():
        edit["text"] = [{"id": "title", "text": title.strip()[:200], "at_f": 0, "dur_f": min(total_f, 90), "x": 0.5, "y": 0.12,
                         "size": 64, "color": "#FFFFFF", "bold": True}]
    shifted = [{"w": str(w["word"]), "s_ms": int(round(float(w["start_s"]) * 1000)), "e_ms": int(round(float(w["end_s"]) * 1000))}
               for w in (words or [])]
    edit["captions"] = {"enabled": bool(shifted), "accent": accent if len(accent) == 7 else "#38bdf8", "words": shifted}
    edit["duration_f"] = total_f
    return edl.validate(edit)


def blank_project(size: str = "landscape") -> dict[str, Any]:
    return edl.validate(edl.blank(size))
