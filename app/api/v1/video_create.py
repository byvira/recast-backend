"""Words for a video: opening lines, scripts, search queries for footage, caption polish, titles and tags, an edit plan, a critique and
a voice check. Every answer is checked against the real transcript and platform limits before it is returned, and the brand voice
version that wrote it is stored with it."""
import logging
from datetime import datetime, timezone
from typing import Any, Literal, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from app.agents.runtime.decisions import record_decision
from app.billing import credits
from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.db.mongo import get_db, media_assets
from app.video import transcribe
from app.video.analysis import silence as S
from app.video.analysis import windows as W
from app.video.create import generate as G
from app.video.create import voice as voice_mod

logger = logging.getLogger(__name__)


async def no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(dependencies=[Depends(no_store)])


def _records():
    return get_db()["video_creations"]


async def _budget(ctx: WorkspaceContext) -> None:
    from app.agents.supervisor.service import assert_ai_budget_available
    from app.shared.llm import set_usage_workspace

    await assert_ai_budget_available(ctx.workspace_id)
    # every model call these tools make is counted against this workspace in the AI call log
    set_usage_workspace(ctx.workspace_id)


def _charged(ctx: WorkspaceContext, action: str, step: str):
    """The credits for one of these tools: set aside before the model is asked, charged when it answers."""
    return credits.spend(ctx.workspace_id, ctx.user_id, action, step=step)


async def _video(ctx: WorkspaceContext, media_id: str) -> dict:
    media = await media_assets.find_one({"id": media_id, "workspace_id": ctx.workspace_id})
    kind = (media or {}).get("kind")
    kind = kind.value if hasattr(kind, "value") else kind
    if not media or kind not in ("video", "audio"):
        raise HTTPException(status_code=404, detail="That video is not in this workspace's library.")
    return media


async def _transcript(ctx: WorkspaceContext, media_id: str) -> tuple[dict, list[dict]]:
    await _video(ctx, media_id)
    tr = await transcribe._transcripts().find_one({"workspace_id": ctx.workspace_id, "media_id": media_id})
    if not tr or tr["status"] not in ("done", "partial"):
        raise HTTPException(status_code=409, detail="Write out what was said in this video first. Everything here is written from the transcript.")
    return tr, await transcribe.all_words(tr["id"])


def _slice(words: list[dict], start_ms: Optional[int], end_ms: Optional[int]) -> list[dict]:
    lo = start_ms if start_ms is not None else 0
    hi = end_ms if end_ms is not None else 10 ** 12
    return [w for w in words if w["s_ms"] >= lo and w["e_ms"] <= hi]


def _text(words: list[dict]) -> str:
    return " ".join(w["w"] for w in words)


async def _finish(
    ctx: WorkspaceContext, kind: str, result: Any, meta: dict, voice: dict, *, media_id: Optional[str] = None, summary: str = "",
) -> dict[str, Any]:
    """Store the record with the voice version and say what could not be fixed. The member is never shown an answer as complete
    when it is not."""
    rec = {
        "id": str(uuid4()), "workspace_id": ctx.workspace_id, "user_id": ctx.user_id, "kind": kind, "media_id": media_id,
        "voice_version": voice.get("version"), "voice_brand_id": voice.get("brand_id"), "prompt_version": meta.get("prompt_version"),
        "model_slot": meta.get("model_slot"), "tries": meta.get("tries"), "language": meta.get("language"),
        "problems": meta.get("problems", []), "result": result, "created_at": datetime.now(timezone.utc),
    }
    await _records().insert_one(dict(rec))
    await record_decision(
        workspace_id=ctx.workspace_id, user_id=ctx.user_id, agent="video", step=f"create.{kind}",
        summary=summary or f"Wrote {kind.replace('_', ' ')} in the brand voice.",
        evidence={"voice_version": voice.get("version"), "prompt_version": meta.get("prompt_version"), "tries": meta.get("tries"),
                  "problems": len(meta.get("problems", []))},
        outcome="ok" if not meta.get("problems") else "partial",
    )
    if kind == "script":
        from app.video import activity

        await activity.emit("script_ready", ctx.workspace_id, ctx.user_id, rec["id"], record_id=rec["id"], media_id=media_id or "")
    return {
        "id": rec["id"], "kind": kind, "result": result,
        "voice": {"version": voice.get("version"), "brand_id": voice.get("brand_id"), "name": voice.get("name"), "known": voice.get("known", False)},
        "meta": {k: meta.get(k) for k in ("prompt_version", "model_slot", "tries", "language", "language_problem", "problems")},
        "complete": not meta.get("problems"),
    }


