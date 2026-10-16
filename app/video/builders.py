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


def clip_from_video(
    media: dict[str, Any], *, start_s: float, end_s: float, words: list[dict[str, Any]], size: str = "vertical", title: str = "",
    accent: str = "#38bdf8", background: str = "#0f172a", fit: str = "cover",
) -> dict[str, Any]:
    """One short clip cut out of a longer video: just that stretch, in the chosen shape (the picture fills the frame and the
    sides are cropped; use fit="blur" to keep the whole picture on a soft backdrop), with captions from its own words and an
    optional title. `words` are {"w", "s_ms", "e_ms"} in the source video's time."""
    probe = media.get("probe") or {}
    video = probe.get("video") or {}
    fps = edl.fps_from_float(video.get("fps") or video.get("tbr"))
    total = float(probe.get("duration_s") or media.get("duration_s") or 0)
    if total <= 0:
        raise edl.EditError("This video's length is not known yet, so a clip can't be made from it.")
    start_s = max(0.0, float(start_s))
    end_s = min(total, float(end_s))
    if end_s - start_s < 1.0:
        raise edl.EditError("A clip has to be at least one second long.")
    width, height = edl.CANVAS_SIZES[size]
    frames = max(1, edl.seconds_to_frames(end_s - start_s, fps))
    edit = edl.blank(size, fps)
    edit["background"] = background if len(background) == 7 and background.startswith("#") else "#0f172a"
    edit["video"] = [{
        "id": "v1", "media_id": media["id"], "at_f": 0, "dur_f": frames, "src_in_f": edl.seconds_to_frames(start_s, fps), "speed": 1.0,
        "fit": fit if fit in edl.FITS else "cover", "fade_in_f": 0, "fade_out_f": 0, "audio": True, "gain_db": 0.0,
    }]
    lo, hi = int(start_s * 1000), int(end_s * 1000)
    shifted = []
    for w in words:
        if w["e_ms"] <= lo or w["s_ms"] >= hi:
            continue
        shifted.append({"w": str(w["w"]), "s_ms": max(0, w["s_ms"] - lo), "e_ms": min(hi - lo, max(w["s_ms"] - lo, w["e_ms"] - lo))})
    edit["captions"] = {"enabled": bool(shifted), "accent": accent if len(accent) == 7 else "#38bdf8", "words": shifted}
    if title.strip():
        edit["text"] = [{"id": "title", "text": title.strip()[:200], "at_f": 0, "dur_f": min(frames, edl.seconds_to_frames(3.0, fps)),
                         "x": 0.5, "y": 0.1, "size": 64, "color": "#FFFFFF", "bold": True}]
    edit["duration_f"] = frames
    edit["width"], edit["height"] = width, height
    return edl.validate(edit)


def blank_project(size: str = "landscape") -> dict[str, Any]:
    return edl.validate(edl.blank(size))


MAX_BEATS = 12
BEAT_GAP_S = 0.35
MAX_REPEATS = 8


def from_beats(
    *, beats: list[dict[str, Any]], size: str = "vertical", accent: str = "#38bdf8", background: str = "#0f172a", captions: bool = True,
) -> dict[str, Any]:
    """A short video made from a script: one scene for each beat. A beat is {"text", "on_screen", "voice": [{"media_id", "seconds"}],
    "clip": {"media_id", "seconds"} or None}. A scene lasts as long as its spoken parts (plus a short breath), its footage plays from
    the start and repeats when it is shorter than the scene, and a scene with no footage shows the background colour. Captions follow
    the voice."""
    from app.video.narrate import plan as narrate_plan

    if not beats:
        raise edl.EditError("There are no scenes to make a video from.")
    if len(beats) > MAX_BEATS:
        raise edl.EditError(f"A video made from a script can have at most {MAX_BEATS} scenes.")
    fps = {"num": 30, "den": 1}
    edit = edl.blank(size, fps)
    edit["background"] = background if len(background) == 7 and background.startswith("#") else "#0f172a"
    video: list[dict[str, Any]] = []
    audio: list[dict[str, Any]] = []
    text: list[dict[str, Any]] = []
    words: list[dict[str, Any]] = []
    at = 0
    for n, beat in enumerate(beats):
        parts = beat.get("voice") or []
        spoken_s = sum(float(p["seconds"]) for p in parts)
        if spoken_s <= 0:
            raise edl.EditError(f"Scene {n + 1} has no spoken part.")
        scene_f = max(1, edl.seconds_to_frames(spoken_s + BEAT_GAP_S, fps))
        cursor = at
        for k, part in enumerate(parts):
            part_f = max(1, edl.seconds_to_frames(float(part["seconds"]), fps))
            audio.append({
                "id": f"v{n + 1}_{k + 1}", "media_id": part["media_id"], "at_f": cursor, "dur_f": part_f, "src_in_f": 0, "speed": 1.0,
                "gain_db": 0.0, "role": "voice", "fade_in_f": 0, "fade_out_f": 0, "duck": False,
            })
            cursor += part_f
        clip = beat.get("clip")
        if clip and float(clip.get("seconds") or 0) > 0:
            own_f = max(1, edl.seconds_to_frames(float(clip["seconds"]), fps))
            pos, tries = 0, 0
            while pos < scene_f and tries < MAX_REPEATS:
                seg = min(own_f, scene_f - pos)
                video.append({
                    "id": f"c{n + 1}_{tries + 1}", "media_id": clip["media_id"], "at_f": at + pos, "dur_f": seg, "src_in_f": 0, "speed": 1.0,
                    "fit": "cover", "fade_in_f": min(6, seg // 4) if (n or tries) else 0, "fade_out_f": 0, "audio": False, "gain_db": 0.0,
                })
                pos += seg
                tries += 1
        label = str(beat.get("on_screen") or "").strip()
        if label:
            text.append({"id": f"t{n + 1}", "text": label[:80], "at_f": at, "dur_f": scene_f, "x": 0.5, "y": 0.14, "size": 56, "color": "#FFFFFF", "bold": True})
        if captions:
            words.extend(narrate_plan.caption_words(str(beat.get("text") or ""), int(at / 30 * 1000), int(spoken_s * 1000)))
        at += scene_f
    edit["video"], edit["audio"], edit["text"] = video, audio, text
    edit["captions"] = {"enabled": bool(words), "accent": accent if len(accent) == 7 else "#38bdf8", "words": words}
    edit["duration_f"] = at
    return edl.validate(edit)
