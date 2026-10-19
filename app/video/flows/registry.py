"""One-click flows: each turns one input into one or more editable projects, using the pieces built before it (analysis, creative
words, voices, stock footage). A flow only builds edit lists; making the projects and rendering them is the runner's job, so every
flow can be tested without a render and a render failure never loses the project.

A flow returns a list of specs, each `{"title", "edit", "created_from", "notes"}`.
"""
from __future__ import annotations

import logging
import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from app.db.mongo import audio_assets, audio_kits, brand_profiles, image_assets, media_assets
from app.models.media import MediaAsset, MediaKind, MediaSource
from app.video import builders, edl, transcribe
from app.video.analysis import analyze as A
from app.video.create import generate as G
from app.video.create import voice as voice_mod
from app.video.flows import edit_ops as ops
from app.video.narrate import plan as P

logger = logging.getLogger(__name__)


class FlowError(Exception):
    """The flow cannot run with what it was given; the message is plain and safe to show."""


@dataclass
class Env:
    workspace_id: str
    user_id: str
    reporter: Any = None
    # leaf calls, replaced in tests: speak(text, speaker=None) -> audio bytes; store(bytes, kind) -> (url, seconds)
    speak: Optional[Callable[..., Awaitable[Optional[bytes]]]] = None
    store_audio: Optional[Callable[[bytes], Awaitable[dict]]] = None
    caller: Any = None
    stock_search: Optional[Callable[..., Awaitable[Any]]] = None
    stock_use: Optional[Callable[..., Awaitable[Any]]] = None
    notes: list[str] = field(default_factory=list)

    async def step(self, text: str) -> None:
        if self.reporter is not None:
            await self.reporter.step(text)


async def _media(env: Env, media_id: str, kind: str) -> dict:
    row = await media_assets.find_one({"id": media_id, "workspace_id": env.workspace_id})
    k = (row or {}).get("kind")
    k = k.value if hasattr(k, "value") else k
    if not row or k != kind:
        raise FlowError(f"That {kind} is not in this workspace's library.")
    return row


async def _words(env: Env, media_id: str) -> tuple[dict, list[dict]]:
    tr = await transcribe._transcripts().find_one({"workspace_id": env.workspace_id, "media_id": media_id})
    if not tr or tr["status"] not in ("done", "partial"):
        raise FlowError("Write out what was said in this video first. A flow is built from the transcript.")
    return tr, await transcribe.all_words(tr["id"])


async def _brand(env: Env, brand_id: Optional[str]) -> dict:
    if brand_id:
        doc = await brand_profiles.find_one({"id": brand_id, "workspace_id": env.workspace_id})
    else:
        doc = await brand_profiles.find_one({"workspace_id": env.workspace_id}, sort=[("created_at", 1)])
    return doc or {}


def _colors(brand: dict) -> tuple[str, str]:
    c = ((brand.get("visual_identity") or {}).get("colors") or {})
    return (c.get("accent") or "#38bdf8", c.get("secondary") or "#0f172a")


async def moments_for(env: Env, media_id: str, words: list[dict], *, language: str, duration_s: float, count: int, min_s: float, max_s: float,
                      only_accepted: bool = False) -> list[dict]:
    """Accepted moments first, then suggested ones by score; rejected ones are never used. When the video has not been read yet it is
    read now (cached model calls, so a second flow costs nothing)."""
    from app.db.mongo import get_db

    doc = await get_db()["video_analysis"].find_one({"workspace_id": env.workspace_id, "media_id": media_id})
    moments = [m for m in (doc or {}).get("moments", []) if m.get("state") != "rejected"] if doc and doc.get("status") == "done" else []
    if not moments:
        await env.step("Finding the moments worth clipping")
        result = await A.analyze(words, language=language, duration_s=duration_s, parts=("moments",), min_s=min_s, max_s=max_s, count=count,
                                 caller=env.caller)
        moments = [m for m in result["moments"] if m.get("state") != "rejected"]
    if only_accepted:
        moments = [m for m in moments if m.get("state") == "accepted"]
    moments.sort(key=lambda m: (m.get("state") != "accepted", -float(m.get("score", 0))))
    return moments[:count]


async def _kit(env: Env, brand: dict) -> dict:
    return await audio_kits.find_one({"workspace_id": env.workspace_id, "brand_id": brand.get("id")}) or {}