def _language(body_language: Optional[str], tr: Optional[dict] = None) -> str:
    return (body_language or (tr or {}).get("language") or "en").strip() or "en"


class _Base(BaseModel):
    brand_id: Optional[str] = None
    language: Optional[str] = Field(None, max_length=12)


class HooksBody(_Base):
    media_id: str
    start_ms: Optional[int] = Field(None, ge=0)
    end_ms: Optional[int] = Field(None, ge=0)


@router.post("/hooks")
@limiter.limit("30/minute")
async def hooks(request: Request, body: HooksBody, ctx: WorkspaceContext = Depends(require("create_content"))) -> dict[str, Any]:
    tr, words = await _transcript(ctx, body.media_id)
    clip = _text(_slice(words, body.start_ms, body.end_ms))
    if len(clip.split()) < 8:
        raise HTTPException(status_code=409, detail="There is too little speech in that part of the video to write an opening line from.")
    await _budget(ctx)
    voice = await voice_mod.load(ctx.workspace_id, body.brand_id)
    result, meta = await G.hooks(clip, language=_language(body.language, tr), voice=voice)
    return await _finish(ctx, "hook", result, meta, voice, media_id=body.media_id)


class ScriptBody(_Base):
    text: str = Field(..., min_length=20, max_length=8000)
    target_seconds: Optional[float] = Field(None, ge=5, le=1800)


@router.post("/script")
@limiter.limit("20/minute")
async def script(request: Request, body: ScriptBody, ctx: WorkspaceContext = Depends(require("create_content"))) -> dict[str, Any]:
    await _budget(ctx)
    voice = await voice_mod.load(ctx.workspace_id, body.brand_id)
    async with _charged(ctx, "video_script", "video_script"):
        result, meta = await G.script_from_text(body.text, target_seconds=body.target_seconds, language=_language(body.language), voice=voice)
    return await _finish(ctx, "script", result, meta, voice)


class PictureScriptBody(_Base):
    media_ids: list[str] = Field(..., min_length=1, max_length=30)
    goal: str = Field("", max_length=300)


@router.post("/script-from-pictures")
@limiter.limit("20/minute")
async def script_from_pictures(request: Request, body: PictureScriptBody, ctx: WorkspaceContext = Depends(require("create_content"))) -> dict[str, Any]:
    pics = []
    for mid in body.media_ids:
        doc = await media_assets.find_one({"id": mid, "workspace_id": ctx.workspace_id})
        kind = (doc or {}).get("kind")
        kind = kind.value if hasattr(kind, "value") else kind
        if not doc or kind != "image":
            raise HTTPException(status_code=404, detail="One of those pictures is not in this workspace's library.")
        pics.append({"id": mid, "caption": (doc.get("title") or "")[:120], "alt": (doc.get("alt_text") or "")[:300]})
    await _budget(ctx)
    voice = await voice_mod.load(ctx.workspace_id, body.brand_id)
    async with _charged(ctx, "video_script", "picture_script"):
        result, meta = await G.picture_script(pics, body.goal, language=_language(body.language), voice=voice)
    return await _finish(ctx, "picture_script", result, meta, voice)


class BrollBody(_Base):
    beat: str = Field(..., min_length=3, max_length=600)
    mood: str = Field("", max_length=60)


@router.post("/broll")
@limiter.limit("60/minute")
async def broll(request: Request, body: BrollBody, ctx: WorkspaceContext = Depends(require("create_content"))) -> dict[str, Any]:
    await _budget(ctx)
    voice = await voice_mod.load(ctx.workspace_id, body.brand_id)
    result, meta = await G.broll(body.beat, body.mood, voice=voice)
    return await _finish(ctx, "broll", result, meta, voice)


class PolishBody(_Base):
    media_id: str
    start_ms: int = Field(..., ge=0)
    end_ms: int = Field(..., ge=0)
    glossary: list[str] = Field(default_factory=list, max_length=60)


