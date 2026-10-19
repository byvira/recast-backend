"""Reads what a video file is, without decoding it.

ffmpeg is asked to open the file and print its description; only the header is read (over a ranged request when the file
is a link), so a large file costs no memory and almost no time on the backend. The result is what ingest decides from:
frame rate, turned phones, HDR, audio layout. The worker does the heavy work (clean working copies) when it renders.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from typing import Any, Optional

import httpx
import imageio_ffmpeg

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 60
#: A stored video up to this size is fetched by the backend itself and read from disk. Larger ones are read over the link by ffmpeg.
FETCH_CAP_BYTES = 300 * 1024 * 1024
MAX_SOURCE_HOURS = 24
HDR_MARKERS = ("smpte2084", "arib-std-b67")

_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO = re.compile(r"Stream #\d+:\d+[^:]*: Video: (?P<codec>[\w]+)[^,]*, (?P<pix>[\w]+)(?:\((?P<color>[^)]*)\))?.*?, (?P<w>\d{2,5})x(?P<h>\d{2,5})")
_AUDIO = re.compile(r"Stream #\d+:\d+[^:]*: Audio: (?P<codec>[\w]+)[^,]*, (?P<rate>\d+) Hz, (?P<layout>[^,]+)")
_FPS = re.compile(r"([\d.]+) fps")
_TBR = re.compile(r"([\d.]+)(k?) tbr")
_ROTATION = re.compile(r"rotation of (-?[\d.]+) degrees")
_ROTATE_TAG = re.compile(r"rotate\s*:\s*(-?\d+)")
# what ffmpeg prints when it could not open the file at all (a network or storage problem), which is not the same as a file with no picture
_OPEN_FAILED = (
    "Server returned", "Connection refused", "Connection timed out", "Operation timed out", "Failed to resolve", "Name or service not known",
    "Temporary failure", "Protocol not found", "Input/output error", "Error in the pull function", "Cannot open connection", "TLS", "tls @",
    "SSL", "HTTP error", "Could not resolve", "No such file or directory", "Error opening input", "not on whitelist",
)


class ProbeError(ValueError):
    """The file could not be read as a video. The message is safe to show the member."""


def _tbr(text: str) -> Optional[float]:
    m = _TBR.search(text)
    if not m:
        return None
    return float(m.group(1)) * (1000 if m.group(2) else 1)


def parse(report: str) -> dict[str, Any]:
    """The facts in ffmpeg's description of a file. Raises ProbeError when it is not a readable video."""
    if "Invalid data found" in report or "moov atom not found" in report:
        raise ProbeError("This file is damaged or incomplete, so it can't be used. Try exporting or uploading it again.")
    d = _DURATION.search(report)
    duration = (int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3))) if d else 0.0
    video_line = next((ln for ln in report.splitlines() if "Video:" in ln and "attached pic" not in ln), "")
    v = _VIDEO.search(video_line)
    if not v:
        if not video_line and any(marker in report for marker in _OPEN_FAILED):
            logger.warning("The video could not be opened to be read: %s", report.strip()[-500:])
            raise ProbeError("The video was uploaded, but it could not be opened to check it. Please try again in a moment.")
        logger.warning("No picture found in the file; ffmpeg said: %s", report.strip()[-500:])
        raise ProbeError("This file has no picture in it, so it can't be used as a video.")
    if duration <= 0:
        raise ProbeError("This file's length could not be read, so it can't be used.")
    if duration > MAX_SOURCE_HOURS * 3600:
        raise ProbeError(f"This video is longer than {MAX_SOURCE_HOURS} hours.")

    after_video = report.split(video_line, 1)[1] if video_line in report else ""
    next_stream = re.search(r"\n\s*Stream #", after_video)
    video_block = video_line + (after_video[: next_stream.start()] if next_stream else after_video)
    fps_match = _FPS.search(video_line)
    fps = float(fps_match.group(1)) if fps_match else None
    tbr = _tbr(video_line)
    rotation = 0
    r = _ROTATION.search(video_block) or _ROTATE_TAG.search(video_block)
    if r:
        rotation = int(round(float(r.group(1)))) % 360
    width, height = int(v.group("w")), int(v.group("h"))
    if rotation in (90, 270):
        shown_w, shown_h = height, width
    else:
        shown_w, shown_h = width, height
    color = (v.group("color") or "").lower()

    a = _AUDIO.search(report)
    return {
        "duration_s": round(duration, 3),
        "video": {
            "codec": v.group("codec"), "pixel_format": v.group("pix"), "width": width, "height": height,
            "shown_width": shown_w, "shown_height": shown_h, "fps": fps, "tbr": tbr, "rotation": rotation,
            "color": color or None,
            # a file whose container rate differs a lot from its stream rate is probably recorded at a changing rate
            "variable_frame_rate_suspected": bool(fps and tbr and abs(fps - tbr) / tbr > 0.05),
            "hdr": any(marker in video_line.lower() for marker in HDR_MARKERS),
        },
        "audio": {"codec": a.group("codec"), "sample_rate": int(a.group("rate")), "layout": a.group("layout").strip()} if a else None,
    }


