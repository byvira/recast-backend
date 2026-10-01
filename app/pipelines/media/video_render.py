"""Real audio-to-video rendering: turns a stretch of a recording (a short
clip, or the whole episode) into an mp4 with word-highlighted captions from
the real transcript timing — not a single templated look, four genuinely
different compositions, each in three real aspect ratios:

  - cover        the brand's own cover image, scaled to fill
  - solid        a plain fill in the brand's own primary color (used when
                 there's no cover image, or chosen on purpose for a clean look)
  - waveform      a live waveform of the real audio, drawn in the brand's
                 accent color, over a dark fill
  - cover_wave   the cover image, with a translucent waveform strip layered
                 near the bottom

All four run through ffmpeg's own real filters (subtitles/libass, showwaves,
overlay, scale, crop, color) — nothing here hand-renders frames. ffmpeg
itself comes from `imageio_ffmpeg`'s bundled static binary, since Render's
plain Python buildpack has no system package manager to install one with.
"""

import asyncio
import logging
import re
import shutil
import tempfile
from pathlib import Path
from typing import Literal, Optional

import imageio_ffmpeg

from app.pipelines.media.image_render import caption_font_family, fonts_dir
from app.pipelines.media.video_compose import BrandLook, compose_layers
from pydantic import BaseModel

logger = logging.getLogger(__name__)

MAX_SECONDS = 30 * 60
_TIMEOUT_S = 600

VideoStyle = Literal["cover", "solid", "waveform", "cover_wave"]
VideoSize = Literal["square", "vertical", "landscape", "portrait"]

_SIZES: dict[str, tuple[int, int]] = {
    "square": (1080, 1080),     # 1:1 feed
    "vertical": (1080, 1920),   # 9:16 Reels, Shorts, TikTok, Stories
    "landscape": (1920, 1080),  # 16:9 YouTube, web
    "portrait": (1080, 1350),   # 4:5 LinkedIn and Instagram feed, the largest feed size without cropping
}
FPS = 30
# Streaming platforms normalise to about -14 LUFS, so a quiet recording would be turned up (and a loud one down) anyway;
# doing it here means the video sounds the same everywhere.
LOUDNESS_FILTER = "loudnorm=I=-14:TP=-1.5:LRA=11"
# The most the finished file may differ from the length asked for before the render is rejected.
DURATION_TOLERANCE_S = 1.0
# Clips up to this long get the slow background zoom; longer ones stay still to keep rendering time sensible.
MOTION_MAX_SECONDS = 180

# The same fallbacks app.pipelines.media.image_render.BrandTokens already
# uses for a brand with no visual identity set yet, one default across
# the product rather than a second invented one just for video.
_DEFAULT_BACKGROUND = "#0f172a"
_DEFAULT_ACCENT = "#38bdf8"


class VideoRenderError(ValueError):
    """A message that's safe to show the member."""


class TranscriptWordLike(BaseModel):
    word: str
    start_s: float
    end_s: float


def _hex_to_ass_bgr(hex_color: str, fallback: str) -> str:
    """ASS colors are &HBBGGRR& — the reverse byte order of a normal hex
    color, and with no leading '#'."""
    value = (hex_color or "").strip().lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", value):
        value = fallback.lstrip("#")
    r, g, b = value[0:2], value[2:4], value[4:6]
    return f"{b}{g}{r}".upper()


def _hex_to_ffmpeg_color(hex_color: str, fallback: str) -> str:
    """A bare RRGGBB hex, the form ffmpeg's own color parser accepts most
    consistently across every filter that takes a color option."""
    value = (hex_color or "").strip().lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", value):
        value = fallback.lstrip("#")
    return value.upper()


def _ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def _escape_ass(text: str) -> str:
    return text.replace("\\", "/").replace("{", "(").replace("}", ")")


def _escape_filter_path(path: str) -> str:
    """A path passed inside an ffmpeg filtergraph string needs its own
    escaping (colons and backslashes are filtergraph syntax)."""
    return path.replace("\\", "/").replace(":", "\\:")


