"""Reading a timeline made in another program back in: a CMX 3600 EDL, Final Cut Pro 7 XML (Premiere and Resolve write it too) or an
OpenTimelineIO file. What comes in is clips with their place, source in point, speed and the file name they used, plus markers. Names
are matched to the member's library by original file name; a clip with no match is listed, never guessed. Nothing is changed until the
member applies it, and applying is one undoable version of the project."""

from __future__ import annotations

import json
import re
from typing import Any
from xml.etree import ElementTree as ET

from app.video.handoff.timeline import Event, Timeline, original_name

START_HOUR = 1
MAX_EVENTS = 2000


class ImportError_(ValueError):
    """A file that cannot be read, with a plain reason for the member."""


def _tc_to_frames(tc: str, timebase: int) -> int:
    parts = re.split(r"[:;]", tc.strip())
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        raise ImportError_("A time in this file is not in the HH:MM:SS:FF form.")
    h, m, s, f = (int(p) for p in parts)
    return ((h * 60 + m) * 60 + s) * timebase + f


# ── EDL ──────────────────────────────────────────────────────────────────────────

_EVENT = re.compile(r"^(\d{3,6})\s+(\S+)\s+(\S+)\s+(\S+)\s+(?:\S+\s+)?(\d\d[:;]\d\d[:;]\d\d[:;]\d\d)\s+(\d\d[:;]\d\d[:;]\d\d[:;]\d\d)\s+(\d\d[:;]\d\d[:;]\d\d[:;]\d\d)\s+(\d\d[:;]\d\d[:;]\d\d[:;]\d\d)\s*$")


def parse_edl(text: str, timebase: int) -> Timeline:
    title, events, markers, speed_for = "Imported timeline", [], [], {}
    last: dict[str, Any] = {}
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("TITLE:"):
            title = line[6:].strip() or title
            continue
        if line.startswith("M2"):
            nums = re.findall(r"(\d+(?:\.\d+)?)\s+(?=\d\d[:;])", line)
            if nums and last:
                last["speed"] = max(0.01, float(nums[0]) / timebase)
            continue
        m = _EVENT.match(line)
        if m:
            _n, _reel, chan, _trans, s_in, s_out, r_in, r_out = m.groups()
            at, end = _tc_to_frames(r_in, timebase), _tc_to_frames(r_out, timebase)
            src_in, src_out = _tc_to_frames(s_in, timebase), _tc_to_frames(s_out, timebase)
            kind = "video" if chan.upper().startswith("V") else "audio"
            track = int(chan[1:]) if kind == "audio" and chan[1:].isdigit() else 1
            dur = max(1, end - at)
            speed = (src_out - src_in) / dur if dur and src_out > src_in else 1.0
            last = {"kind": kind, "track": track, "at": at, "dur": dur, "src_in": src_in, "speed": speed or 1.0, "name": ""}
            events.append(last)
        elif line.startswith("* FROM CLIP NAME:") and last:
            last["name"] = line.split(":", 1)[1].strip()
        elif line.startswith("* LOC:"):
            parts = line[6:].split(None, 2)
            if len(parts) >= 1:
                try:
                    markers.append({"at_f": _tc_to_frames(parts[0], timebase), "label": parts[2] if len(parts) > 2 else ""})
                except ImportError_:
                    pass
        if len(events) > MAX_EVENTS:
            raise ImportError_("This timeline has more clips than a project can hold.")
    if not events:
        raise ImportError_("No clips were found in this edit list.")
    base = min(e["at"] for e in events)
    base = START_HOUR * 3600 * timebase if base >= START_HOUR * 3600 * timebase else 0
    tl = Timeline(title=title, width=1920, height=1080, fps_num=timebase, fps_den=1, duration_f=0)
    for e in events:
        tl.events.append(Event(e["kind"], e["track"], e["at"] - base, e["dur"], e["src_in"], round(e["speed"], 4), "", e["name"] or "clip", role="" if e["kind"] == "video" else "clip"))
    tl.markers = [{"at_f": max(0, m["at_f"] - base), "label": m["label"]} for m in markers]
    tl.duration_f = max(e.at_f + e.dur_f for e in tl.events)
    return tl


# ── Final Cut Pro 7 XML ──────────────────────────────────────────────────────────

