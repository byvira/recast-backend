"""Fresh Angles for a picture, a recording or a video clip: 3 new pieces of the same kind, not 3 written posts.

Two steps. `plan_ideas` asks the model for 3 angles shaped for the kind (a headline, scene and look for a picture, a spoken script
for a recording, a moment and an opening hook for a video cut). It is free, like the written angles. `make_pieces` then makes
the ones the member ticked with the same functions the picture, audio and video pipelines use, so each piece is charged, saved and
refunded on failure exactly like one made there. Every charge is labelled as Fresh Angles in the credits history.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field

from app.billing import costs as C
from app.billing import credits
from app.db.mongo import audio_assets, image_assets
from app.pipelines.media import duration as spoken_length
from app.pipelines.media.headline import trim_headline
from app.pipelines.media.visual_profiles import VISUAL_PROFILE_STYLE
from app.pipelines.text.angle_sources import NO_TEXT_MESSAGE, text_for_angles
from app.pipelines.text.generator import build_language_instruction
from app.pipelines.text.language_check import source_language
from app.prompts.registry import load_prompt
from app.prompts.safe import contains_banned, guard_output
from app.shared.llm import call_llm_structured

logger = logging.getLogger(__name__)

MediaKind = Literal["image", "audio", "video"]

#: What each kind is charged as, and how it reads in the credits history.
ACTION = {"image": "picture", "audio": "narration", "video": "render"}
CHARGE_LABEL = {"image": "Fresh Angles picture", "audio": "Fresh Angles recording", "video": "Fresh Angles video"}

#: A spoken angle aims at the original's length, within these bounds, so a 10 second sting and a 20 minute episode both get a
#: script that can be voiced in one go.
MIN_SCRIPT_WORDS = 40
MAX_SCRIPT_WORDS = 900
CUT_SEARCH_WORDS = 6000


class MediaAngleIdea(BaseModel):
    """One planned piece. The fields that apply to its kind are set; the member may edit them before it is made."""

    name: str = Field(max_length=80)
    rationale: str = Field("", max_length=400)
    # picture
    headline: Optional[str] = Field(None, max_length=200)
    scene: Optional[str] = Field(None, max_length=1500)
    look: Optional[str] = Field(None, max_length=40)
    # recording
    script: Optional[str] = Field(None, max_length=30000)
    # video cut
    start_s: Optional[float] = Field(None, ge=0.0, le=86_400.0)
    end_s: Optional[float] = Field(None, gt=0.0, le=86_400.0)
    hook: Optional[str] = Field(None, max_length=200)
    quote: Optional[str] = Field(None, max_length=400)
    # what it will cost: the costs-table action and its amount (1 picture, or minutes)
    action: Optional[str] = None
    units: float = 1


# ── reading the source ───────────────────────────────────────────────────

async def _image_source(asset_id: str, workspace_id: str) -> dict:
    doc = await image_assets.find_one(
        {"id": asset_id, "workspace_id": workspace_id, "deleted": {"$ne": True}},
        {"_id": 0, "id": 1, "title": 1, "brand_id": 1, "prompt": 1, "visual_profile": 1, "og_description": 1, "alt_text": 1,
         "slides.text_content": 1, "slides.layout": 1, "slides.effects": 1},
    )
    if not doc:
        raise HTTPException(status_code=404, detail="That item wasn't found.")
    return doc


async def _audio_source(asset_id: str, workspace_id: str) -> dict:
    doc = await audio_assets.find_one({"id": asset_id, "workspace_id": workspace_id, "deleted": {"$ne": True}}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="That item wasn't found.")
    return doc


async def source_brand_id(kind: MediaKind, asset_id: str, workspace_id: str) -> Optional[str]:
    """The brand the picture or recording belongs to, or None when it names none (or is not found)."""
    collection = image_assets if kind == "image" else audio_assets
    doc = await collection.find_one({"id": asset_id, "workspace_id": workspace_id}, {"brand_id": 1})
    return (doc or {}).get("brand_id") or None


def _clip_of(doc: dict, clip_id: Optional[str]) -> dict:
    for clip in doc.get("video_clips") or []:
        if isinstance(clip, dict) and clip.get("id") == clip_id:
            return clip
    raise HTTPException(status_code=404, detail="That video wasn't found.")


def _master_words(doc: dict) -> list[dict]:
    from app.api.v1.audio_assets import _master_transcript

    return [w for w in _master_transcript(doc) if isinstance(w, dict) and "start_s" in w and "end_s" in w]


# ── planning the 3 ideas ─────────────────────────────────────────────────

def _clean(value: Any, *, source: str, banned: list, limit: int) -> Optional[str]:
    text = guard_output(value, source=source, json_keys=())
    if not text or contains_banned(text, banned):
        return None
    return text.strip()[:limit]


def usable_visual_ideas(result: Any, *, source: str, banned: list, fallback_look: str) -> list[MediaAngleIdea]:
    """The picture plans that can be made: a headline and a scene that are clean and free of banned words. A look not on the list
    becomes the original's look."""
    out: list[MediaAngleIdea] = []
    for index, item in enumerate((result or {}).get("angles") or [] if isinstance(result, dict) else [], start=1):
        if not isinstance(item, dict):
            continue
        headline = _clean(item.get("headline"), source=source, banned=banned, limit=200)
        scene = _clean(item.get("scene"), source=source, banned=banned, limit=1500)
        if not headline or not scene:
            continue
        look = str(item.get("look") or "").strip()
        out.append(MediaAngleIdea(
            name=str(item.get("name") or f"Angle {index}").strip()[:80], rationale=str(item.get("rationale") or "").strip()[:400],
            headline=trim_headline(headline), scene=scene, look=look if look in VISUAL_PROFILE_STYLE else fallback_look,
            action=ACTION["image"], units=1,
        ))
    return out[:3]


