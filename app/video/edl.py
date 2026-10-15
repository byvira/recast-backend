"""The edit list: one description of a video that the editor saves, the preview plays and the render worker draws.

Time is whole frames of the project's own frame rate, so a cut means the same thing everywhere and nothing drifts over a
three hour timeline. Values out of range are refused with a message that says where; nothing is ever clamped quietly.
A clip names a library item by id (`media_id`), never by address, so a project can only use what its workspace owns.
"""
from __future__ import annotations

import copy
import re
from typing import Any, Iterable, Optional

SCHEMA_VERSION = 1
FITS = ("cover", "contain", "blur")
TRACKS = ("video", "audio", "text", "shapes")
ANIMATIONS = ("none", "fade", "pop", "slide")
MAX_SHAPES = 200
#: The families a caption or text can be set in. The worker bundles the same files (scripts.CHOOSABLE_FAMILIES).
FONT_KEYS = ("inter", "lora", "open sans", "oswald", "playfair display", "poppins", "roboto", "work sans")
MAX_BLEEPS = 500
DEFAULT_LOOK = {"brightness": 0.0, "contrast": 1.0, "saturation": 1.0}
DEFAULT_CROP = {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}
ROLES = ("voice", "music", "effect")
MAX_CLIPS = 5000
MAX_WORDS = 400_000
MAX_MARKERS = 2000
MAX_SIDE = 4096
MAX_TEXT = 2000
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

CANVAS_SIZES: dict[str, tuple[int, int]] = {
    "square": (1080, 1080), "vertical": (1080, 1920), "landscape": (1920, 1080), "portrait": (1080, 1350),
}

# Frame rates that arrive from a camera as a rounded number are put back to their exact broadcast fraction.
_BROADCAST = {23.976: (24000, 1001), 29.97: (30000, 1001), 47.952: (48000, 1001), 59.94: (60000, 1001), 119.88: (120000, 1001)}


class EditError(ValueError):
    """The edit list breaks a rule. The message names the item and is safe to show the member."""


def _need(ok: bool, message: str) -> None:
    if not ok:
        raise EditError(message)


def _int(d: dict, key: str, where: str, *, lo: int = 0, hi: int = 10**9, default: Optional[int] = None) -> int:
    value = d.get(key, default)
    _need(isinstance(value, int) and not isinstance(value, bool), f"{where}: {key} must be a whole number.")
    _need(lo <= value <= hi, f"{where}: {key} must be between {lo} and {hi}.")
    return value


def _num(d: dict, key: str, where: str, *, lo: float, hi: float, default: Optional[float] = None) -> float:
    value = d.get(key, default)
    _need(isinstance(value, (int, float)) and not isinstance(value, bool), f"{where}: {key} must be a number.")
    _need(lo <= float(value) <= hi, f"{where}: {key} must be between {lo} and {hi}.")
    return float(value)


def _color(d: dict, key: str, where: str, default: str) -> str:
    value = d.get(key, default)
    _need(isinstance(value, str) and bool(_COLOR.match(value)), f"{where}: {key} must look like #RRGGBB.")
    return value


def _font_key(c: dict, where: str) -> str:
    value = c.get("font", "") or ""
    _need(isinstance(value, str) and (value == "" or value.lower() in FONT_KEYS), f"{where}: font must be one of {', '.join(FONT_KEYS)}.")
    return value.lower()


def _anim(c: dict, key: str, where: str) -> str:
    value = c.get(key, "none")
    _need(value in ANIMATIONS, f"{where}: {key} must be one of {', '.join(ANIMATIONS)}.")
    return value


def _look(c: dict, where: str) -> dict[str, float]:
    raw = c.get("look") or {}
    _need(isinstance(raw, dict), f"{where}: the look must be an object.")
    return {
        "brightness": _num(raw, "brightness", f"{where} look", lo=-1.0, hi=1.0, default=0.0),
        "contrast": _num(raw, "contrast", f"{where} look", lo=0.0, hi=2.0, default=1.0),
        "saturation": _num(raw, "saturation", f"{where} look", lo=0.0, hi=3.0, default=1.0),
    }