@router.post("/captions/polish")
@limiter.limit("30/minute")
async def polish(request: Request, body: PolishBody, ctx: WorkspaceContext = Depends(require("create_content"))) -> dict[str, Any]:
    tr, words = await _transcript(ctx, body.media_id)
    part = _slice(words, body.start_ms, body.end_ms)
    if not part:
        raise HTTPException(status_code=409, detail="There are no words in that part of the video.")
    if len(part) > 400:
        raise HTTPException(status_code=400, detail="Polish captions in parts of 400 words or fewer.")
    await _budget(ctx)
    voice = await voice_mod.load(ctx.workspace_id, body.brand_id)
    result, meta = await G.polish_captions([{"w": w["w"], "s_ms": w["s_ms"], "e_ms": w["e_ms"]} for w in part],
                                           language=_language(body.language, tr), glossary=body.glossary)
    return await _finish(ctx, "caption_polish", result, meta, voice, media_id=body.media_id,
                         summary=f"Polished {len(part)} caption words; {result['changed']} corrected.")


class PublishBody(_Base):
    media_id: str
    platform: Literal["youtube", "youtube_shorts", "instagram", "tiktok", "linkedin", "facebook", "x"] = "youtube"


@router.post("/publish-text")
@limiter.limit("30/minute")
async def publish_text(request: Request, body: PublishBody, ctx: WorkspaceContext = Depends(require("create_content"))) -> dict[str, Any]:
    tr, words = await _transcript(ctx, body.media_id)
    analysis = await get_db()["video_analysis"].find_one({"workspace_id": ctx.workspace_id, "media_id": body.media_id}, {"chapters": 1})
    chapters = (analysis or {}).get("chapters") or []
    await _budget(ctx)
    voice = await voice_mod.load(ctx.workspace_id, body.brand_id)
    async with _charged(ctx, "text_post", f"publish_text:{body.platform}"):
        result, meta = await G.publish_text(_text(words)[:6000], body.platform, chapters=chapters, language=_language(body.language, tr), voice=voice)
    return await _finish(ctx, "publish_text", result, meta, voice, media_id=body.media_id)


class EditPlanBody(BaseModel):
    media_id: str
    style: Literal["tight", "natural", "punchy"] = "natural"


@router.post("/edit-plan")
@limiter.limit("20/minute")
async def edit_plan(request: Request, body: EditPlanBody, ctx: WorkspaceContext = Depends(require("create_content"))) -> dict[str, Any]:
    media = await _video(ctx, body.media_id)
    tr, words = await _transcript(ctx, body.media_id)
    duration_ms = int(float(media.get("duration_s") or tr.get("duration_s") or 0) * 1000)
    if duration_ms <= 0:
        raise HTTPException(status_code=409, detail="This video's length is not known yet.")
    pauses = [(p["start_ms"], p["end_ms"]) for p in S.pauses(words)]
    lines = "\n".join(f"{x['start_ms']} {x['text']}" for x in W.sentences(words)[:1500])
    await _budget(ctx)
    voice = await voice_mod.load(ctx.workspace_id, None)
    async with _charged(ctx, "video_plan", "edit_plan"):
        result, meta = await G.edit_plan(lines, pauses, duration_ms, body.style)
    return await _finish(ctx, "edit_plan", result, meta, voice, media_id=body.media_id,
                         summary=f"Planned {len(result)} edits in a {body.style} style.")


class CritiqueBody(_Base):
    material: str = Field(..., min_length=10, max_length=8000)
    platform: Literal["youtube", "youtube_shorts", "instagram", "tiktok", "linkedin", "facebook", "x"] = "youtube"


@router.post("/critique")
@limiter.limit("20/minute")
async def critique(request: Request, body: CritiqueBody, ctx: WorkspaceContext = Depends(require("create_content"))) -> dict[str, Any]:
    await _budget(ctx)
    voice = await voice_mod.load(ctx.workspace_id, body.brand_id)
    async with _charged(ctx, "draft_feedback", "critique"):
        result, meta = await G.critique(body.material, body.platform, language=_language(body.language), voice=voice)
    return await _finish(ctx, "critique", result, meta, voice)


class VoiceCheckBody(_Base):
    lines: list[str] = Field(..., min_length=1, max_length=40)