def usable_audio_ideas(result: Any, *, source: str, banned: list, max_chars: int) -> list[MediaAngleIdea]:
    """The scripts that can be voiced: clean, free of banned words and short enough to narrate in one go."""
    out: list[MediaAngleIdea] = []
    for index, item in enumerate((result or {}).get("angles") or [] if isinstance(result, dict) else [], start=1):
        if not isinstance(item, dict):
            continue
        script = _clean(item.get("script"), source=source, banned=banned, limit=max_chars)
        if not script or spoken_length.count_words(script) < 5:
            continue
        out.append(MediaAngleIdea(
            name=str(item.get("name") or f"Angle {index}").strip()[:80], rationale=str(item.get("rationale") or "").strip()[:400],
            script=script, action=ACTION["audio"], units=spoken_length.estimate_seconds(script) / 60.0,
        ))
    return out[:3]


def usable_cuts(result: Any, words: list[dict], *, banned: list) -> list[MediaAngleIdea]:
    """The cuts that can be rendered: each span moved onto real word boundaries and checked like a suggested clip, none
    overlapping another, each with a clean hook. The quote is the words really spoken, never the model's."""
    from app.api.v1.audio_assets import validate_clip_suggestions

    out: list[MediaAngleIdea] = []
    for index, item in enumerate((result or {}).get("cuts") or [] if isinstance(result, dict) else [], start=1):
        if not isinstance(item, dict):
            continue
        checked = validate_clip_suggestions({"suggestions": [item]}, words)
        if not checked:
            continue
        span = checked[0]
        if any(span.start_s < (o.end_s or 0) and span.end_s > (o.start_s or 0) for o in out):
            continue
        hook = _clean(item.get("hook"), source=span.quote, banned=banned, limit=200)
        if not hook:
            continue
        out.append(MediaAngleIdea(
            name=str(item.get("name") or f"Angle {index}").strip()[:80], rationale=str(item.get("rationale") or "").strip()[:400],
            start_s=span.start_s, end_s=span.end_s, hook=trim_headline(hook), quote=span.quote,
            action=ACTION["video"], units=(span.end_s - span.start_s) / 60.0,
        ))
    return out[:3]