def _crop(c: dict, where: str) -> dict[str, float]:
    raw = c.get("crop") or {}
    _need(isinstance(raw, dict), f"{where}: the crop must be an object.")
    out = {
        "x": _num(raw, "x", f"{where} crop", lo=0.0, hi=1.0, default=0.0),
        "y": _num(raw, "y", f"{where} crop", lo=0.0, hi=1.0, default=0.0),
        "w": _num(raw, "w", f"{where} crop", lo=0.05, hi=1.0, default=1.0),
        "h": _num(raw, "h", f"{where} crop", lo=0.05, hi=1.0, default=1.0),
    }
    _need(out["x"] + out["w"] <= 1.0001 and out["y"] + out["h"] <= 1.0001, f"{where}: the crop is outside the picture.")
    return out


def fps_from_float(value: Optional[float]) -> dict[str, int]:
    """A frame rate as an exact fraction. Camera rates such as 29.97 become 30000/1001; anything else is rounded to whole."""
    if not value or value <= 0:
        return {"num": 30, "den": 1}
    for rate, (num, den) in _BROADCAST.items():
        if abs(value - rate) < 0.02:
            return {"num": num, "den": den}
    return {"num": max(1, min(120, int(round(value)))), "den": 1}


def seconds_to_frames(seconds: float, fps: dict[str, int]) -> int:
    return int(round(seconds * fps["num"] / fps["den"]))


def frames_to_seconds(frames: int, fps: dict[str, int]) -> float:
    return frames * fps["den"] / fps["num"]


def blank(size: str = "landscape", fps: Optional[dict[str, int]] = None) -> dict[str, Any]:
    width, height = CANVAS_SIZES[size]
    return {
        "schema_version": SCHEMA_VERSION, "fps": fps or {"num": 30, "den": 1}, "width": width, "height": height,
        "duration_f": 0, "background": "#000000", "video": [], "audio": [], "text": [],
        "captions": {"enabled": False, "accent": "#38bdf8", "words": []}, "markers": [],
    }


def end_frame(edit: dict[str, Any]) -> int:
    ends = [c["at_f"] + c["dur_f"] for key in ("video", "audio", "text", "shapes") for c in edit.get(key, [])]
    return max(ends) if ends else 0


def media_ids(edit: dict[str, Any]) -> set[str]:
    # a shape that carries a library item is a picture over the video, so its item is one of the project's files
    return {c["media_id"] for key in ("video", "audio", "shapes") for c in edit.get(key, []) if c.get("media_id")}