@router.post("/voice-check")
@limiter.limit("20/minute")
async def voice_check(request: Request, body: VoiceCheckBody, ctx: WorkspaceContext = Depends(require("create_content"))) -> dict[str, Any]:
    """Remy's check: do these lines sound like the person who will say them. The whole-text verdict uses the member's learned
    voice when there is one; the lines that drift get a rewrite that keeps the meaning and every figure."""
    from app.agents.personal import assist

    lines = [str(t).strip()[:600] for t in body.lines if str(t).strip()]
    if not lines:
        raise HTTPException(status_code=400, detail="There is nothing to check.")
    voice = await voice_mod.load(ctx.workspace_id, body.brand_id)
    language = _language(body.language)
    summary_parts = [voice.get("context", "")] if voice.get("known") else []
    member: dict[str, Any] = {"status": "unavailable"}
    try:
        member = await assist.align_draft(workspace_id=ctx.workspace_id, user_id=ctx.user_id, pipeline_type="video",
                                          draft_text="\n".join(lines)[:1800], target="video")
    except Exception as exc:  # noqa: BLE001 - the learned voice is a bonus, never a blocker
        logger.info("voice check: member voice unavailable: %s", exc)
    if member.get("status") == "ok":
        summary_parts.append(f"The member's own writing: {'; '.join(member.get('deltas') or [])[:400]}")
    from app.agents.personal import recording_voice
    from app.shared.llm import embed_text

    rec = await recording_voice.compare(ctx.workspace_id, ctx.user_id, "\n".join(lines)[:1800], embed=embed_text)
    await _budget(ctx)
    result, meta = await G.voice_check(lines, "\n".join(p for p in summary_parts if p), language=language)
    result["recording_voice"] = rec
    result["member_voice"] = {"status": member.get("status"), "in_voice": member.get("in_voice"), "similarity": member.get("similarity"),
                              "message": member.get("remy_message")}
    if member.get("status") == "ok" and member.get("in_voice") is False and result["verdict"] == "in_voice":
        result["verdict"] = "drifts"
    if rec.get("status") == "ok" and rec.get("in_voice") is False and result["verdict"] == "in_voice":
        result["verdict"] = "drifts"
    return await _finish(ctx, "voice_check", result, meta, voice, summary=f"Checked {len(lines)} lines against the voice: {result['verdict']}.")


class RecordingVoiceBody(BaseModel):
    enabled: bool


@router.get("/recording-voice")
@limiter.limit("60/minute")
async def recording_voice_status(request: Request, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    """Whether Remy is set to learn the member's voice from their own recordings, and how much it has learned so far."""
    from app.agents.personal import recording_voice

    return await recording_voice.status(ctx.workspace_id, ctx.user_id)


@router.put("/recording-voice")
@limiter.limit("20/minute")
async def recording_voice_switch(request: Request, body: RecordingVoiceBody, ctx: WorkspaceContext = Depends(require("create_content"))) -> dict[str, Any]:
    """Switch learning from the member's own recordings on or off. Off by default."""
    from app.agents.personal import recording_voice

    return await recording_voice.set_enabled(ctx.workspace_id, ctx.user_id, body.enabled)


@router.post("/recording-voice/learn")
@limiter.limit("6/minute")
async def recording_voice_learn(request: Request, ctx: WorkspaceContext = Depends(require("create_content"))) -> dict[str, Any]:
    """Learn from the member's own recordings that have not been used yet. A few embedding calls at most, and once an hour."""
    from app.agents.personal import recording_voice
    from app.shared.llm import embed_text

    # learning the member's voice is a calibration: 3 credits, only when something new was learned
    async with credits.spend(ctx.workspace_id, ctx.user_id, "voice_analysis", step="recording_voice") as hold:
        out = await recording_voice.learn(ctx.workspace_id, ctx.user_id, embed=embed_text)
        hold.nothing_made = out.get("result") != "learned"
    return out


@router.delete("/recording-voice")
@limiter.limit("20/minute")
async def recording_voice_forget(request: Request, ctx: WorkspaceContext = Depends(require("create_content"))) -> dict[str, Any]:
    """Forget everything learned from the member's recordings."""
    from app.agents.personal import recording_voice

    return await recording_voice.forget(ctx.workspace_id, ctx.user_id)


@router.get("/records")
@limiter.limit("120/minute")
async def records(
    request: Request, media_id: Optional[str] = None, kind: Optional[str] = None, limit: int = Query(20, ge=1, le=100),
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict[str, Any]:
    flt: dict[str, Any] = {"workspace_id": ctx.workspace_id}
    if media_id:
        flt["media_id"] = media_id
    if kind:
        flt["kind"] = kind
    rows = await _records().find(flt).sort("created_at", -1).limit(limit).to_list(limit)
    return {"records": [
        {"id": r["id"], "kind": r["kind"], "media_id": r.get("media_id"), "voice_version": r.get("voice_version"),
         "prompt_version": r.get("prompt_version"), "problems": r.get("problems", []), "result": r.get("result"),
         "created_at": r["created_at"].replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")}
        for r in rows
    ]}