def parse_fcp7(text: str) -> Timeline:
    if re.search(r"<!ENTITY|<!DOCTYPE[^>]*\[", text):
        # a file that defines its own entities can be made to grow without limit when it is read, and a timeline never needs one
        raise ImportError_("This file uses features that a timeline file does not need, so it was not read.")
    try:
        root = ET.fromstring(re.sub(r"<!DOCTYPE[^>]*>", "", text, count=1))
    except ET.ParseError as exc:
        raise ImportError_("This file is not valid Final Cut Pro XML.") from exc
    seq = root.find("sequence")
    if seq is None:
        raise ImportError_("No sequence was found in this file.")
    tb = int(float(seq.findtext("rate/timebase") or 30))
    ntsc = (seq.findtext("rate/ntsc") or "FALSE").upper() == "TRUE"
    tl = Timeline(title=seq.findtext("name") or "Imported timeline", width=int(seq.findtext("media/video/format/samplecharacteristics/width") or 1920),
                  height=int(seq.findtext("media/video/format/samplecharacteristics/height") or 1080), fps_num=tb * 1000 if ntsc else tb,
                  fps_den=1001 if ntsc else 1, duration_f=int(float(seq.findtext("duration") or 0)))
    names: dict[str, str] = {}
    for f in root.iter("file"):
        if f.get("id") and f.findtext("name"):
            names[f.get("id")] = f.findtext("name")
    for kind in ("video", "audio"):
        for ti, track in enumerate(seq.findall(f"media/{kind}/track"), 1):
            for item in track.findall("clipitem"):
                start, end = int(float(item.findtext("start") or 0)), int(float(item.findtext("end") or 0))
                if start < 0 or end <= start:
                    continue
                fid = (item.find("file").get("id") if item.find("file") is not None else None) or ""
                name = item.findtext("file/name") or names.get(fid) or item.findtext("name") or "clip"
                src_in, src_out = int(float(item.findtext("in") or 0)), int(float(item.findtext("out") or 0))
                speed = 1.0
                for p in item.iter("parameter"):
                    if p.findtext("parameterid") == "speed":
                        speed = max(0.01, float(p.findtext("value") or 100) / 100)
                if speed == 1.0 and src_out > src_in and (end - start):
                    speed = round((src_out - src_in) / (end - start), 4)
                tl.events.append(Event(kind, ti, start, end - start, src_in, speed, "", name, role="" if kind == "video" else "clip"))
    for m in seq.findall("marker"):
        tl.markers.append({"at_f": int(float(m.findtext("in") or 0)), "label": m.findtext("name") or ""})
    if not tl.events:
        raise ImportError_("No clips were found in this file.")
    if len(tl.events) > MAX_EVENTS:
        raise ImportError_("This timeline has more clips than a project can hold.")
    tl.duration_f = max([tl.duration_f] + [e.at_f + e.dur_f for e in tl.events])
    return tl


# ── OpenTimelineIO (JSON) ────────────────────────────────────────────────────────

def _rt(t: dict | None, rate: float) -> int:
    if not t:
        return 0
    return int(round(float(t.get("value", 0)) * (rate / float(t.get("rate", rate) or rate))))


def to_otio(tl: Timeline) -> str:
    rate = tl.fps_num / tl.fps_den

    def rt(frames: int) -> dict:
        return {"OTIO_SCHEMA": "RationalTime.1", "rate": rate, "value": float(frames)}

    def track(kind: str, n: int) -> dict:
        children = []
        cursor = 0
        for e in sorted([e for e in tl.events if e.kind == kind and e.track == n], key=lambda e: e.at_f):
            if e.at_f > cursor:
                children.append({"OTIO_SCHEMA": "Gap.1", "name": "", "source_range": {"OTIO_SCHEMA": "TimeRange.1", "duration": rt(e.at_f - cursor), "start_time": rt(0)}})
            children.append({
                "OTIO_SCHEMA": "Clip.1", "name": e.name, "metadata": {"ekavo": {"speed": e.speed, "role": e.role}},
                "source_range": {"OTIO_SCHEMA": "TimeRange.1", "duration": rt(e.dur_f), "start_time": rt(e.src_in_f)},
                "media_reference": {"OTIO_SCHEMA": "ExternalReference.1", "target_url": e.name, "name": e.name},
            })
            cursor = e.at_f + e.dur_f
        return {"OTIO_SCHEMA": "Track.1", "name": f"{kind[0].upper()}{n}", "kind": "Video" if kind == "video" else "Audio", "children": children}

    tracks = [track("video", n) for n in sorted({e.track for e in tl.events if e.kind == "video"})]
    tracks += [track("audio", n) for n in sorted({e.track for e in tl.events if e.kind == "audio"})]
    doc = {
        "OTIO_SCHEMA": "Timeline.1", "name": tl.title,
        "global_start_time": rt(0),
        "metadata": {"ekavo": {"width": tl.width, "height": tl.height, "markers": tl.markers}},
        "tracks": {"OTIO_SCHEMA": "Stack.1", "name": "tracks", "children": tracks},
    }
    return json.dumps(doc, indent=2)


