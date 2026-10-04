"""Content Guard for pictures, recordings and video.

Pictures are read by a vision model once and the answer is remembered by content, so the same picture is never read twice.
Speech is screened as text: what is said in a recording or video (its transcript) and the script of a narration go through
the same rules as a post. A problem comes back as a plain, friendly message and nothing unsafe is stored or sent. If the
vision model cannot be reached the picture is let through and the problem is logged, so a provider outage never blocks work.
"""

from __future__ import annotations

import hashlib
import logging
from collections import OrderedDict
from typing import Any, Optional

import httpx
from fastapi import HTTPException

from app.agents.content_guard import config as guard_config
from app.agents.content_guard.agent import rule_screen
from app.agents.content_guard.rules import CATEGORIES, ScreenResult
from app.agents.content_guard.store import record_event
from app.core.config import settings

logger = logging.getLogger(__name__)

IMAGE_CATEGORIES = ("sexual", "violence", "hate", "self_harm")
MAX_IMAGE_BYTES = 8 * 1024 * 1024
_VERDICTS: "OrderedDict[str, ScreenResult]" = OrderedDict()
_CACHE = 500

FRIENDLY = {
    "picture": "We can't use this picture because it may show {names}. Please choose a different one.",
    "speech": "We can't use this {noun} because the words may include {names}. Please edit it and try again.",
}


class ContentRejected(HTTPException):
    """A 422 whose message is written for the person who sees it."""

    def __init__(self, message: str, categories: list[str]):
        super().__init__(status_code=422, detail={"code": "CONTENT_REJECTED", "message": message, "categories": categories})


def _names(categories: list[str]) -> str:
    return ", ".join(CATEGORIES.get(c, c).lower() for c in categories) or "content we can't publish"


async def _enabled() -> bool:
    cfg = await guard_config.ensure_fresh()
    return bool(settings.CONTENT_GUARD_LIVE_CHECKS and cfg.get("enabled", True) and cfg.get("media_check", True))


# ── pictures ──────────────────────────────────────────────────────────────────

async def check_image_bytes(data: bytes, mime_type: str = "image/jpeg") -> ScreenResult:
    """What a vision model says about the picture. Cached by the picture's bytes."""
    from app.shared.llm import call_vision
    from app.utils.jsonparser import parse_llm_json

    key = hashlib.sha256(data).hexdigest()
    if key in _VERDICTS:
        _VERDICTS.move_to_end(key)
        return _VERDICTS[key]
    reply = await call_vision(
        prompt=(
            "Decide whether this picture is unsafe for a brand to publish. Unsafe means: explicit nudity or sexual acts, "
            "graphic violence or gore, hate symbols or hateful imagery, or self harm. Swimwear, art, medical and news "
            "pictures that are not graphic, and ordinary people are fine.\n"
            'Answer with JSON only: {"unsafe": true or false, "categories": ["sexual","violence","hate","self_harm"], '
            '"reason": "a short phrase"}.'
        ),
        image_bytes=data,
        mime_type=mime_type,
    )
    parsed = parse_llm_json(reply) if isinstance(reply, str) else {}
    parsed = parsed if isinstance(parsed, dict) else {}
    unsafe = bool(parsed.get("unsafe"))
    categories = [c for c in (parsed.get("categories") or []) if c in IMAGE_CATEGORIES] if unsafe else []
    if unsafe and not categories:
        categories = ["sexual"] if "nud" in str(parsed.get("reason", "")).lower() else ["violence"]
    verdict = ScreenResult(ok=not unsafe, categories=categories, matches=[str(parsed.get("reason") or "")[:60]] if unsafe else [])
    _VERDICTS[key] = verdict
    while len(_VERDICTS) > _CACHE:
        _VERDICTS.popitem(last=False)
    return verdict


