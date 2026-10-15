"""The editor's edit list flattened into what editing programs understand: clips on numbered video and audio tracks, with their place on
the timeline, the part of the source they use, and their speed. Everything the exporters write comes from this one structure, so the
three formats cannot disagree with each other.

What carries over: video clips, the sound of those clips, audio clips (voice, music, effects) and markers. What does not: looks, crops,
fades, text, captions, shapes, pictures and the progress bar. Those are the editor's own and have no equal in a plain timeline; the
captions go alongside as SRT and VTT files, and the package says so."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import unquote, urlparse

NOT_CARRIED = ("looks and crops", "fades", "text and captions on the picture (the captions come as SRT and VTT files)", "shapes and pictures",
               "the progress bar")


@dataclass
class Event:
    kind: str                 # "video" | "audio"
    track: int                # 1-based, per kind
    at_f: int
    dur_f: int
    src_in_f: int
    speed: float
    media_id: str
    name: str                 # the file's original name, so the editor's program can relink it
    role: str = ""            # for audio: voice | music | effect, or "clip" for the sound of a video clip
    gain_db: float = 0.0


@dataclass
class Timeline:
    title: str
    width: int
    height: int
    fps_num: int
    fps_den: int
    duration_f: int
    events: list[Event] = field(default_factory=list)
    markers: list[dict[str, Any]] = field(default_factory=list)

    @property
    def timebase(self) -> int:
        """The whole-number frame rate editing programs count timecode in (24, 25, 30, 60), with NTSC rates rounded up."""
        return max(1, int(round(self.fps_num / self.fps_den)))

    @property
    def ntsc(self) -> bool:
        return self.fps_den == 1001


_UPLOAD_PREFIX = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}_")


def original_name(media: Optional[dict], fallback: str) -> str:
    """The name the member's file had when it was uploaded. Our storage names an upload `<upload id>_<original name>`, so the prefix is
    taken off. Falls back to a plain name from the id when the address is unusable."""
    url = str((media or {}).get("url") or "")
    base = unquote(urlparse(url).path.rsplit("/", 1)[-1]) if url else ""
    base = _UPLOAD_PREFIX.sub("", base).strip()
    return base or f"{fallback}.mp4"


def _lanes(items: list[tuple[int, int, Any]]) -> list[tuple[int, Any]]:
    """Give each (start, end, payload) the lowest track number where it overlaps nothing already there. Returns (track, payload)."""
    ends: list[int] = []
    out: list[tuple[int, Any]] = []
    for start, end, payload in sorted(items, key=lambda x: (x[0], x[1])):
        for i, last in enumerate(ends):
            if start >= last:
                ends[i] = end
                out.append((i + 1, payload))
                break
        else:
            ends.append(end)
            out.append((len(ends), payload))
    return out


def build(title: str, edit: dict[str, Any], media: dict[str, dict[str, Any]]) -> Timeline:
    """The timeline for one project. `media` maps media id to its record (for the original file names)."""
    fps = edit.get("fps") or {"num": 30, "den": 1}
    tl = Timeline(title=(title or "Untitled video").strip() or "Untitled video", width=int(edit.get("width") or 1920), height=int(edit.get("height") or 1080),
                  fps_num=int(fps.get("num") or 30), fps_den=int(fps.get("den") or 1), duration_f=int(edit.get("duration_f") or 0))
    hidden = set((edit.get("tracks") or {}).get("hide") or [])

    clips = [c for c in edit.get("video") or [] if c.get("media_id")] if "video" not in hidden else []
    for track, c in _lanes([(int(c["at_f"]), int(c["at_f"]) + int(c["dur_f"]), c) for c in clips]):
        name = original_name(media.get(c["media_id"]), c["media_id"][:8])
        tl.events.append(Event("video", track, int(c["at_f"]), int(c["dur_f"]), int(c.get("src_in_f", 0)), float(c.get("speed", 1.0) or 1.0), c["media_id"], name))
    # the sound that belongs to a video clip travels with it, on its own audio track
    with_sound = [c for c in clips if c.get("audio")]
    sound_lanes = _lanes([(int(c["at_f"]), int(c["at_f"]) + int(c["dur_f"]), c) for c in with_sound])
    next_audio = 0
    for track, c in sound_lanes:
        name = original_name(media.get(c["media_id"]), c["media_id"][:8])
        tl.events.append(Event("audio", track, int(c["at_f"]), int(c["dur_f"]), int(c.get("src_in_f", 0)), float(c.get("speed", 1.0) or 1.0),
                               c["media_id"], name, role="clip", gain_db=float(c.get("gain_db", 0.0) or 0.0)))
        next_audio = max(next_audio, track)
    audio = [a for a in edit.get("audio") or [] if a.get("media_id")] if "audio" not in hidden else []
    for track, a in _lanes([(int(a["at_f"]), int(a["at_f"]) + int(a["dur_f"]), a) for a in audio]):
        name = original_name(media.get(a["media_id"]), a["media_id"][:8])
        tl.events.append(Event("audio", next_audio + track, int(a["at_f"]), int(a["dur_f"]), int(a.get("src_in_f", 0)), float(a.get("speed", 1.0) or 1.0),
                               a["media_id"], name, role=str(a.get("role") or ""), gain_db=float(a.get("gain_db", 0.0) or 0.0)))
    tl.markers = [{"at_f": int(m["at_f"]), "label": str(m.get("label") or "")} for m in edit.get("markers") or []]
    end = max([e.at_f + e.dur_f for e in tl.events] + [tl.duration_f])
    tl.duration_f = end
    return tl


def timecode(frames: int, timebase: int, start_hour: int = 0) -> str:
    """HH:MM:SS:FF counted at the whole-number frame rate, non-drop. `start_hour` shifts the start (editing programs start at 01:00:00:00)."""
    frames = max(0, int(frames)) + start_hour * 3600 * timebase
    ff = frames % timebase
    seconds = frames // timebase
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}:{ff:02d}"
