"""Making a dub or a narration: translate and fit, speak every line, mix over the original and render on the worker.

Everything that talks to a provider is passed in (`speak`, `store`, `forget`, `caller`), so the same flow is used live and in
tests with only those leaf calls replaced. Progress goes to `reporter` as plain stage lines, so the run shows what is happening.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from app.core.config import settings
from app.models.media import ORIGIN_AI
from app.pipelines.media import duration as audio_duration
from app.video import jobs, service
from app.video.narrate import dub as D
from app.video.narrate import plan as P
from app.video.origin import origin_of, summarise

logger = logging.getLogger(__name__)

Speak = Callable[..., Awaitable[Optional[bytes]]]            # speak(text, speaker=None) -> audio bytes
Store = Callable[[bytes], Awaitable[str]]
Forget = Callable[[str], Awaitable[None]]

MAX_FAILED_SHARE = 0.25


async def _step(reporter: Any, text: str) -> None:
    if reporter is not None:
        await reporter.step(text)


async def make_dub(
    *, workspace_id: str, user_id: str, media: dict, words: list[dict[str, Any]], target_code: str, target_name: str, speak: Speak,
    store: Store, forget: Forget, register_note: str = "", glossary: Optional[list[str]] = None, caller: Any = None, reporter: Any = None,
    keep_original_db: float = P.ORIGINAL_DUCK_DB, tighten: bool = True, captions: bool = True, accent: str = "#38bdf8",
) -> dict[str, Any]:
    """The dubbed video as a new library item. Raises service.RenderFailed with a plain message when it cannot be made."""
    segments = P.segments_from_words(words)
    if not segments:
        raise service.RenderFailed("There is no speech in this video to dub.")
    duration = float(media.get("duration_s") or (segments[-1]["end_ms"] / 1000 + 1))
    cap = float(settings.VIDEO_MAX_OUTPUT_SECONDS)
    if duration > cap:
        raise service.RenderFailed(f"That is longer than the {int(cap // 60)} minute limit for one video.")

    await _step(reporter, f"Translating {len(segments)} lines into {target_name}")
    lines, rep = await D.translate(segments, target_name=target_name, target_code=target_code, register_note=register_note, glossary=glossary,
                                   caller=caller, tighten=tighten)
    if len(rep["failed"]) > MAX_FAILED_SHARE * len(segments):
        raise service.RenderFailed(
            f"{len(rep['failed'])} of {len(segments)} lines could not be translated well enough, so no dub was made. Please try again.")
    text_by_i = {x["i"]: x["text"] for x in lines}

    urls: dict[str, str] = {}
    secs: list[Optional[float]] = []
    prepare = getattr(speak, "prepare", None)
    if prepare is not None:
        await _step(reporter, "Making the voices")
        await prepare([t for t in (text_by_i.get(s["i"]) for s in segments) if t])
    try:
        for n, seg in enumerate(segments):
            await _step(reporter, f"Speaking line {n + 1} of {len(segments)}")
            text = text_by_i.get(seg["i"])
            audio = await speak(text, None) if text else None
            length = audio_duration.audio_duration_seconds(audio) if audio else None
            if audio and length:
                urls[f"d{seg['i']}"] = await store(audio)
                secs.append(length)
            else:
                secs.append(None)
        placed = P.place(segments, secs)
        for p in placed:
            p["text_out"] = text_by_i.get(p["i"]) or ""
        if not any(p.get("flag") != "missing" for p in placed):
            raise service.RenderFailed("No line could be spoken, so no dub was made. Check the voice settings and try again.")

        await _step(reporter, "Mixing the voices over the video")
        width, height = int(media.get("width") or 1280), int(media.get("height") or 720)
        edit = P.build_dub_edit(width=width - width % 2, height=height - height % 2, duration_s=max(duration, placed[-1]["at_ms"] / 1000 + placed[-1]["dur_ms"] / 1000),
                                placed=placed, original_db=keep_original_db, captions=captions, accent=accent)
        inputs: dict[str, Any] = {"src": {"url": media["url"], "kind": "video"}}
        for key, url in urls.items():
            inputs[key] = {"url": url, "kind": "audio"}
        origin = summarise([origin_of(media), ORIGIN_AI])
        try:
            job = await jobs.enqueue(
                workspace_id=workspace_id, user_id=user_id, inputs=inputs, edit=edit, label=f"Dub in {target_name}", origin=origin,
                output={"crf": 21, "preset": "fast", "audio_bitrate": "192k", "lufs": -14.0},
            )
        except jobs.JobError as exc:
            raise service.RenderFailed(exc.message)
        done = await service.wait_for_job(workspace_id, job["id"], reporter=reporter)
    finally:
        # a render that did not finish (cancelled, timed out) is stopped before its inputs go, so it is not left to fail on a missing file
        started = locals().get("job")
        if started and "done" not in locals():
            try:
                await jobs.cancel_job(workspace_id, started["id"])
            except Exception as exc:  # noqa: BLE001 - it may already have ended
                logger.debug("No render left to stop for %s: %s", started.get("id"), exc)
        for url in urls.values():
            try:
                await forget(url)
            except Exception as exc:  # noqa: BLE001 - a leftover temporary file must never fail the dub
                logger.warning("Could not remove a temporary dub file: %s", exc)
    return {
        "media_id": done["media_id"], "job_id": done["job_id"], "origin": origin, "summary": P.summary(placed),
        "lines": [{"i": p["i"], "start_ms": p["start_ms"], "end_ms": p["end_ms"], "source": p["text"], "text": p["text_out"], "flag": p["flag"],
                   "speed": p["speed"], "at_ms": p["at_ms"], "dur_ms": p["dur_ms"], "tightened": p["i"] in rep["tightened"]} for p in placed],
        "translate": rep, "language": target_code, "voice": getattr(speak, "summary", lambda: None)(),
    }


async def make_narration(
    *, workspace_id: str, user_id: str, media: dict, script: str = "", speak: Speak, store: Store, forget: Forget, language: str = "en",
    fit_to_video: bool = False, original_audio_db: Optional[float] = None, music_url: Optional[str] = None, captions: bool = True,
    reporter: Any = None, accent: str = "#38bdf8", wpm: Optional[float] = None, lines: Optional[list[dict[str, str]]] = None,
) -> dict[str, Any]:
    """A narration over a video that has no speech (or music only). The script is spoken in parts so any length works. With
    `fit_to_video` the picture is kept at its own length and the narration must fit inside it (the caller has already fitted
    the script); without, the screen shows the background colour after the video ends if the narration is longer."""
    from app.pipelines.media.speech_chunks import split_for_speech

    script = (script or "").strip()
    # a dialogue is a list of {"speaker", "text"} lines, each spoken in its speaker's own voice
    spoken: list[tuple[str, Optional[str]]] = []
    if lines:
        for ln in lines:
            for piece in split_for_speech(str(ln.get("text", "")).strip()):
                spoken.append((piece, str(ln.get("speaker") or "") or None))
    elif script:
        spoken = [(piece, None) for piece in split_for_speech(script)]
    if not spoken:
        raise service.RenderFailed("There is no script to narrate.")
    duration = float(media.get("duration_s") or 0)
    if duration <= 0:
        raise service.RenderFailed("This video's length is not known yet.")
    parts = [t for t, _ in spoken]
    prepare = getattr(speak, "prepare", None)
    if prepare is not None:
        await _step(reporter, "Making the voices")
        await prepare(list(spoken))
    stored: list[str] = []
    audio_parts: list[tuple[bytes, float]] = []
    try:
        for n, part in enumerate(parts):
            await _step(reporter, f"Speaking part {n + 1} of {len(parts)}")
            audio = await speak(part, spoken[n][1])
            length = audio_duration.audio_duration_seconds(audio) if audio else None
            if not audio or not length:
                raise service.RenderFailed("The voice could not read the script. Check the voice settings and try again.")
            audio_parts.append((audio, length))
        total = sum(length for _, length in audio_parts)
        if fit_to_video and total > duration * 1.15:
            raise service.RenderFailed(
                f"The narration runs {total:.0f} seconds and the video is {duration:.0f}. Shorten the script, or turn off fitting to the video.")
        await _step(reporter, "Mixing the narration over the video")
        inputs: dict[str, Any] = {"src": {"url": media["url"], "kind": "video"}}
        pos = 0.0
        clips = []
        cap_words: list[dict[str, Any]] = []
        for n, (audio, length) in enumerate(audio_parts):
            url = await store(audio)
            stored.append(url)
            inputs[f"n{n}"] = {"url": url, "kind": "audio"}
            clips.append({"id": f"n{n}", "src": f"n{n}", "at_f": int(round(pos * P.FPS)), "dur_f": max(1, int(round(length * P.FPS))),
                          "src_in_f": 0, "role": "voice", "gain_db": 0})
            if captions:
                cap_words.extend(P.caption_words(parts[n], int(pos * 1000), int(length * 1000)))
            pos += length
        if music_url:
            inputs["music"] = {"url": music_url, "kind": "audio"}
        width, height = int(media.get("width") or 1280), int(media.get("height") or 720)
        edit = P.build_narration_edit(width=width - width % 2, height=height - height % 2, duration_s=duration, narration_seconds=total,
                                      has_music=bool(music_url), captions=cap_words, original_audio_db=original_audio_db, accent=accent)
        edit["audio"] = clips + [a for a in edit["audio"] if a["id"] != "narration"]
        origin = summarise([origin_of(media), ORIGIN_AI])
        try:
            job = await jobs.enqueue(
                workspace_id=workspace_id, user_id=user_id, inputs=inputs, edit=edit, label="Narrated video", origin=origin,
                output={"crf": 21, "preset": "fast", "audio_bitrate": "192k", "lufs": -14.0},
            )
        except jobs.JobError as exc:
            raise service.RenderFailed(exc.message)
        done = await service.wait_for_job(workspace_id, job["id"], reporter=reporter)
    finally:
        # a render that did not finish (cancelled, timed out) is stopped before its inputs go, so it is not left to fail on a missing file
        started = locals().get("job")
        if started and "done" not in locals():
            try:
                await jobs.cancel_job(workspace_id, started["id"])
            except Exception as exc:  # noqa: BLE001 - it may already have ended
                logger.debug("No render left to stop for %s: %s", started.get("id"), exc)
        for url in stored:
            try:
                await forget(url)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not remove a temporary narration file: %s", exc)
    return {"media_id": done["media_id"], "job_id": done["job_id"], "origin": origin, "narration_seconds": round(total, 1),
            "video_seconds": round(duration, 1), "parts": len(parts), "language": language, "voice": getattr(speak, "summary", lambda: None)()}