def validate(raw: Any) -> dict[str, Any]:
    """Check an edit list and return it in its canonical form (defaults filled, duration worked out). Raises EditError."""
    _need(isinstance(raw, dict), "The edit must be an object.")
    version = raw.get("schema_version", SCHEMA_VERSION)
    _need(version == SCHEMA_VERSION, f"This editor understands edit version {SCHEMA_VERSION}, not {version}.")

    fps_raw = raw.get("fps")
    _need(isinstance(fps_raw, dict), "The frame rate is missing.")
    num = _int(fps_raw, "num", "Frame rate", lo=1, hi=240_000)
    den = _int(fps_raw, "den", "Frame rate", lo=1, hi=1001, default=1)
    _need(1 <= num / den <= 120, "The frame rate must be between 1 and 120 frames a second.")
    fps = {"num": num, "den": den}
    width = _int(raw, "width", "Canvas", lo=16, hi=MAX_SIDE)
    height = _int(raw, "height", "Canvas", lo=16, hi=MAX_SIDE)
    _need(width % 2 == 0 and height % 2 == 0, "The canvas width and height must be even numbers.")

    seen: set[str] = set()

    def uid(value: Any, label: str) -> str:
        _need(isinstance(value, str) and bool(_ID.match(value)), f"Every {label} needs an id of letters, numbers, - or _.")
        _need(value not in seen, f"Two items share the id {value}.")
        seen.add(value)
        return value

    lists = {key: raw.get(key, []) for key in ("video", "audio", "text")}
    for key, value in lists.items():
        _need(isinstance(value, list), f"{key} must be a list.")
    _need(sum(len(v) for v in lists.values()) <= MAX_CLIPS, f"A project can hold at most {MAX_CLIPS} items.")

    video: list[dict] = []
    for c in lists["video"]:
        _need(isinstance(c, dict), "A video clip must be an object.")
        w = f"Video clip {c.get('id')}"
        _need(isinstance(c.get("media_id"), str) and c["media_id"], f"{w}: it must name a library item.")
        fit = c.get("fit", "cover")
        _need(fit in FITS, f"{w}: fit must be one of {', '.join(FITS)}.")
        item = {
            "id": uid(c.get("id"), "clip"), "media_id": c["media_id"], "at_f": _int(c, "at_f", w), "dur_f": _int(c, "dur_f", w, lo=1),
            "src_in_f": _int(c, "src_in_f", w, default=0), "speed": _num(c, "speed", w, lo=0.5, hi=2.0, default=1.0), "fit": fit,
            "fade_in_f": _int(c, "fade_in_f", w, default=0), "fade_out_f": _int(c, "fade_out_f", w, default=0),
            "audio": bool(c.get("audio", True)), "gain_db": _num(c, "gain_db", w, lo=-60, hi=24, default=0.0),
            "motion": bool(c.get("motion", False)), "freeze": bool(c.get("freeze", False)),
            "look": _look(c, w), "crop": _crop(c, w),
        }
        _need(item["fade_in_f"] + item["fade_out_f"] <= item["dur_f"], f"{w}: the fades are longer than the clip.")
        video.append(item)

    audio: list[dict] = []
    for c in lists["audio"]:
        _need(isinstance(c, dict), "An audio clip must be an object.")
        w = f"Audio clip {c.get('id')}"
        _need(isinstance(c.get("media_id"), str) and c["media_id"], f"{w}: it must name a library item.")
        role = c.get("role", "voice")
        _need(role in ROLES, f"{w}: role must be one of {', '.join(ROLES)}.")
        item = {
            "id": uid(c.get("id"), "clip"), "media_id": c["media_id"], "at_f": _int(c, "at_f", w), "dur_f": _int(c, "dur_f", w, lo=1),
            "src_in_f": _int(c, "src_in_f", w, default=0), "speed": _num(c, "speed", w, lo=0.5, hi=2.0, default=1.0),
            "gain_db": _num(c, "gain_db", w, lo=-60, hi=24, default=0.0), "role": role,
            "fade_in_f": _int(c, "fade_in_f", w, default=0), "fade_out_f": _int(c, "fade_out_f", w, default=0),
            "duck": bool(c.get("duck", role == "music")),
        }
        _need(item["fade_in_f"] + item["fade_out_f"] <= item["dur_f"], f"{w}: the fades are longer than the clip.")
        audio.append(item)

    text: list[dict] = []
    for c in lists["text"]:
        _need(isinstance(c, dict), "A text item must be an object.")
        w = f"Text {c.get('id')}"
        body = c.get("text")
        _need(isinstance(body, str) and 0 < len(body) <= MAX_TEXT, f"{w}: the text must be 1 to {MAX_TEXT} characters.")
        text.append({
            "id": uid(c.get("id"), "text"), "text": body, "at_f": _int(c, "at_f", w), "dur_f": _int(c, "dur_f", w, lo=1),
            "x": _num(c, "x", w, lo=0, hi=1, default=0.5), "y": _num(c, "y", w, lo=0, hi=1, default=0.5),
            "size": _int(c, "size", w, lo=8, hi=400, default=64), "color": _color(c, "color", w, "#FFFFFF"), "bold": bool(c.get("bold", True)),
            "anim_in": _anim(c, "anim_in", w), "anim_out": _anim(c, "anim_out", w), "font": _font_key(c, w),
        })

    cap_raw = raw.get("captions") or {}
    _need(isinstance(cap_raw, dict), "Captions must be an object.")
    words: list[dict] = []
    for word in cap_raw.get("words", []):
        _need(isinstance(word, dict) and isinstance(word.get("w"), str), "Every caption word needs its text.")
        s, e = word.get("s_ms"), word.get("e_ms")
        _need(isinstance(s, int) and isinstance(e, int) and not isinstance(s, bool) and 0 <= s <= e,
              "Every caption word needs a start and an end, with the start first.")
        words.append({"w": word["w"], "s_ms": s, "e_ms": e})
    _need(len(words) <= MAX_WORDS, "There are too many caption words.")
    cy = cap_raw.get("y")
    captions = {
        "enabled": bool(cap_raw.get("enabled", bool(words))),
        "accent": _color(cap_raw, "accent", "Captions", "#38bdf8"), "words": words,
        "size": _num(cap_raw, "size", "Captions", lo=0.5, hi=2.0, default=1.0),
        "color": _color(cap_raw, "color", "Captions", "#FFFFFF"),
        "outline": _num(cap_raw, "outline", "Captions", lo=0.0, hi=10.0, default=4.0),
        "shadow": _num(cap_raw, "shadow", "Captions", lo=0.0, hi=8.0, default=2.0),
        "box": bool(cap_raw.get("box", False)),
        "y": None if cy is None else _num(cap_raw, "y", "Captions", lo=0.02, hi=0.98),
        "per_line": _int(cap_raw, "per_line", "Captions", lo=0, hi=10, default=0),
        "upper": bool(cap_raw.get("upper", False)),
        "font": _font_key(cap_raw, "Captions"),
    }

    markers: list[dict] = []
    for m in raw.get("markers", []):
        _need(isinstance(m, dict), "A marker must be an object.")
        label = m.get("label", "")
        _need(isinstance(label, str) and len(label) <= 120, "A marker label can be up to 120 characters.")
        markers.append({"id": uid(m.get("id"), "marker"), "at_f": _int(m, "at_f", "Marker"), "label": label})
    _need(len(markers) <= MAX_MARKERS, f"A project can hold at most {MAX_MARKERS} markers.")

    shapes_raw = raw.get("shapes", [])
    _need(isinstance(shapes_raw, list), "shapes must be a list.")
    _need(len(shapes_raw) <= MAX_SHAPES, f"A project can hold at most {MAX_SHAPES} shapes.")
    shapes: list[dict] = []
    for s in shapes_raw:
        _need(isinstance(s, dict), "A shape must be an object.")
        w = f"Shape {s.get('id')}"
        item = {
            "id": uid(s.get("id"), "shape"), "at_f": _int(s, "at_f", w), "dur_f": _int(s, "dur_f", w, lo=1),
            "x": _num(s, "x", w, lo=0, hi=1, default=0.0), "y": _num(s, "y", w, lo=0, hi=1, default=0.0),
            "w": _num(s, "w", w, lo=0.01, hi=1, default=0.5), "h": _num(s, "h", w, lo=0.01, hi=1, default=0.1),
            "color": _color(s, "color", w, "#000000"), "opacity": _num(s, "opacity", w, lo=0.0, hi=1.0, default=0.6),
            "radius": _num(s, "radius", w, lo=0.0, hi=0.5, default=0.0),
        }
        picture = s.get("media_id")
        if picture is not None:
            _need(isinstance(picture, str) and bool(picture), f"{w}: the picture must name a library item.")
            item["media_id"] = picture
        _need(item["x"] + item["w"] <= 1.0001 and (picture is not None or item["y"] + item["h"] <= 1.0001), f"{w}: it is outside the picture.")
        shapes.append(item)

    fx_raw = raw.get("effects") or {}
    _need(isinstance(fx_raw, dict), "Effects must be an object.")
    bar_raw = fx_raw.get("progress_bar") or {}
    _need(isinstance(bar_raw, dict), "The progress bar must be an object.")
    effects = {"progress_bar": {
        "enabled": bool(bar_raw.get("enabled", False)), "color": _color(bar_raw, "color", "Progress bar", "#38bdf8"),
        "height_px": _int(bar_raw, "height_px", "Progress bar", lo=0, hi=64, default=0),
    }}

    bleeps_raw = raw.get("bleeps", [])
    _need(isinstance(bleeps_raw, list), "bleeps must be a list.")
    _need(len(bleeps_raw) <= MAX_BLEEPS, f"A project can hold at most {MAX_BLEEPS} bleeps.")
    bleeps: list[dict[str, int]] = []
    for b in bleeps_raw:
        _need(isinstance(b, dict), "A bleep must be an object.")
        s_ms, e_ms = b.get("s_ms"), b.get("e_ms")
        _need(isinstance(s_ms, int) and isinstance(e_ms, int) and not isinstance(s_ms, bool) and not isinstance(e_ms, bool)
              and 0 <= s_ms < e_ms, "A bleep needs a start and an end in milliseconds, with the start first.")
        bleeps.append({"s_ms": s_ms, "e_ms": e_ms})
    bleeps.sort(key=lambda x: (x["s_ms"], x["e_ms"]))

    tracks_raw = raw.get("tracks") or {}
    _need(isinstance(tracks_raw, dict), "Tracks must be an object.")
    tracks: dict[str, dict[str, bool]] = {}
    for name in TRACKS:
        state = tracks_raw.get(name) or {}
        _need(isinstance(state, dict), f"Track {name} must be an object.")
        tracks[name] = {"locked": bool(state.get("locked", False)), "hidden": bool(state.get("hidden", False))}

    out = {
        "schema_version": SCHEMA_VERSION, "fps": fps, "width": width, "height": height,
        "background": _color(raw, "background", "Canvas", "#000000"),
        "video": video, "audio": audio, "text": text, "captions": captions, "markers": markers, "tracks": tracks,
        "shapes": shapes, "effects": effects, "bleeps": bleeps,
    }
    given = _int(raw, "duration_f", "Project", default=0)
    out["duration_f"] = max(given, end_frame(out))
    return out


