"""Writers for the open timeline formats editing programs read: CMX 3600 EDL, Final Cut Pro 7 XML (which Premiere Pro and DaVinci Resolve
read too) and Final Cut Pro X XML. Each takes the one Timeline and returns text. Time is counted in whole frames of the project's frame
rate; NTSC rates (29.97, 23.976) are flagged where the format has a place for it and counted non-drop."""

from __future__ import annotations

import re
from typing import Optional
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from app.video.handoff.timeline import Event, Timeline, timecode

START_HOUR = 1   # editing programs start a sequence at 01:00:00:00


def _src_out(e: Event) -> int:
    """The last source frame used, counted in the source's own frames: a clip at 2x speed uses twice as much source."""
    return e.src_in_f + max(1, int(round(e.dur_f * e.speed)))


# ── CMX 3600 EDL ─────────────────────────────────────────────────────────────────

def to_cmx3600(tl: Timeline) -> str:
    tb = tl.timebase
    lines = [f"TITLE: {tl.title}", "FCM: NON-DROP FRAME", ""]
    n = 0
    ordered = sorted(tl.events, key=lambda e: (e.kind != "video", e.track, e.at_f))
    for e in ordered:
        n += 1
        chan = "V" if e.kind == "video" else ("A" if e.track == 1 else f"A{e.track}")
        rec_in, rec_out = timecode(e.at_f, tb, START_HOUR), timecode(e.at_f + e.dur_f, tb, START_HOUR)
        src_in, src_out = timecode(e.src_in_f, tb), timecode(_src_out(e), tb)
        lines.append(f"{n:03d}  AX       {chan:<5} C        {src_in} {src_out} {rec_in} {rec_out}")
        if abs(e.speed - 1.0) > 1e-6:
            lines.append(f"M2   AX       {tb * e.speed:6.1f}                {src_in}")
        lines.append(f"* FROM CLIP NAME: {e.name}")
    for m in tl.markers:
        lines.append(f"* LOC: {timecode(m['at_f'], tb, START_HOUR)} RED     {m['label']}")
    return "\n".join(lines) + "\n"


# ── Final Cut Pro 7 XML (xmeml) ──────────────────────────────────────────────────

def _el(parent: ET.Element, tag: str, text: Optional[object] = None, **attrs: str) -> ET.Element:
    e = ET.SubElement(parent, tag, {k: str(v) for k, v in attrs.items()})
    if text is not None:
        e.text = str(text)
    return e


def _rate(parent: ET.Element, tl: Timeline) -> None:
    r = _el(parent, "rate")
    _el(r, "timebase", tl.timebase)
    _el(r, "ntsc", "TRUE" if tl.ntsc else "FALSE")


def to_fcp7_xml(tl: Timeline) -> str:
    root = ET.Element("xmeml", version="4")
    seq = _el(root, "sequence", id="sequence-1")
    _el(seq, "name", tl.title)
    _el(seq, "duration", tl.duration_f)
    _rate(seq, tl)
    tc = _el(seq, "timecode")
    _rate(tc, tl)
    _el(tc, "string", timecode(0, tl.timebase, START_HOUR))
    _el(tc, "frame", START_HOUR * 3600 * tl.timebase)
    _el(tc, "displayformat", "NDF")
    for m in tl.markers:
        mk = _el(seq, "marker")
        _el(mk, "name", m["label"] or "Marker")
        _el(mk, "in", m["at_f"])
        _el(mk, "out", -1)
    media = _el(seq, "media")
    files: dict[str, str] = {}
    written: set[str] = set()
    for kind in ("video", "audio"):
        section = _el(media, kind)
        if kind == "video":
            fmt = _el(section, "format")
            sc = _el(fmt, "samplecharacteristics")
            _rate(sc, tl)
            _el(sc, "width", tl.width)
            _el(sc, "height", tl.height)
            _el(sc, "pixelaspectratio", "square")
        tracks = sorted({e.track for e in tl.events if e.kind == kind})
        for t in tracks:
            track = _el(section, "track")
            for i, e in enumerate([e for e in tl.events if e.kind == kind and e.track == t], 1):
                item = _el(track, "clipitem", id=f"{kind}-{t}-{i}")
                _el(item, "name", e.name)
                _el(item, "duration", e.dur_f)
                _rate(item, tl)
                _el(item, "start", e.at_f)
                _el(item, "end", e.at_f + e.dur_f)
                _el(item, "in", e.src_in_f)
                _el(item, "out", _src_out(e))
                fid = files.setdefault(e.media_id, f"file-{len(files) + 1}")
                if fid not in written:
                    written.add(fid)
                    f = _el(item, "file", id=fid)
                    _el(f, "name", e.name)
                    _el(f, "pathurl", "file://localhost/" + e.name.replace(" ", "%20"))
                    _rate(f, tl)
                else:
                    _el(item, "file", id=fid)
                if abs(e.speed - 1.0) > 1e-6:
                    flt = _el(item, "filter")
                    eff = _el(flt, "effect")
                    _el(eff, "name", "Time Remap")
                    _el(eff, "effectid", "timeremap")
                    _el(eff, "effectcategory", "motion")
                    _el(eff, "effecttype", "motion")
                    _el(eff, "mediatype", "video")
                    p = _el(eff, "parameter", authoringApp="")
                    _el(p, "parameterid", "speed")
                    _el(p, "name", "speed")
                    _el(p, "value", round(e.speed * 100, 3))
                if kind == "audio":
                    src = _el(item, "sourcetrack")
                    _el(src, "mediatype", "audio")
                    _el(src, "trackindex", 1)
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n' + ET.tostring(root, encoding="unicode") + "\n"