async def plan_ideas(
    kind: MediaKind, asset_id: str, *, workspace_id: str, brand_context: str, banned: list, clip_id: Optional[str] = None,
) -> tuple[list[MediaAngleIdea], dict]:
    """3 ideas for this item, and the item's own details the screen shows beside them (its title)."""
    from app.core.config import settings

    if kind == "image":
        doc = await _image_source(asset_id, workspace_id)
        words = text_for_angles("image", doc) or ""
        if not words:
            raise HTTPException(status_code=400, detail=NO_TEXT_MESSAGE["image"])
        current_look = doc.get("visual_profile") or "midnight_swiss"
        prompt = load_prompt(
            "text/angles/visual", brand_context=brand_context, banned_words=banned, content=words[:4000],
            language_instruction=build_language_instruction(source_language(words)),
            looks=list(VISUAL_PROFILE_STYLE.items()), current_look=current_look, source_scene=(doc.get("prompt") or "")[:1500],
        )
        result = await call_llm_structured(prompt, max_tokens=2500)
        ideas = usable_visual_ideas(result, source=words, banned=banned, fallback_look=current_look)
    elif kind == "audio":
        doc = await _audio_source(asset_id, workspace_id)
        words = text_for_angles("audio", doc) or ""
        if not words:
            raise HTTPException(status_code=400, detail=NO_TEXT_MESSAGE["audio"])
        target = max(MIN_SCRIPT_WORDS, min(MAX_SCRIPT_WORDS, spoken_length.count_words(words)))
        prompt = load_prompt(
            "text/angles/audio", brand_context=brand_context, banned_words=banned, content=words[:12000], target_words=target,
            language_instruction=build_language_instruction(source_language(words, doc.get("language"))),
        )
        result = await call_llm_structured(prompt, max_tokens=min(8000, 600 + target * 6))
        ideas = usable_audio_ideas(result, source=words, banned=banned, max_chars=settings.AUDIO_SCRIPT_MAX_CHARS)
    else:
        doc = await _audio_source(asset_id, workspace_id)
        clip = _clip_of(doc, clip_id)
        words = _master_words(doc)
        if not words:
            raise HTTPException(status_code=400, detail="This recording has no transcript to find new moments in yet. Transcribe it first.")
        spoken = " ".join(str(w.get("word", "")) for w in words[:CUT_SEARCH_WORDS])
        prompt = load_prompt(
            "text/angles/video_cuts", brand_context=brand_context, words=words[:CUT_SEARCH_WORDS],
            current_start=float(clip.get("start_s") or 0), current_end=float(clip.get("end_s") or 0),
            language_instruction=build_language_instruction(source_language(spoken, doc.get("language"))),
        )
        result = await call_llm_structured(prompt, max_tokens=2000)
        ideas = usable_cuts(result, words, banned=banned)

    if not ideas:
        raise HTTPException(status_code=502, detail="Couldn't come up with fresh angles for this. Try again.")
    return ideas, {"title": doc.get("title") or "", "brand_id": doc.get("brand_id")}


# ── making the ticked ones ───────────────────────────────────────────────

async def credits_needed(ideas: list[MediaAngleIdea], kind: MediaKind) -> int:
    table = await C.costs()
    return sum(C.price_with(table, ACTION[kind], _units(idea, kind)) for idea in ideas)


def _units(idea: MediaAngleIdea, kind: MediaKind) -> float:
    if kind == "audio":
        return spoken_length.estimate_seconds(idea.script or "") / 60.0
    if kind == "video":
        return max(0.0, float(idea.end_s or 0) - float(idea.start_s or 0)) / 60.0
    return 1


def check_ideas(kind: MediaKind, ideas: list[MediaAngleIdea], words: Optional[list[dict]] = None) -> list[MediaAngleIdea]:
    """The ideas as they will be made. Edited fields are checked here, since the member may have changed them: a picture needs a
    headline and a scene, a recording a script, and a cut a span that lies on the recording's words."""
    checked: list[MediaAngleIdea] = []
    for idea in ideas:
        if kind == "image":
            if not (idea.headline or "").strip() or not (idea.scene or "").strip():
                raise HTTPException(status_code=422, detail=f"\"{idea.name}\" needs a headline and a scene.")
            look = idea.look if idea.look in VISUAL_PROFILE_STYLE else None
            checked.append(idea.model_copy(update={"headline": trim_headline(idea.headline or ""), "look": look}))
        elif kind == "audio":
            if spoken_length.count_words(idea.script or "") < 5:
                raise HTTPException(status_code=422, detail=f"\"{idea.name}\" needs a script to voice.")
            checked.append(idea)
        else:
            from app.api.v1.audio_assets import validate_clip_suggestions

            span = validate_clip_suggestions(
                {"suggestions": [{"start_s": idea.start_s, "end_s": idea.end_s, "reason": idea.rationale}]}, words or [],
            )
            if not span:
                raise HTTPException(status_code=422, detail=f"\"{idea.name}\" is not a usable part of the recording. Plan the angles again.")
            checked.append(idea.model_copy(update={"start_s": span[0].start_s, "end_s": span[0].end_s, "hook": trim_headline(idea.hook or "")}))
    return checked


class _Stage:
    """Lets pipeline functions that report `await run.step(label)` show their step on the Fresh Angles run, and stop at a pause or
    cancel between steps, without moving its count of pieces made."""

    def __init__(self, run_id: str, prefix: str) -> None:
        self.run_id = run_id
        self.prefix = prefix

    async def step(self, label: str) -> None:
        from app.shared import pipeline_runs

        await pipeline_runs.set_stage(self.run_id, f"{self.prefix}: {label}")
        await pipeline_runs.checkpoint(self.run_id)