def to_job(edit: dict[str, Any], media: dict[str, dict[str, Any]], output: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """The worker's job for an edit list. `media` maps each media_id to {"url", "kind"}; clips become input keys m0, m1...
    Raises EditError when a clip names an item that was not resolved, or when there is nothing to draw."""
    # a hidden track is not drawn, and not heard
    hidden = {name for name, state in (edit.get("tracks") or {}).items() if state.get("hidden")}
    edit = {**edit, **{name: [] for name in hidden if name in TRACKS}}
    _need(edit["video"] or edit["audio"], "There is nothing in this project to render yet.")
    keys: dict[str, str] = {}
    inputs: dict[str, dict[str, Any]] = {}
    for mid in sorted(media_ids(edit)):
        found = media.get(mid)
        _need(found is not None, "A clip uses a file that is no longer in the library.")
        keys[mid] = f"m{len(keys)}"
        inputs[keys[mid]] = {"url": found["url"], "kind": found["kind"]}

    def clip(c: dict[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in c.items() if k != "media_id"}
        out["src"] = keys[c["media_id"]]
        return out

    worker_edit = {
        "version": 1, "fps": edit["fps"], "width": edit["width"], "height": edit["height"], "duration_f": edit["duration_f"],
        "background": edit["background"], "video": [clip(c) for c in edit["video"]], "audio": [clip(c) for c in edit["audio"]],
        "text": edit["text"], "captions": edit["captions"], "effects": edit.get("effects", {}),
        "bleeps": edit.get("bleeps", []),
        # a plain coloured box is a shape; one that carries a library item is a picture over the video
        "shapes": [s for s in edit.get("shapes", []) if not s.get("media_id")],
        "pictures": [
            {"id": s["id"], "src": keys[s["media_id"]], "at_f": s["at_f"], "dur_f": s["dur_f"], "x": s["x"], "y": s["y"], "w": s["w"],
             "opacity": s["opacity"]}
            for s in edit.get("shapes", []) if s.get("media_id")
        ],
    }
    return {"inputs": inputs, "edit": worker_edit, "output": output or {}}


def duplicate(edit: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(edit)


def used_ids(edit: dict[str, Any]) -> Iterable[str]:
    return media_ids(edit)