def parse_otio(text: str) -> Timeline:
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ImportError_("This file is not valid OpenTimelineIO.") from exc
    if not str(doc.get("OTIO_SCHEMA", "")).startswith("Timeline"):
        raise ImportError_("This is not an OpenTimelineIO timeline.")
    meta = (doc.get("metadata") or {}).get("ekavo") or {}
    first_rate = 30.0
    for tr in (doc.get("tracks") or {}).get("children", []):
        for c in tr.get("children", []):
            sr = c.get("source_range") or {}
            if (sr.get("duration") or {}).get("rate"):
                first_rate = float(sr["duration"]["rate"])
                break
        else:
            continue
        break
    ntsc = abs(first_rate - round(first_rate)) > 0.001
    tl = Timeline(title=doc.get("name") or "Imported timeline", width=int(meta.get("width") or 1920), height=int(meta.get("height") or 1080),
                  fps_num=int(round(first_rate)) * 1000 if ntsc else int(round(first_rate)), fps_den=1001 if ntsc else 1, duration_f=0)
    count = {"video": 0, "audio": 0}
    for tr in (doc.get("tracks") or {}).get("children", []):
        kind = "video" if str(tr.get("kind", "")).lower() == "video" else "audio"
        count[kind] += 1
        cursor = 0
        for c in tr.get("children", []):
            sr = c.get("source_range") or {}
            dur = _rt(sr.get("duration"), first_rate)
            if str(c.get("OTIO_SCHEMA", "")).startswith("Gap"):
                cursor += dur
                continue
            if not str(c.get("OTIO_SCHEMA", "")).startswith("Clip") or dur <= 0:
                cursor += dur
                continue
            ref = c.get("media_reference") or {}
            name = str(c.get("name") or ref.get("name") or ref.get("target_url") or "clip").rsplit("/", 1)[-1]
            speed = float(((c.get("metadata") or {}).get("ekavo") or {}).get("speed", 1.0) or 1.0)
            tl.events.append(Event(kind, count[kind], cursor, dur, _rt(sr.get("start_time"), first_rate), speed, "", name, role="" if kind == "video" else "clip"))
            cursor += dur
    tl.markers = [{"at_f": int(m.get("at_f", 0)), "label": str(m.get("label", ""))} for m in meta.get("markers", []) if isinstance(m, dict)]
    if not tl.events:
        raise ImportError_("No clips were found in this file.")
    if len(tl.events) > MAX_EVENTS:
        raise ImportError_("This timeline has more clips than a project can hold.")
    tl.duration_f = max(e.at_f + e.dur_f for e in tl.events)
    return tl


def parse(filename: str, text: str, timebase: int) -> Timeline:
    """Read an uploaded timeline by what it looks like, not only its extension."""
    name = (filename or "").lower()
    head = text.lstrip()[:200]
    if name.endswith((".otio", ".json")) or head.startswith("{"):
        return parse_otio(text)
    if name.endswith(".xml") or head.startswith("<"):
        if "<fcpxml" in text[:400]:
            raise ImportError_("This is a Final Cut Pro X file. Export Final Cut Pro 7 XML (the Premiere format), an edit list, or OpenTimelineIO instead.")
        return parse_fcp7(text)
    return parse_edl(text, timebase)


# ── onto a project ───────────────────────────────────────────────────────────────

def match_clips(tl: Timeline, library: dict[str, dict]) -> tuple[dict[str, str], list[str]]:
    """({file name: media id}, [names with no match]). `library` maps media id to its record. A name matches a library file with the
    same original name, ignoring case; two library files with one name are not guessed between, so that name is left unmatched."""
    by_name: dict[str, list[str]] = {}
    for mid, rec in library.items():
        by_name.setdefault(original_name(rec, mid[:8]).lower(), []).append(mid)
    matched: dict[str, str] = {}
    missing: list[str] = []
    for name in dict.fromkeys(e.name for e in tl.events):
        ids = by_name.get(name.lower(), [])
        if len(ids) == 1:
            matched[name] = ids[0]
        else:
            missing.append(name)
    return matched, missing


def to_edit_parts(tl: Timeline, matched: dict[str, str]) -> dict[str, Any]:
    """The video clips, audio clips and markers for a project's edit list from the matched events. Clips with no match are left out."""
    video, audio = [], []
    sounds = [e for e in tl.events if e.kind == "audio" and e.name in matched]
    used: set[int] = set()
    for i, e in enumerate([e for e in tl.events if e.kind == "video" and e.name in matched], 1):
        has_sound = False
        for j, s in enumerate(sounds):
            if j not in used and s.name == e.name and s.at_f == e.at_f and s.dur_f == e.dur_f:
                used.add(j)
                has_sound = True
                break
        video.append({"id": f"v{i}", "media_id": matched[e.name], "at_f": e.at_f, "dur_f": e.dur_f, "src_in_f": e.src_in_f, "speed": e.speed,
                      "fit": "cover", "fade_in_f": 0, "fade_out_f": 0, "audio": has_sound, "gain_db": 0.0, "motion": False})
    for j, s in enumerate(sounds):
        if j in used:
            continue
        audio.append({"id": f"a{len(audio) + 1}", "media_id": matched[s.name], "at_f": s.at_f, "dur_f": s.dur_f, "src_in_f": s.src_in_f, "speed": s.speed,
                      "gain_db": 0.0, "role": "music", "fade_in_f": 0, "fade_out_f": 0, "duck": False})
    return {"video": video, "audio": audio, "markers": [{"id": f"m{i}", "at_f": m["at_f"], "label": m["label"][:80]} for i, m in enumerate(tl.markers, 1)],
            "duration_f": max([c["at_f"] + c["dur_f"] for c in video + audio] + [0])}