async def _decorate(env: Env, edit: dict, params: dict, brand: dict, *, title: str = "") -> dict:
    """Music bed and the brand's intro and outro, when asked for. Each is a plain edit of the project, so it can be removed there."""
    music = params.get("music_media_id")
    if music:
        m = await _media(env, music, "audio")
        edit = ops.with_music_bed(edit, m["id"])
    if params.get("use_kit"):
        kit = await _kit(env, brand)
        for key, fn in (("intro", ops.with_intro), ("outro", ops.with_outro)):
            clip = kit.get(key)
            if not clip:
                env.notes.append(f"The brand has no {key} in its audio kit, so none was added.")
                continue
            m = await media_assets.find_one({"id": clip["media_id"], "workspace_id": env.workspace_id})
            if not m or not m.get("duration_s"):
                env.notes.append(f"The {key} could not be read, so it was left out.")
                continue
            edit = fn(edit, media_id=m["id"], seconds=float(m["duration_s"]), title=title)
        sponsor = kit.get("sponsor_clip")
        if params.get("sponsor") and sponsor:
            m = await media_assets.find_one({"id": sponsor["media_id"], "workspace_id": env.workspace_id})
            if m and m.get("duration_s"):
                at = int(float(params.get("sponsor_at_s") or max(5, edit["duration_f"] / edit["fps"]["num"] * edit["fps"]["den"] / 2)) * 1000)
                edit = ops.with_sponsor(edit, media_id=m["id"], seconds=float(m["duration_s"]), at_ms=at, label=kit.get("sponsor_name", ""))
    return edit


# ── the flows ────────────────────────────────────────────────────────────────────────────────────────────────────────────

#: Most clips one run may make. Each is a project and a render, so a run cannot be asked for an endless number.
MAX_CLIPS_PER_FLOW = 10


def _whole(value, default: int, low: int, high: int) -> int:
    """A whole number from a run's settings, kept between low and high. Something that is not a number is a plain refusal."""
    try:
        n = int(default if value in (None, "") else value)
    except (TypeError, ValueError):
        raise FlowError("One of the settings is not a number.") from None
    return max(low, min(high, n))


def _number(value, default: float, low: float, high: float) -> float:
    try:
        n = float(default if value in (None, "") else value)
    except (TypeError, ValueError):
        raise FlowError("One of the settings is not a number.") from None
    return max(low, min(high, n))


async def video_to_clips(env: Env, p: dict) -> list[dict]:
    media = await _media(env, p["media_id"], "video")
    tr, words = await _words(env, p["media_id"])
    duration = float(media.get("duration_s") or tr.get("duration_s") or 0)
    brand = await _brand(env, p.get("brand_id"))
    accent, background = _colors(brand)
    moments = await moments_for(env, p["media_id"], words, language=tr.get("language") or "en", duration_s=duration, count=_whole(p.get("count"), 5, 1, MAX_CLIPS_PER_FLOW),
                                min_s=_number(p.get("min_s"), 20, 3, 600), max_s=_number(p.get("max_s"), 60, 3, 600), only_accepted=bool(p.get("only_accepted")))
    if not moments:
        raise FlowError("No moment in this video was strong enough to clip. Try a longer video, or choose the moments yourself first.")
    specs = []
    for n, m in enumerate(moments, 1):
        await env.step(f"Building clip {n} of {len(moments)}")
        edit = builders.clip_from_video(media, start_s=m["start_ms"] / 1000, end_s=m["end_ms"] / 1000, words=words, size=p.get("size", "vertical"),
                                        title=m.get("title", "") if p.get("titles", True) else "", accent=accent, background=background,
                                        fit=p.get("fit", "cover"))
        edit = await _decorate(env, edit, p, brand, title=m.get("title", ""))
        specs.append({"title": m.get("title") or f"Clip {n}", "edit": edit, "created_from": {"kind": "flow", "flow": "video_to_clips",
                                                                                          "media_id": p["media_id"], "moment_id": m.get("id")},
                      "notes": []})
    return specs