async def assert_image_ok(data: bytes, mime_type: str, *, workspace_id: Optional[str], where: str) -> None:
    """Raise ContentRejected when the picture is unsafe. Never raises for any other reason."""
    if not data or not await _enabled():
        return
    try:
        verdict = await check_image_bytes(data[:MAX_IMAGE_BYTES] if len(data) > MAX_IMAGE_BYTES else data, mime_type)
    except Exception as exc:
        logger.warning("Content Guard picture check could not run, letting it through: %s", exc)
        return
    if not verdict.ok:
        await record_event(
            outcome="blocked", categories=verdict.categories, matches=verdict.matches, text="[picture]", where=where,
            workspace_id=workspace_id,
        )
        raise ContentRejected(FRIENDLY["picture"].format(names=_names(verdict.categories)), verdict.categories)


# ── speech ────────────────────────────────────────────────────────────────────

def transcript_text(words: Any) -> str:
    """The plain text of a transcript given as a list of words (objects or dicts) or as one string."""
    if isinstance(words, str):
        return words
    parts: list[str] = []
    for w in words or []:
        parts.append(str(w.get("word", "") if isinstance(w, dict) else getattr(w, "word", "")))
    return " ".join(p for p in parts if p)


def speech_problem(text: str, noun: str = "recording") -> Optional[tuple[str, list[str]]]:
    """(friendly message, categories) when the spoken or scripted words fail the rules, else None. Free and synchronous."""
    verdict = rule_screen(text)
    if verdict.ok:
        return None
    return FRIENDLY["speech"].format(noun=noun, names=_names(verdict.categories)), verdict.categories


async def assert_speech_ok(text: str, *, noun: str, workspace_id: Optional[str], where: str) -> None:
    """Raise ContentRejected when a script or transcript fails the rules."""
    await guard_config.ensure_fresh()
    problem = speech_problem(text, noun)
    if problem:
        await record_event(
            outcome="blocked", categories=problem[1], matches=rule_screen(text).matches, text=text, where=where,
            workspace_id=workspace_id,
        )
        raise ContentRejected(*problem)


# ── what is already attached to a post ────────────────────────────────────────

async def _fetch(url: str) -> Optional[bytes]:
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content
    except Exception as exc:
        logger.warning("Content Guard could not fetch media for checking: %s", exc)
        return None


MAX_TRANSCRIBE_BYTES = 24 * 1024 * 1024
FRAME_COUNT = 3
_CLOUDINARY_VIDEO = "/video/upload/"


def frame_urls(video_url: str, duration_s: Optional[float], count: int = FRAME_COUNT) -> list[str]:
    """Still frames spread across a Cloudinary-hosted video (near the start, middle and end), as small JPEG links."""
    if _CLOUDINARY_VIDEO not in (video_url or ""):
        return []
    length = float(duration_s or 0)
    offsets = [round(length * f, 1) for f in (0.1, 0.5, 0.9)[:count]] if length >= 3 else [1, 5, 15][:count]
    base = video_url.rsplit(".", 1)[0] if "." in video_url.rsplit("/", 1)[-1] else video_url
    return [base.replace(_CLOUDINARY_VIDEO, f"{_CLOUDINARY_VIDEO}so_{o},w_640,c_limit/", 1) + ".jpg" for o in offsets]


def _track_url(item: dict[str, Any]) -> str:
    from app.pipelines.media.video_analysis import audio_track_url

    return audio_track_url(item.get("url") or "")


async def transcribe_item(item: dict[str, Any], workspace_id: str) -> Optional[list[dict[str, Any]]]:
    """The spoken words of an attached recording or video that has no transcript yet, taken once and saved on the media
    record so nothing is transcribed twice. None when it cannot be done (too long, no speech, provider down)."""
    if not settings.CONTENT_GUARD_LIVE_CHECKS:
        return None
    url = _track_url(item)
    if not url:
        return None
    data = await _fetch(url)
    if not data or len(data) > MAX_TRANSCRIBE_BYTES:
        return None
    try:
        from app.pipelines.audio.transcriber import transcribe_audio_detailed

        words, language = await transcribe_audio_detailed(data, "track.mp3", None)
    except Exception as exc:
        logger.warning("Content Guard could not transcribe %s: %s", item.get("id"), exc)
        return None
    if not words:
        return []
    saved = [{"word": w.word, "start_s": w.start_s, "end_s": w.end_s} for w in words]
    try:
        from app.db.mongo import media_assets

        await media_assets.update_one(
            {"id": item.get("id"), "workspace_id": workspace_id},
            {"$set": {"transcript": saved, "transcript_language": language}},
        )
    except Exception as exc:
        logger.warning("Content Guard could not save a transcript for %s: %s", item.get("id"), exc)
    return saved


