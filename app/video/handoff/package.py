"""The send-to-editor package: one zip an editor can open in their own program. The timeline in three formats, the captions as SRT and VTT,
the markers, the transcript, and a manifest of the source files by their original names so the program can relink them. The media
themselves are not in the zip: they are the member's own files, and the manifest says which they are."""

from __future__ import annotations

import io
import json
import re
import zipfile
from typing import Any

from app.video.handoff import formats
from app.video.handoff.timeline import NOT_CARRIED, Timeline

README = """Hand-off package for "{title}"
================================

Open the timeline in your editing program:
  timeline_premiere.xml    Premiere Pro and DaVinci Resolve (File > Import). Also reads in Final Cut Pro 7.
  timeline_finalcut.fcpxml Final Cut Pro (File > Import > XML).
  timeline.edl             Any program that reads a CMX 3600 EDL. One video track, audio tracks A, A2 and so on.
  timeline.otio            OpenTimelineIO, for programs and scripts that read it.

Also here:
  captions.srt, captions.vtt   The captions as subtitle files.
  markers.csv                  Your markers with timecodes. They are inside the timeline files too.
  transcript.txt               The words of the video, if it was written out.
  manifest.json                The source files used, by their original names.

Put the source files named in manifest.json in one folder and point your program at it when it asks where the media went.
The sequence starts at 01:00:00:00 and runs at {fps} frames a second, {width} x {height}.

Not carried across, because a plain timeline has no place for it: {not_carried}.
"""


def build_zip(tl: Timeline, words: list[dict[str, Any]], transcript_text: str = "") -> bytes:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", tl.title).strip("_") or "video"
    sources: dict[str, dict[str, Any]] = {}
    for e in tl.events:
        row = sources.setdefault(e.media_id, {"media_id": e.media_id, "file": e.name, "used_as": set(), "frames_used": 0})
        row["used_as"].add(e.kind)
        row["frames_used"] = max(row["frames_used"], e.src_in_f + int(round(e.dur_f * e.speed)))
    manifest = {
        "title": tl.title, "width": tl.width, "height": tl.height, "frame_rate": f"{tl.fps_num}/{tl.fps_den}", "duration_frames": tl.duration_f,
        "sources": [{**r, "used_as": sorted(r["used_as"])} for r in sources.values()], "not_carried": list(NOT_CARRIED),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{safe}/README.txt", README.format(title=tl.title, fps=round(tl.fps_num / tl.fps_den, 3), width=tl.width, height=tl.height,
                                                        not_carried="; ".join(NOT_CARRIED)))
        z.writestr(f"{safe}/timeline_premiere.xml", formats.to_fcp7_xml(tl))
        z.writestr(f"{safe}/timeline_finalcut.fcpxml", formats.to_fcpxml(tl))
        z.writestr(f"{safe}/timeline.edl", formats.to_cmx3600(tl))
        from app.video.handoff import importer

        z.writestr(f"{safe}/timeline.otio", importer.to_otio(tl))
        z.writestr(f"{safe}/markers.csv", formats.to_markers_csv(tl))
        if words:
            z.writestr(f"{safe}/captions.srt", formats.to_srt(words))
            z.writestr(f"{safe}/captions.vtt", formats.to_vtt(words))
        if transcript_text.strip():
            z.writestr(f"{safe}/transcript.txt", transcript_text.strip() + "\n")
        z.writestr(f"{safe}/manifest.json", json.dumps(manifest, indent=2))
    return buf.getvalue()