def caption_layout(video_w: int, video_h: int) -> tuple[int, int, int]:
    """(font size, bottom margin, words per line) sized for the shape of the video. Tall video gets bigger text, shorter lines
    and a bottom margin that clears the buttons and captions platforms draw over the lower fifth."""
    if video_h > video_w * 1.5:       # 9:16
        return max(40, video_w // 15), int(video_h * 0.22), 4
    if video_h > video_w * 1.1:       # 4:5
        return max(36, video_w // 17), int(video_h * 0.14), 5
    if video_w > video_h * 1.2:       # 16:9
        return max(36, video_w // 30), int(video_h * 0.12), 7
    return max(36, video_w // 17), int(video_h * 0.12), 5  # square


def build_ass_captions(
    words: list[TranscriptWordLike], start_s: float, end_s: float, video_w: int, video_h: int, accent_hex: str,
    font_family: str = "Arial",
) -> str:
    """Real word-level karaoke captions from the real transcript timing:
    each line highlights the exact word being spoken at that moment (ASS's
    own `\\k` tag — a real, standard subtitle feature, not custom drawing).
    Grouped into short lines (about 6 words, or a sentence end) so nothing
    runs off the bottom of the frame."""
    _, _, per_line = caption_layout(video_w, video_h)
    in_range = [w for w in words if w.end_s > start_s and w.start_s < end_s]
    lines: list[list[TranscriptWordLike]] = []
    current: list[TranscriptWordLike] = []
    for w in in_range:
        current.append(w)
        if len(current) >= per_line or w.word.strip().endswith((".", "!", "?")):
            lines.append(current)
            current = []
    if current:
        lines.append(current)

    font_size, margin_v, per_line = caption_layout(video_w, video_h)
    accent_bgr = _hex_to_ass_bgr(accent_hex, _DEFAULT_ACCENT)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {video_w}
PlayResY: {video_h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font_family},{font_size},&H00FFFFFF,&H00{accent_bgr},&H00101010,&H90000000,1,0,0,0,100,100,0,0,1,4,2,2,60,60,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    for line in lines:
        if not line:
            continue
        line_start = max(line[0].start_s, start_s) - start_s
        line_end = min(line[-1].end_s, end_s) - start_s
        if line_end <= line_start:
            continue
        parts = []
        for w in line:
            dur_cs = max(1, round((w.end_s - w.start_s) * 100))
            parts.append(f"{{\\k{dur_cs}}}{_escape_ass(w.word)} ")
        events.append(f"Dialogue: 0,{_ass_time(line_start)},{_ass_time(line_end)},Caption,,0,0,0,,{''.join(parts).strip()}")
    return header + "\n".join(events) + "\n"


async def _run_ffmpeg(args: list[str]) -> None:
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    proc = await asyncio.create_subprocess_exec(
        exe, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        raise VideoRenderError("Rendering took too long and was stopped. Try a shorter clip.")
    if proc.returncode != 0:
        logger.warning("ffmpeg render failed (%s): %s", proc.returncode, stderr.decode(errors="replace")[-2000:])
        raise VideoRenderError("Rendering this video failed. Try a different style or a shorter clip.")


def _build_filtergraph(
    *, style: str, size_name: str, w: int, h: int, dur: float, accent_ffmpeg: str, ass_path: str,
    fonts_dir: Optional[str] = None, progress: bool = True, has_text_layer: bool = False, motion: bool = False,
) -> tuple[str, list[str]]:
    """Returns (filter_complex, output_maps). Input 0 is the background picture, input 1 the trimmed audio and, when
    there is one, input 2 the transparent title and logo layer."""
    ass = _escape_filter_path(ass_path)
    if fonts_dir:
        # the bundled brand fonts, so captions use the brand's typeface instead of whatever the server happens to have
        ass = f"{ass}':fontsdir='{_escape_filter_path(fonts_dir)}"
    # Everything is composed in RGB and converted to broadcast-standard Rec.709 video once, at the end, so the brand colours
    # of the picture, waveform and progress bar all convert the same way and look right on every phone and browser
    if motion:
        # a slow, smooth zoom in and out (a 24 second breath) so the picture is never frozen. It works on a double size copy so
        # it stays smooth, and only on the background: the title, logo, waveform and captions do not move with it.
        parts = [
            f"[0:v]scale={w * 2}:{h * 2},zoompan=z='1.05+0.05*sin(2*PI*on/{FPS * 24})':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d=1:s={w}x{h}:fps={FPS},format=rgba[v0]"
        ]
    else:
        parts = ["[0:v]format=rgba[v0]"]
    parts.append(f"[1:a]{LOUDNESS_FILTER},aresample=48000,asplit=2[aout][awave]")
    video = "v0"
    if has_text_layer:
        parts.append(f"[2:v]format=rgba[txt]")
        parts.append(f"[{video}][txt]overlay=0:0:format=auto[vt]")
        video = "vt"
    if style in ("waveform", "cover_wave"):
        wave_h = max(120, h // 3) if style == "waveform" else max(100, h // 6)
        # showwaves draws mixed colours (anything but a pure red, green, blue or white) in the wrong colour, so the wave is
        # drawn in white and used as a mask over a solid fill of the exact brand colour. One channel only, or the second one
        # is drawn on top as well.
        parts.append(
            f"[awave]aformat=channel_layouts=mono,showwaves=s={w}x{wave_h}:mode=cline:scale=sqrt:rate={FPS}:colors=white,"
            f"format=gray,lutyuv=y='clip(val*2.4\,0\,255)'[wmask]"
        )
        parts.append(f"color=c=0x{accent_ffmpeg}:s={w}x{wave_h}:r={FPS},format=rgba[wcol]")
        parts.append("[wcol][wmask]alphamerge[wave]")
        safe_bottom = {"vertical": 0.22, "portrait": 0.14, "square": 0.12, "landscape": 0.12}.get(size_name, 0.12)
        wave_y = int(h * 0.42) if style == "waveform" and size_name in ("vertical", "portrait") else (
            (h - wave_h) // 2 if style == "waveform" else int(h * (1 - safe_bottom)) - wave_h - int(h * 0.02))
        parts.append(f"[{video}][wave]overlay=(W-w)/2:{wave_y}:format=auto[v1]")
        video = "v1"
    else:
        parts.append("[awave]anullsink")
    if progress:
        bar_h = max(6, h // 180)
        parts.append(f"color=c=0x{accent_ffmpeg}:s={w}x{bar_h}:r={FPS},format=rgba[bar]")
        parts.append(f"[{video}][bar]overlay=x='-w+w*t/{dur:.3f}':y=H-h:eval=frame[v2]")
        video = "v2"
    parts.append(f"[{video}]format=rgb24,scale=in_range=pc:out_range=tv:out_color_matrix=bt709,format=yuv420p[vyuv]")
    parts.append(f"[vyuv]subtitles='{ass}'[vout]")
    return ";".join(parts), ["[vout]", "[aout]"]


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_RE = re.compile(r"Video:\s*h264.*?(\d{3,5})x(\d{3,5})")


async def _ffmpeg_report(args: list[str]) -> str:
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    proc = await asyncio.create_subprocess_exec(exe, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        raise VideoRenderError("Checking the finished video took too long.")
    return stderr.decode(errors="replace")


async def verify_output(path: Path, expected_s: float, w: int, h: int) -> None:
    """A quality gate on the finished file: it must be h264 video plus aac audio at the size asked for, about the length
    asked for, and (for clips up to 10 minutes) decode with no errors. Raises VideoRenderError otherwise, so a broken
    file is never handed to the member."""
    info = await _ffmpeg_report(["-hide_banner", "-i", str(path)])
    m = _DURATION_RE.search(info)
    actual = (int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))) if m else 0.0
    v = _VIDEO_RE.search(info)
    problems = []
    if not m or abs(actual - expected_s) > DURATION_TOLERANCE_S:
        problems.append(f"length {actual:.1f}s instead of {expected_s:.1f}s")
    if not v or (int(v.group(1)), int(v.group(2))) != (w, h):
        problems.append("wrong picture size")
    if "Audio: aac" not in info:
        problems.append("no audio track")
    if not problems and expected_s <= 600:
        decode = await _ffmpeg_report(["-v", "error", "-i", str(path), "-f", "null", "-"])
        if decode.strip():
            problems.append("decode errors")
    if problems:
        logger.warning("Video quality check failed (%s): %s", ", ".join(problems), info[-800:])
        raise VideoRenderError("The finished video did not pass the quality check, so it was not saved. Try again or pick a shorter clip.")


async def render_video(
    *,
    audio_bytes: bytes,
    words: list[TranscriptWordLike],
    start_s: float,
    end_s: float,
    style: VideoStyle,
    size: VideoSize,
    background_hex: str,
    accent_hex: str,
    cover_bytes: Optional[bytes] = None,
    font_name: Optional[str] = None,
    primary_hex: str = "",
    title: str = "",
    logo_bytes: Optional[bytes] = None,
    progress: bool = True,
    motion: bool = True,
) -> bytes:
    """Renders the [start_s, end_s) stretch of `audio_bytes` into an mp4.
    Falls back honestly from a cover-based style to its non-cover
    equivalent when there's no real cover image, rather than failing or
    faking one."""
    duration = end_s - start_s
    if duration <= 0:
        raise VideoRenderError("The end time has to be after the start time.")
    if duration > MAX_SECONDS:
        raise VideoRenderError(f"That's longer than the {MAX_SECONDS // 60}-minute limit for one video.")

    w, h = _SIZES[size]
    accent_ffmpeg = _hex_to_ffmpeg_color(accent_hex, _DEFAULT_ACCENT)

    effective_style = style
    if style in ("cover", "cover_wave") and not cover_bytes:
        effective_style = "solid" if style == "cover" else "waveform"

    # A plain TemporaryDirectory can race ffmpeg's own process exit on
    # Windows (the OS can hold a file handle open a moment longer than the
    # subprocess itself has finished), which then fails deleting the temp
    # dir with a real PermissionError rather than a fake test flake. Clean
    # up by hand with a short retry instead of trusting the context
    # manager's own single-attempt delete.
    tmp = tempfile.mkdtemp(prefix="recast-video-")
    try:
        tmp_path = Path(tmp)
        audio_path = tmp_path / "audio.src"
        audio_path.write_bytes(audio_bytes)
        # A real per-recording trim first, so the filtergraph below always
        # starts at t=0 regardless of where in the episode the clip sits.
        trimmed_path = tmp_path / "trimmed.wav"
        await _run_ffmpeg([
            "-y", "-i", str(audio_path), "-ss", str(start_s), "-t", str(duration),
            "-vn", "-ar", "44100", str(trimmed_path),
        ])

        ass_path = tmp_path / "captions.ass"
        ass_path.write_text(
            build_ass_captions(words, start_s, end_s, w, h, accent_hex, font_family=caption_font_family(font_name)), encoding="utf-8",
        )

        # the still picture every frame starts from (brand background or cover, title, logo), composed with the brand's own fonts
        look = BrandLook(primary_hex=primary_hex, secondary_hex=background_hex, accent_hex=accent_hex, heading_font=font_name)
        background_png, text_png = compose_layers(
            size=(w, h), size_name=size, style=effective_style, look=look,
            cover_bytes=cover_bytes, title=title, logo_bytes=logo_bytes,
        )
        base_path = tmp_path / "base.png"
        base_path.write_bytes(background_png)
        text_path = None
        if text_png:
            text_path = tmp_path / "text.png"
            text_path.write_bytes(text_png)
        # the slow zoom costs extra encoding time, so it is used on clips up to 3 minutes and long recordings stay still
        use_motion = motion and duration <= MOTION_MAX_SECONDS

        graph, output_maps = _build_filtergraph(
            style=effective_style, size_name=size, w=w, h=h, dur=duration, accent_ffmpeg=accent_ffmpeg,
            ass_path=str(ass_path), fonts_dir=fonts_dir(), progress=progress,
            has_text_layer=text_path is not None, motion=use_motion,
        )

        args = ["-y", "-loop", "1", "-framerate", str(FPS), "-t", f"{duration:.3f}", "-i", str(base_path), "-i", str(trimmed_path)]
        if text_path:
            args += ["-loop", "1", "-framerate", str(FPS), "-t", f"{duration:.3f}", "-i", str(text_path)]

        out_path = tmp_path / "out.mp4"
        args += [
            "-filter_complex", graph,
            "-map", output_maps[0], "-map", output_maps[1],
            # H.264 High profile at level 4.1, 30 fps, a keyframe every 2 seconds, Rec.709 colour tags, 192k AAC at 48 kHz,
            # and the index at the front of the file so it starts playing before it has fully downloaded. These are the
            # settings YouTube, LinkedIn, Instagram, Facebook and TikTok all accept without re-encoding badly.
            "-r", str(FPS), "-c:v", "libx264", "-preset", "fast", "-crf", "19", "-profile:v", "high", "-level", "4.1",
            "-pix_fmt", "yuv420p", "-g", str(FPS * 2), "-keyint_min", str(FPS * 2), "-sc_threshold", "0",
            "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart",
            "-shortest", "-t", f"{duration:.3f}",
            str(out_path),
        ]
        await _run_ffmpeg(args)
        await verify_output(out_path, duration, w, h)
        return out_path.read_bytes()
    finally:
        for attempt in range(3):
            try:
                shutil.rmtree(tmp, ignore_errors=attempt == 2)
                break
            except OSError:
                await asyncio.sleep(0.1)