async def _picture_verdicts(urls: list[str], mime: str = "image/jpeg") -> list[ScreenResult]:
    results: list[ScreenResult] = []
    for url in urls:
        data = await _fetch(url)
        if not data:
            continue
        try:
            results.append(await check_image_bytes(data, mime))
        except Exception as exc:
            logger.warning("Content Guard picture check could not run: %s", exc)
    return results


async def media_problem(item: dict[str, Any], workspace_id: str) -> Optional[dict[str, Any]]:
    """A verdict record for one attached item: a picture is read by the vision model; a recording by what is said in it
    (transcribed first when it has no transcript); a video by what is said in it, its poster picture and, when Ops turns it
    on, frames spread across it. `unchecked` carries the reason when nothing could be read. None when the guard is off."""
    kind = getattr(item.get("kind"), "value", item.get("kind"))
    media_id = item.get("id")
    if not media_id:
        return None
    ok, categories, read_anything, why = True, [], False, ""
    cfg = guard_config.current()

    def add(result: ScreenResult) -> None:
        nonlocal ok, read_anything
        read_anything = True
        if not result.ok:
            ok = False
            categories.extend(c for c in result.categories if c not in categories)

    if kind in ("audio", "video"):
        words = item.get("transcript")
        if not words:
            words = await transcribe_item(item, workspace_id)
            if words:
                item["transcript"] = words
        text = transcript_text(words)
        if text.strip():
            add(rule_screen(text))
        else:
            why = "no speech could be read"
        if kind == "video":
            urls = [item["poster_url"]] if item.get("poster_url") else []
            if cfg.get("video_frames") and settings.CONTENT_GUARD_LIVE_CHECKS:
                urls += frame_urls(item.get("url") or "", item.get("duration_s"))
            for result in await _picture_verdicts(urls):
                add(result)
    else:
        url = item.get("url")
        if url and settings.CONTENT_GUARD_LIVE_CHECKS:
            for result in await _picture_verdicts([url], item.get("mime_type") or "image/jpeg"):
                add(result)
        if not read_anything:
            why = "the picture could not be read"

    if not read_anything:
        return {"hash": "", "ok": True, "categories": [], "unchecked": why or "nothing could be read"}
    return {"hash": hashlib.sha256(f"{media_id}:{item.get('url')}".encode("utf-8")).hexdigest()[:32], "ok": ok, "categories": categories}


def episode_is_safe(episode: dict[str, Any]) -> bool:
    """Whether a podcast episode may appear in the public feed: its title, and what is said in it, pass the rules."""
    text = f"{episode.get('title') or ''}\n{transcript_text(episode.get('transcript'))}"
    return rule_screen(text).ok


async def read_upload_speech(contents: bytes, filename: str, *, noun: str, workspace_id: Optional[str]) -> Optional[list[dict[str, Any]]]:
    """The words of an uploaded recording or video, read before it is stored. Raises ContentRejected when they fail the rules.
    Files too large to transcribe, files with no speech and a provider that is down are let through here and read again
    (or noted as unchecked) when the post is scheduled or sent."""
    if not contents or len(contents) > MAX_TRANSCRIBE_BYTES or not await _enabled():
        return None
    try:
        from app.pipelines.audio.transcriber import transcribe_audio_detailed

        words, _language = await transcribe_audio_detailed(contents, filename, None)
    except Exception as exc:
        logger.warning("Content Guard could not read an upload's speech, letting it through: %s", exc)
        return None
    if not words:
        return None
    saved = [{"word": w.word, "start_s": w.start_s, "end_s": w.end_s} for w in words]
    await assert_speech_ok(transcript_text(saved), noun=noun, workspace_id=workspace_id, where="upload")
    return saved