def notices(probe: dict[str, Any]) -> list[str]:
    """Plain sentences for the member about what will be done to the file when it is edited or rendered."""
    v, out = probe["video"], []
    if v["hdr"]:
        out.append("This video is HDR. It will be converted to standard colours so it looks right everywhere.")
    if v["rotation"]:
        out.append("This video was filmed turned. It will be straightened.")
    if v["variable_frame_rate_suspected"]:
        out.append("This video's speed of frames changes (common on phones). It will be made steady so sound and picture stay together.")
    if not probe["audio"]:
        out.append("This video has no sound.")
    return out


async def _fetch_to_disk(url: str) -> Optional[str]:
    """Downloads a stored video to a temporary file, a piece at a time, so a large file never sits in memory. None when it is too large or
    cannot be fetched, and the caller then reads it over the link instead."""
    fd, path = tempfile.mkstemp(suffix=".video")
    os.close(fd)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=15), follow_redirects=True) as client:
            async with client.stream("GET", url) as reply:
                if reply.status_code >= 400:
                    raise httpx.HTTPError(f"HTTP {reply.status_code}")
                declared = reply.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > FETCH_CAP_BYTES:
                    os.unlink(path)
                    return None
                written = 0
                out = await asyncio.to_thread(open, path, "wb")
                try:
                    async for chunk in reply.aiter_bytes(1024 * 1024):
                        written += len(chunk)
                        if written > FETCH_CAP_BYTES:
                            out.close()
                            os.unlink(path)
                            return None
                        # written off the event loop, so a slow disk does not freeze every other request
                        await asyncio.to_thread(out.write, chunk)
                finally:
                    out.close()
        return path
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("Could not fetch %s to read it: %s", url, exc)
        try:
            os.unlink(path)
        except OSError:
            pass
        return None


async def probe_source(source: str) -> dict[str, Any]:
    """Probe a local file path or an https link. Raises ProbeError for anything that is not a usable video. A link is fetched by the backend
    and read from disk, because ffmpeg's own web access is the part that fails on a small host; when that cannot be done, ffmpeg reads the link."""
    if source.startswith(("https://", "http://")):
        path = await _fetch_to_disk(source)
        if path:
            try:
                return await _probe_command(path)
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass
    return await _probe_command(source)


async def _probe_command(source: str) -> dict[str, Any]:
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    args = [exe, "-hide_banner", "-nostdin", "-protocol_whitelist", "file,https,tcp,tls,crypto", "-i", source]
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=PROBE_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()      # the stopped process is collected, not left behind
        raise ProbeError("Reading this video took too long. Please try again.")
    report = stderr.decode(errors="replace")
    probe = parse(report)
    probe["notices"] = notices(probe)
    return probe
