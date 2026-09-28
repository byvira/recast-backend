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
from pydantic import BaseModel

logger = logging.getLogger(__name__)

MAX_SECONDS = 30 * 60
_TIMEOUT_S = 600

VideoStyle = Literal["cover", "solid", "waveform", "cover_wave"]
VideoSize = Literal["square", "vertical", "landscape"]

_SIZES: dict[VideoSize, tuple[int, int]] = {
    "square": (1080, 1080),
    "vertical": (1080, 1920),
    "landscape": (1920, 1080),
}

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


def build_ass_captions(
    words: list[TranscriptWordLike], start_s: float, end_s: float, video_w: int, video_h: int, accent_hex: str,
) -> str:
    """Real word-level karaoke captions from the real transcript timing:
    each line highlights the exact word being spoken at that moment (ASS's
    own `\\k` tag — a real, standard subtitle feature, not custom drawing).
    Grouped into short lines (about 6 words, or a sentence end) so nothing
    runs off the bottom of the frame."""
    in_range = [w for w in words if w.end_s > start_s and w.start_s < end_s]
    lines: list[list[TranscriptWordLike]] = []
    current: list[TranscriptWordLike] = []
    for w in in_range:
        current.append(w)
        if len(current) >= 6 or w.word.strip().endswith((".", "!", "?")):
            lines.append(current)
            current = []
    if current:
        lines.append(current)

    font_size = max(28, video_w // 22)
    margin_v = max(60, video_h // 12)
    accent_bgr = _hex_to_ass_bgr(accent_hex, _DEFAULT_ACCENT)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {video_w}
PlayResY: {video_h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,Arial,{font_size},&H00FFFFFF,&H00{accent_bgr},&H00202020,&H80000000,1,0,0,0,100,100,0,0,1,3,0,2,60,60,{margin_v},1

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
    *, style: VideoStyle, has_cover: bool, w: int, h: int, dur: float,
    background_ffmpeg: str, accent_ffmpeg: str, ass_path: str,
) -> tuple[str, list[str]]:
    """Returns (filter_complex, output_maps)."""
    ass = _escape_filter_path(ass_path)
    trim = f"[0:a]atrim=start=0:end={dur},asetpts=PTS-STARTPTS[aout]"

    if style == "solid":
        graph = f"{trim};color=c={background_ffmpeg}:s={w}x{h}:d={dur}[bg];[bg]subtitles='{ass}'[vout]"
        return graph, ["[vout]", "[aout]"]

    if style == "waveform":
        graph = (
            f"{trim};[aout]asplit=2[a1][a2];"
            f"[a1]showwaves=s={w}x{max(120, h // 3)}:mode=cline:rate=25:colors={accent_ffmpeg}[wave];"
            f"color=c={background_ffmpeg}:s={w}x{h}:d={dur}[bg];"
            f"[bg][wave]overlay=(W-w)/2:(H-h)/2:format=auto,subtitles='{ass}'[vout]"
        )
        return graph, ["[vout]", "[a2]"]

    if not has_cover:
        # No real cover to fall back on for "cover"/"cover_wave" — the
        # caller resolves this to "solid"/"waveform" instead, this branch
        # should never actually be reached.
        raise VideoRenderError("This brand has no cover image set for that style yet.")

    if style == "cover":
        graph = (
            f"{trim};[1:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
            f"subtitles='{ass}'[vout]"
        )
        return graph, ["[vout]", "[aout]"]

    # cover_wave
    strip_h = max(100, h // 5)
    graph = (
        f"{trim};[aout]asplit=2[a1][a2];"
        f"[a1]showwaves=s={w}x{strip_h}:mode=cline:rate=25:colors={accent_ffmpeg}@0.7[wave];"
        f"[1:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}[cov];"
        f"[cov][wave]overlay=0:H-h,subtitles='{ass}'[vout]"
    )
    return graph, ["[vout]", "[a2]"]


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
    background_ffmpeg = _hex_to_ffmpeg_color(background_hex, _DEFAULT_BACKGROUND)
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
            build_ass_captions(words, start_s, end_s, w, h, accent_hex), encoding="utf-8",
        )

        cover_path = None
        if cover_bytes and effective_style in ("cover", "cover_wave"):
            cover_path = tmp_path / "cover.img"
            cover_path.write_bytes(cover_bytes)

        graph, output_maps = _build_filtergraph(
            style=effective_style, has_cover=bool(cover_path), w=w, h=h, dur=duration,
            background_ffmpeg=background_ffmpeg, accent_ffmpeg=accent_ffmpeg, ass_path=str(ass_path),
        )

        args = ["-y"]
        if cover_path:
            args += ["-loop", "1", "-i", str(cover_path)]
            args += ["-i", str(trimmed_path)]
            # Filtergraph above numbers cover as [1:v] and audio as [0:a] —
            # swap the real input order to match when a cover is present.
            graph = graph.replace("[0:a]", "[1:a]").replace("[1:v]", "[0:v]")
        else:
            args += ["-i", str(trimmed_path)]

        out_path = tmp_path / "out.mp4"
        args += [
            "-filter_complex", graph,
            "-map", output_maps[0], "-map", output_maps[1],
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
            "-c:a", "aac", "-b:a", "160k", "-shortest", "-t", str(duration),
            str(out_path),
        ]
        await _run_ffmpeg(args)
        return out_path.read_bytes()
    finally:
        for attempt in range(3):
            try:
                shutil.rmtree(tmp, ignore_errors=attempt == 2)
                break
            except OSError:
                await asyncio.sleep(0.1)