async def _make_picture(idea: MediaAngleIdea, source: dict, ctx) -> dict:
    from app.api.v1.image_assets import GenerateImageAssetRequest, create_image_asset

    slide = (source.get("slides") or [{}])[0] or {}
    words = slide.get("text_content") if isinstance(slide.get("text_content"), dict) else {}
    keep = {k: words[k] for k in ("author", "icon_name", "illustration_accent", "show_text", "show_mascot", "type_ratio", "inner_radius")
            if words.get(k) is not None}
    body = GenerateImageAssetRequest(
        title=f"{source.get('title') or 'Picture'} ({idea.name})"[:200], brand_id=source["brand_id"], prompt=idea.scene,
        visual_profile=idea.look or source.get("visual_profile") or "midnight_swiss", headline=idea.headline or "",
        show_logo=words.get("show_logo"), **({"active_layout": slide["layout"]} if slide.get("layout") else {}), **keep,
    )
    asset = await create_image_asset(body, ctx)
    return {"kind": "image", "id": asset.id, "name": idea.name, "href": f"/dashboard/pipelines/image?asset={asset.id}"}


async def _make_recording(idea: MediaAngleIdea, source: dict, ctx, stage: _Stage) -> dict:
    from app.api.v1.audio_assets import GenerateAudioAssetRequest, create_audio_from_script
    from app.models.voice_settings import MemberVoiceSettings

    voice = None
    snapshot = source.get("voice_settings_snapshot")
    if isinstance(snapshot, dict) and snapshot:
        try:
            voice = MemberVoiceSettings(**snapshot)
        except Exception:  # noqa: BLE001 - an older snapshot shape falls back to the member's own voice
            logger.info("Recording %s has a voice snapshot that no longer fits; using the member's voice", source.get("id"))
    body = GenerateAudioAssetRequest(
        title=f"{source.get('title') or 'Recording'} ({idea.name})"[:200], brand_id=source["brand_id"], script=idea.script,
        language=(source.get("language") or None),
    )
    asset = await create_audio_from_script(body, ctx, stage, voice=voice)
    return {"kind": "audio", "id": asset.id, "name": idea.name, "href": f"/dashboard/pipelines/audio?asset={asset.id}"}


async def _make_cut(idea: MediaAngleIdea, source: dict, clip: dict, ctx, stage: _Stage) -> dict:
    from app.api.v1.audio_assets import make_clip
    from app.models.audio_asset import MakeVideoRequest

    body = MakeVideoRequest(
        start_s=float(idea.start_s or 0), end_s=float(idea.end_s or 0), style=clip.get("style") or "cover",
        size=clip.get("size") or "vertical", title=idea.hook, platform=clip.get("platform"),
    )
    _asset, made = await make_clip(source["id"], body, ctx, stage)
    return {"kind": "video", "id": made.id, "name": idea.name, "href": "/dashboard/library?type=video"}


async def make_pieces(run_id: str, kind: MediaKind, source: dict, clip: Optional[dict], ideas: list[MediaAngleIdea], ctx) -> dict:
    """Make each ticked idea in turn. One that fails is noted and the rest still go ahead; its credits come back through the
    pipeline's own refund. A pause or cancel takes effect between pieces (and between a piece's steps)."""
    from app.shared import pipeline_runs

    made: list[dict] = []
    failed: list[dict] = []
    credits.bind_label(CHARGE_LABEL[kind])
    for position, idea in enumerate(ideas, start=1):
        await pipeline_runs.checkpoint(run_id)
        stage = _Stage(run_id, f"Angle {position} of {len(ideas)}, {idea.name}")
        await pipeline_runs.set_stage(run_id, stage.prefix)
        try:
            if kind == "image":
                made.append(await _make_picture(idea, source, ctx))
            elif kind == "audio":
                made.append(await _make_recording(idea, source, ctx, stage))
            else:
                made.append(await _make_cut(idea, source, clip or {}, ctx, stage))
        except (asyncio.CancelledError, pipeline_runs.RunCancelled):
            raise
        except Exception as exc:  # noqa: BLE001 - one piece failing must not lose the others
            if credits.is_out_of_credits(exc):
                # the rest would be refused the same way, so they are not tried
                failed.extend({"name": i.name, "reason": pipeline_runs._plain(exc)} for i in ideas[position - 1:])
                break
            logger.warning("Fresh Angles %s piece %d of %d failed: %s", kind, position, len(ideas), exc)
            failed.append({"name": idea.name, "reason": pipeline_runs._plain(exc)})
        await pipeline_runs.mark_progress(run_id, position)
    if not made:
        reason = failed[0]["reason"] if failed else "Nothing could be made."
        raise HTTPException(status_code=502, detail=f"None of the angles could be made. {reason}")
    href = made[0]["href"] if len(made) == 1 else f"/dashboard/library?type={kind}"
    summary = None
    if failed:
        missed = "; ".join(f"{f['name']}: {f['reason']}" for f in failed)
        summary = f"{len(made)} of {len(ideas)} made. Not made: {missed}"[:500]
    return {"made": made, "failed": failed, "href": href, "summary": summary}