# ── Final Cut Pro X XML (fcpxml) ─────────────────────────────────────────────────

def _t(frames: int, tl: Timeline) -> str:
    """A time as FCPXML writes it: a fraction of a second, so no frame is ever rounded."""
    return f"{max(0, int(frames)) * tl.fps_den}/{tl.fps_num}s"


def to_fcpxml(tl: Timeline) -> str:
    fr = f"{tl.fps_den}/{tl.fps_num}s"
    media_ids: dict[str, str] = {}
    names: dict[str, str] = {}
    longest: dict[str, int] = {}
    for e in tl.events:
        media_ids.setdefault(e.media_id, f"r{len(media_ids) + 2}")
        names[e.media_id] = e.name
        longest[e.media_id] = max(longest.get(e.media_id, 0), _src_out(e))
    out = ['<?xml version="1.0" encoding="UTF-8"?>', "<!DOCTYPE fcpxml>", '<fcpxml version="1.10">', "  <resources>",
           f'    <format id="r1" name="FFVideoFormatCustom" frameDuration="{fr}" width="{tl.width}" height="{tl.height}"/>']
    for mid, rid in media_ids.items():
        has_video = any(e.kind == "video" and e.media_id == mid for e in tl.events)
        out.append(f'    <asset id="{rid}" name="{escape(names[mid], {chr(34): "&quot;"})}" start="0s" duration="{_t(longest[mid], tl)}" '
                   f'hasVideo="{1 if has_video else 0}" hasAudio="1" format="r1">')
        out.append(f'      <media-rep kind="original-media" src="file:///{escape(names[mid].replace(" ", "%20"))}"/>')
        out.append("    </asset>")
    out.append("  </resources>")
    out.append("  <library>")
    out.append(f'    <event name="{escape(tl.title, {chr(34): "&quot;"})}">')
    out.append(f'      <project name="{escape(tl.title, {chr(34): "&quot;"})}">')
    out.append(f'        <sequence format="r1" duration="{_t(tl.duration_f, tl)}" tcStart="{_t(START_HOUR * 3600 * tl.timebase, tl)}" tcFormat="NDF">')
    out.append("          <spine>")
    out.append(f'            <gap name="Timeline" offset="0s" start="0s" duration="{_t(tl.duration_f, tl)}">')
    for e in sorted(tl.events, key=lambda e: (e.kind != "video", e.track, e.at_f)):
        lane = e.track if e.kind == "video" else -e.track
        speed = "" if abs(e.speed - 1.0) <= 1e-6 else f' <!-- speed {e.speed:g}x -->'
        out.append(f'              <asset-clip ref="{media_ids[e.media_id]}" lane="{lane}" name="{escape(e.name, {chr(34): "&quot;"})}" '
                   f'offset="{_t(e.at_f, tl)}" start="{_t(e.src_in_f, tl)}" duration="{_t(e.dur_f, tl)}"'
                   f'{"" if e.kind == "video" else " audioRole=" + chr(34) + (e.role or "dialogue") + chr(34)}/>{speed}')
    for m in tl.markers:
        out.append(f'              <marker start="{_t(m["at_f"], tl)}" duration="{fr}" value="{escape(m["label"] or "Marker", {chr(34): "&quot;"})}"/>')
    out.append("            </gap>")
    out.append("          </spine>")
    out.append("        </sequence>")
    out.append("      </project>")
    out.append("    </event>")
    out.append("  </library>")
    out.append("</fcpxml>")
    return "\n".join(out) + "\n"


# ── marker list and caption files ────────────────────────────────────────────────

def to_markers_csv(tl: Timeline) -> str:
    rows = ["timecode,frame,label"]
    for m in sorted(tl.markers, key=lambda m: m["at_f"]):
        label = re.sub(r'"', '""', m["label"] or "")
        rows.append(f'{timecode(m["at_f"], tl.timebase, START_HOUR)},{m["at_f"]},"{label}"')
    return "\n".join(rows) + "\n"


def _cue_time(ms: int, sep: str) -> str:
    ms = max(0, int(ms))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d}{sep}{ms % 1000:03d}"


def caption_cues(words: list[dict], max_words: int = 7, max_ms: int = 3500) -> list[tuple[int, int, str]]:
    """Group the caption words into readable lines: at most `max_words` words and `max_ms` long, ended early at a full stop."""
    cues: list[tuple[int, int, str]] = []
    cur: list[dict] = []
    for w in words:
        if cur and (len(cur) >= max_words or w["e_ms"] - cur[0]["s_ms"] > max_ms):
            cues.append((cur[0]["s_ms"], cur[-1]["e_ms"], " ".join(x["w"] for x in cur)))
            cur = []
        cur.append(w)
        if str(w["w"]).rstrip().endswith((".", "?", "!")):
            cues.append((cur[0]["s_ms"], cur[-1]["e_ms"], " ".join(x["w"] for x in cur)))
            cur = []
    if cur:
        cues.append((cur[0]["s_ms"], cur[-1]["e_ms"], " ".join(x["w"] for x in cur)))
    return cues


def to_srt(words: list[dict]) -> str:
    return "\n".join(f"{i}\n{_cue_time(a, ',')} --> {_cue_time(b, ',')}\n{t}\n" for i, (a, b, t) in enumerate(caption_cues(words), 1))


def to_vtt(words: list[dict]) -> str:
    body = "\n".join(f"{_cue_time(a, '.')} --> {_cue_time(b, '.')}\n{t}\n" for a, b, t in caption_cues(words))
    return "WEBVTT\n\n" + body