async def recording_to_shorts(env: Env, p: dict) -> list[dict]:
    from app.api.v1.audio_assets import _master_transcript

    doc = await audio_assets.find_one({"id": p["audio_asset_id"], "workspace_id": env.workspace_id, "deleted": {"$ne": True}})
    if not doc:
        raise FlowError("That recording was not found.")
    voice_id = doc.get("approved_master_media_id") or doc.get("media_id")
    voice = await media_assets.find_one({"id": voice_id, "workspace_id": env.workspace_id}) if voice_id else None
    if not voice:
        raise FlowError("This recording has no audio file to turn into shorts.")
    raw = _master_transcript(doc)
    words = [{"w": w["word"], "s_ms": int(w["start_s"] * 1000), "e_ms": int(w["end_s"] * 1000)} for w in raw]
    if len(words) < 20:
        raise FlowError("This recording has too little transcript to find moments in.")
    brand = await brand_profiles.find_one({"id": doc["brand_id"], "workspace_id": env.workspace_id}) or {}
    accent, background = _colors(brand)
    duration = float(voice.get("duration_s") or words[-1]["e_ms"] / 1000)
    moments = await moments_for(env, doc["id"], words, language=doc.get("language") or "en", duration_s=duration, count=int(p.get("count", 3)),
                                min_s=float(p.get("min_s", 20)), max_s=float(p.get("max_s", 60)))
    if not moments:
        raise FlowError("No moment in this recording was strong enough for a short.")
    specs = []
    for n, m in enumerate(moments, 1):
        await env.step(f"Building short {n} of {len(moments)}")
        s, e = m["start_ms"] / 1000, m["end_ms"] / 1000
        edit = builders.from_recording(
            voice_media_id=voice["id"], seconds=e - s, words=[{"word": w["w"], "start_s": w["s_ms"] / 1000, "end_s": w["e_ms"] / 1000} for w in words],
            size=p.get("size", "vertical"), title=m.get("title", ""), accent=accent, background=background, start_s=s,
        )
        edit = await _decorate(env, edit, p, brand, title=m.get("title", ""))
        specs.append({"title": m.get("title") or f"Short {n}", "edit": edit,
                      "created_from": {"kind": "flow", "flow": "recording_to_shorts", "audio_asset_id": p["audio_asset_id"], "moment_id": m.get("id")},
                      "notes": []})
    return specs


async def _speak_beats(env: Env, beats: list[dict], language: str) -> list[dict]:
    """Each beat spoken and stored; returns [{"media_id", "seconds", "text"}] in order. A beat that cannot be spoken stops the flow with a
    plain reason, because a half-voiced video is worse than none."""
    from app.pipelines.media import duration as audio_duration

    if env.speak is None or env.store_audio is None:
        raise FlowError("Narration is not available right now.")
    speak = env.speak.for_language(language) if hasattr(env.speak, "for_language") else env.speak
    if hasattr(speak, "prepare"):
        await env.step("Making the voices")
        await speak.prepare([b["narration"] for b in beats])
    out = []
    urls: list[str] = []
    try:
        for n, b in enumerate(beats, 1):
            await env.step(f"Speaking beat {n} of {len(beats)}")
            audio = await speak(b["narration"], None)
            secs = audio_duration.audio_duration_seconds(audio) if audio else None
            if not audio or not secs:
                raise FlowError("The voice could not read the script. Check the voice settings and try again.")
            stored = await env.store_audio(audio)
            urls.append(stored["url"])
            mid = str(uuid.uuid4())
            await media_assets.insert_one(MediaAsset(
                id=mid, workspace_id=env.workspace_id, kind=MediaKind.AUDIO, url=stored["url"], mime_type="audio/mpeg", duration_s=round(secs, 2),
                source=MediaSource.SYNTHESIZED, created_by=env.user_id, created_at=datetime.now(timezone.utc), size_bytes=stored.get("bytes"),
            ).model_dump())
            out.append({"media_id": mid, "seconds": secs, "text": b["narration"]})
    except BaseException:
        # a half-voiced video is not kept: the voice files made so far and their library rows go, so nothing is left behind unused
        try:
            if out:
                await asyncio.shield(media_assets.delete_many({"workspace_id": env.workspace_id, "id": {"$in": [o["media_id"] for o in out]}}))
            from app.shared.storage import delete_file_by_url

            for url in urls:
                await asyncio.shield(delete_file_by_url(url))
        except Exception as exc:  # noqa: BLE001 - tidying up must not hide the real outcome
            logger.warning("Could not remove the voice files of a flow that stopped: %s", exc)
        raise
    return out


