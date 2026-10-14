"""A recording made into a video by the render worker.

The existing audio-to-video look (a brand background or cover, the title and logo, a live waveform, a progress bar, a slow
zoom, word-highlighted captions, loudness to -14) is expressed as an edit list and rendered by the worker, so it has no
30 minute cap and no render inside the web process. The picture layers are made here exactly as before (the same Pillow code),
stored briefly, and removed when the render ends.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.core.config import settings
from app.pipelines.media.video_compose import BrandLook, compose_layers
from app.pipelines.media.video_render import MOTION_MAX_SECONDS, TranscriptWordLike, _SIZES
from app.shared.storage import ContentType as UploadContentType, delete_file_by_url, upload_file
from app.video import edl, jobs, service
from app.video.origin import summarise

logger = logging.getLogger(__name__)

FPS = 30
# where the wave sits and how tall it is, as fractions of the picture: the same placement the local renderer uses
_SAFE_BOTTOM = {"vertical": 0.22, "portrait": 0.14, "square": 0.12, "landscape": 0.12}


def wave_geometry(style: str, size_name: str, h: int) -> tuple[float, float]:
    """(top as a fraction of the height, height as a fraction), matching the local renderer."""
    wave_h = max(120, h // 3) if style == "waveform" else max(100, h // 6)
    if style == "waveform" and size_name in ("vertical", "portrait"):
        top = int(h * 0.42)
    elif style == "waveform":
        top = (h - wave_h) // 2
    else:
        top = int(h * (1 - _SAFE_BOTTOM.get(size_name, 0.12))) - wave_h - int(h * 0.02)
    return max(0.0, top / h), wave_h / h


def build_edit(
    *, size_name: str, style: str, duration_s: float, start_s: float, words: list[TranscriptWordLike], accent_hex: str,
    background_hex: str, progress: bool, motion: bool, has_text_layer: bool,
) -> dict[str, Any]:
    """The edit list for one recording: background (slowly zooming when short), the title and logo layer, the voice, captions,
    and the waveform and progress bar looks. Pure, so it can be checked without any file."""
    w, h = _SIZES[size_name]
    frames = max(1, edl.seconds_to_frames(duration_s, {"num": FPS, "den": 1}))
    end_s = start_s + duration_s
    caption_words = []
    for word in words:
        if word.end_s > start_s and word.start_s < end_s:
            s_ms = max(0, int(round((word.start_s - start_s) * 1000)))
            e_ms = min(int(duration_s * 1000), max(s_ms, int(round((word.end_s - start_s) * 1000))))
            caption_words.append({"w": word.word, "s_ms": s_ms, "e_ms": e_ms})
    video = [{"id": "bg", "src": "bg", "at_f": 0, "dur_f": frames, "src_in_f": 0, "fit": "cover",
              "motion": bool(motion and duration_s <= MOTION_MAX_SECONDS)}]
    inputs_audio = [{"id": "voice", "src": "voice", "at_f": 0, "dur_f": frames, "src_in_f": int(round(start_s * FPS)),
                     "role": "voice", "gain_db": 0}]
    if has_text_layer:
        video.append({"id": "txt", "src": "txt", "at_f": 0, "dur_f": frames, "src_in_f": 0, "fit": "cover", "alpha": True})
    effects: dict[str, Any] = {}
    if style in ("waveform", "cover_wave"):
        top, height = wave_geometry(style, size_name, h)
        effects["waveform"] = {"enabled": True, "color": accent_hex if len(accent_hex) == 7 else "#38bdf8", "y": top, "height": height}
    if progress:
        effects["progress_bar"] = {"enabled": True, "color": accent_hex if len(accent_hex) == 7 else "#38bdf8", "height_px": max(6, h // 180)}
    return {
        "version": 1, "fps": {"num": FPS, "den": 1}, "width": w, "height": h, "duration_f": frames,
        "background": background_hex if len(background_hex) == 7 and background_hex.startswith("#") else "#0f172a",
        "video": video, "audio": inputs_audio, "text": [],
        "captions": {"enabled": bool(caption_words), "accent": accent_hex if len(accent_hex) == 7 else "#38bdf8", "words": caption_words},
        "effects": effects,
    }


async def render_via_worker(
    *, workspace_id: str, user_id: str, audio_url: str, voice_origin: str, words: list[TranscriptWordLike], start_s: float, end_s: float,
    style: str, size: str, background_hex: str, accent_hex: str, primary_hex: str = "", cover_bytes: Optional[bytes] = None,
    cover_origin: str = "own", font_name: Optional[str] = None, title: str = "", logo_bytes: Optional[bytes] = None,
    progress: bool = True, motion: bool = True, reporter: Any = None, label: str = "Video from a recording",
) -> dict:
    """Make the video on the worker and return {"media_id", "job_id", ...}. Raises service.RenderFailed with a plain message."""
    duration = end_s - start_s
    if duration <= 0:
        raise service.RenderFailed("The end time has to be after the start time.")
    cap = float(settings.VIDEO_MAX_OUTPUT_SECONDS)
    if duration > cap:
        raise service.RenderFailed(f"That's longer than the {int(cap // 60)}-minute limit for one video.")
    w, h = _SIZES[size]
    effective = style
    if style in ("cover", "cover_wave") and not cover_bytes:
        effective = "solid" if style == "cover" else "waveform"
    look = BrandLook(primary_hex=primary_hex, secondary_hex=background_hex, accent_hex=accent_hex, heading_font=font_name)
    background_png, text_png = compose_layers(
        size=(w, h), size_name=size, style=effective, look=look, cover_bytes=cover_bytes, title=title, logo_bytes=logo_bytes,
    )
    stored: list[str] = []
    try:
        bg_url = await upload_file(background_png, UploadContentType.IMAGE, user_id)
        stored.append(bg_url)
        txt_url = None
        if text_png:
            txt_url = await upload_file(text_png, UploadContentType.IMAGE, user_id)
            stored.append(txt_url)
        edit = build_edit(
            size_name=size, style=effective, duration_s=duration, start_s=start_s, words=words, accent_hex=accent_hex,
            background_hex=background_hex, progress=progress, motion=motion, has_text_layer=bool(txt_url),
        )
        inputs: dict[str, Any] = {"bg": {"url": bg_url, "kind": "image"}, "voice": {"url": audio_url, "kind": "audio"}}
        if txt_url:
            inputs["txt"] = {"url": txt_url, "kind": "image"}
        origins = [voice_origin, cover_origin if (cover_bytes and effective in ("cover", "cover_wave")) else "template"]
        try:
            job = await jobs.enqueue(
                workspace_id=workspace_id, user_id=user_id, inputs=inputs, edit=edit, label=label, origin=summarise(origins),
                output={"crf": 19, "preset": "fast", "audio_bitrate": "192k", "lufs": -14.0},
            )
        except jobs.JobError as exc:
            raise service.RenderFailed(exc.message)
        return await service.wait_for_job(workspace_id, job["id"], reporter=reporter)
    finally:
        for url in stored:
            await delete_file_by_url(url)