async def post_to_video(env: Env, p: dict) -> list[dict]:
    text = (p.get("text") or "").strip()
    if len(text) < 20:
        raise FlowError("Write or paste the post first. A narrated video is made from its words.")
    brand = await _brand(env, p.get("brand_id"))
    accent, background = _colors(brand)
    language = p.get("language") or "en"
    voice = await voice_mod.load(env.workspace_id, brand.get("id"))
    await env.step("Writing the script")
    script, meta = await G.script_from_text(text, target_seconds=p.get("target_seconds"), language=language, voice=voice, caller=env.caller)
    beats = script["beats"]
    if not beats:
        raise FlowError("A script could not be written from that post. Try a longer post.")
    if meta["problems"]:
        env.notes.append("The script was written but not every check passed: " + meta["problems"][0])
    spoken = await _speak_beats(env, beats, language)

    visual = p.get("visual", "brand")
    still = None
    if visual != "colour":
        row = await media_assets.find_one({"workspace_id": env.workspace_id, "source": MediaSource.BRAND_ASSET.value, "kind": "image"})
        still = row["id"] if row else None
    fps = {"num": 30, "den": 1}
    edit = edl.blank(p.get("size", "vertical"), fps)
    edit["background"] = background
    at = 0
    video, audio, text_items, caption_words = [], [], [], []
    for i, (b, s) in enumerate(zip(beats, spoken), 1):
        dur = max(1, edl.seconds_to_frames(s["seconds"], fps))
        clip_media = None
        if visual == "stock" and env.stock_search and env.stock_use:
            query = b.get("visual") or b.get("on_screen") or b["narration"][:60]
            try:
                found = await env.stock_search(query)
                if found:
                    clip_media = await env.stock_use(found[0], query)
            except Exception as exc:  # noqa: BLE001 - stock is a bonus; the brand picture or the colour stands in
                logger.info("Stock footage unavailable for a beat: %s", exc)
        pick = clip_media or still
        if pick:
            video.append({"id": f"b{i}", "media_id": pick, "at_f": at, "dur_f": dur, "src_in_f": 0, "speed": 1.0, "fit": "cover",
                          "fade_in_f": min(6, dur // 4) if i > 1 else 0, "fade_out_f": 0, "audio": False, "gain_db": 0.0, "motion": clip_media is None})
        audio.append({"id": f"v{i}", "media_id": s["media_id"], "at_f": at, "dur_f": dur, "src_in_f": 0, "speed": 1.0, "gain_db": 0.0,
                      "role": "voice", "fade_in_f": 0, "fade_out_f": 0, "duck": False})
        if b.get("on_screen"):
            text_items.append({"id": f"t{i}", "text": b["on_screen"], "at_f": at, "dur_f": dur, "x": 0.5, "y": 0.18, "size": 64,
                               "color": "#FFFFFF", "bold": True})
        caption_words.extend(P.caption_words(s["text"], int(at / 30 * 1000), int(s["seconds"] * 1000)))
        at += dur
    edit.update({"video": video, "audio": audio, "text": text_items, "duration_f": at,
                 "captions": {"enabled": bool(caption_words), "accent": accent, "words": caption_words}})
    edit = edl.validate(edit)
    edit = await _decorate(env, edit, p, brand, title=p.get("title", ""))
    note = [] if still or visual == "colour" else ["No brand picture was found, so the background colour is shown behind the words."]
    return [{"title": (p.get("title") or beats[0]["narration"][:60]).strip() or "Narrated video", "edit": edit,
             "created_from": {"kind": "flow", "flow": "post_to_video", "language": language, "voice_version": voice.get("version")},
             "notes": note}]


async def carousel_to_slideshow(env: Env, p: dict) -> list[dict]:
    """Each slide on screen in turn with a slow zoom. With narration, a slide stays as long as its words take to say (plus a short
    pause), so a picture is never cut off; without, each stays for `seconds_per_picture`."""
    doc = await image_assets.find_one({"id": p["image_asset_id"], "workspace_id": env.workspace_id, "deleted": {"$ne": True}})
    if not doc:
        raise FlowError("That carousel was not found.")
    slides = [s for s in sorted(doc.get("slides") or [], key=lambda s: s.get("slide_number", 0)) if s.get("media_id")]
    if not slides:
        raise FlowError("This carousel has no pictures yet.")
    if len(slides) > builders.MAX_PICTURES:
        raise FlowError(f"A slideshow can have at most {builders.MAX_PICTURES} pictures.")
    per = float(p.get("seconds_per_picture", 3.0))
    if not (builders.MIN_SECONDS_PER_PICTURE <= per <= builders.MAX_SECONDS_PER_PICTURE):
        raise FlowError(f"Each picture can stay on screen for {builders.MIN_SECONDS_PER_PICTURE:g} to {builders.MAX_SECONDS_PER_PICTURE:g} seconds.")
    brand = await brand_profiles.find_one({"id": doc.get("brand_id"), "workspace_id": env.workspace_id}) or {}
    accent, background = _colors(brand)
    fps = {"num": 30, "den": 1}
    edit = edl.blank(p.get("size", "portrait"), fps)
    edit["background"] = background
    at = 0
    video, audio, caption_words = [], [], []
    spoken_any = False
    for i, s in enumerate(slides, 1):
        words_text = (s.get("title") or "").strip()
        dur = edl.seconds_to_frames(per, fps)
        if p.get("narrate") and words_text:
            got = (await _speak_beats(env, [{"narration": words_text}], p.get("language") or "en"))[0]
            dur = max(dur, edl.seconds_to_frames(got["seconds"] + 0.4, fps))
            audio.append({"id": f"v{i}", "media_id": got["media_id"], "at_f": at, "dur_f": edl.seconds_to_frames(got["seconds"], fps), "src_in_f": 0,
                          "speed": 1.0, "gain_db": 0.0, "role": "voice", "fade_in_f": 0, "fade_out_f": 0, "duck": False})
            caption_words.extend(P.caption_words(words_text, int(at / 30 * 1000), int(got["seconds"] * 1000)))
            spoken_any = True
        video.append({"id": f"p{i}", "media_id": s["media_id"], "at_f": at, "dur_f": dur, "src_in_f": 0, "speed": 1.0, "fit": "cover",
                      "fade_in_f": min(8, dur // 4) if i > 1 else 0, "fade_out_f": 0, "audio": False, "gain_db": 0.0, "motion": True})
        at += dur
    edit.update({"video": video, "audio": audio, "duration_f": at,
                 "captions": {"enabled": bool(caption_words), "accent": accent, "words": caption_words}})
    if (doc.get("title") or "").strip():
        edit["text"] = [{"id": "title", "text": doc["title"].strip()[:200], "at_f": 0, "dur_f": min(at, 90), "x": 0.5, "y": 0.12, "size": 64,
                         "color": "#FFFFFF", "bold": True}]
    edit = edl.validate(edit)
    edit = await _decorate(env, edit, p, brand, title=doc.get("title", ""))
    notes = ["The slides have no titles to narrate, so the slideshow is silent."] if p.get("narrate") and not spoken_any else []
    return [{"title": doc.get("title") or "Slideshow", "edit": edit,
             "created_from": {"kind": "flow", "flow": "carousel_to_slideshow", "image_asset_id": p["image_asset_id"]}, "notes": notes}]


FLOWS: dict[str, dict[str, Any]] = {
    "video_to_clips": {"label": "Long video to short clips", "fn": video_to_clips, "needs": ["media_id"],
                       "describe": "Finds the strongest moments and makes a vertical clip with captions from each."},
    "recording_to_shorts": {"label": "Recording to shorts", "fn": recording_to_shorts, "needs": ["audio_asset_id"],
                            "describe": "Finds the strongest moments of a recording and makes a short video from each."},
    "post_to_video": {"label": "Post to narrated video", "fn": post_to_video, "needs": ["text"],
                      "describe": "Writes a script from a post, speaks it, and adds pictures or footage and captions."},
    "carousel_to_slideshow": {"label": "Carousel to slideshow", "fn": carousel_to_slideshow, "needs": ["image_asset_id"],
                              "describe": "Turns a carousel into a slideshow with slow motion, optional narration and music."},
}


def catalog() -> list[dict[str, Any]]:
    return [{"id": k, "label": v["label"], "describe": v["describe"], "needs": v["needs"]} for k, v in FLOWS.items()]
