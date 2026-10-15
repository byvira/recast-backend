"""The audio director: a goal for a recording becomes a validated plan, run by the shared plan executor.

Its tools are the audio routes' own handlers (the same code the Audio page calls), so nothing is rewritten and every route keeps
its contract. The director adds what the routes do not have on their own: one goal made of several steps, skipping what is
already done, a budget, a decision entry for each step, and a hand-back to the member for the sign-off.

It is started through the existing job layer as the `audio.director` action, so it appears in Control Tower with pause, cancel
and retry like every other long piece of work.
"""
from __future__ import annotations

import logging
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from app.agents.runtime import Budget, Outcome, PlanExecutor, Step, ToolRegistry, ToolSpec, record_decision
from app.db.mongo import audio_assets

logger = logging.getLogger(__name__)

AGENT = "audio"
Goal = Literal["make_episode", "publish_ready", "show_notes", "clean_and_check"]
GOALS: dict[str, str] = {
    "make_episode": "Fit the script to a length if asked, voice it, then find chapters and soundbites, write show notes, check the sound, "
                    "make other languages and an export if asked, then ask for sign-off",
    "publish_ready": "Clean up if asked, transcribe, find chapters and soundbites, write show notes, check the sound, make other languages "
                     "and an export if asked, then ask for sign-off",
    "show_notes": "Transcribe if needed, find chapters, write show notes",
    "clean_and_check": "Clean up the recording and check the sound",
}


class DirectorSettings(BaseModel):
    goal: Goal = "publish_ready"
    # The script and voice to narrate, in the shape the generate route takes (needed for the make_episode goal).
    narration: Optional[dict] = None
    # Optional extra work, each in the shape the route for it already takes.
    cleanup: Optional[dict] = None
    assemble: Optional[dict] = None
    outputs: list[Literal["show_notes", "captions", "summary"]] = Field(default_factory=lambda: ["show_notes"])
    language: Optional[str] = Field(default=None, max_length=12)
    soundbites: bool = True
    # Optional extras. Each one only runs when asked for, and a failed extra never stops the rest.
    fit_seconds: Optional[int] = Field(default=None, ge=1, le=36000)       # rewrite the script to about this length before voicing it
    localize: list[str] = Field(default_factory=list, max_length=4)        # full language names, for example "Tamil"
    clip_suggestions: bool = False
    export_format: Optional[Literal["mp3", "wav"]] = None


class _Reporter:
    """Hands a handler's own `run.step(label)` to the job's reporter, so a handler's sub-steps show in the activity experience."""

    def __init__(self, base: Any) -> None:
        self._base = base

    async def step(self, label: str) -> None:
        if self._base is not None:
            await self._base.step(label)


def _audio():
    from app.api.v1 import audio_assets as audio

    return audio


def _inner(name: str):
    handler = getattr(_audio(), name)
    return getattr(handler, "__wrapped__", handler)


async def _doc(state: dict) -> dict:
    doc = await audio_assets.find_one({"id": state["asset_id"], "workspace_id": state["ctx"].workspace_id, "deleted": {"$ne": True}})
    return doc or {}


# ── tools: thin calls into the handlers the Audio page already uses ──────────────────────────────────────────────────────

async def _tool_narrate(*, state: dict, request: dict) -> Any:
    """Audio generation: a script (or a post) voiced into a new recording. The new recording becomes the one the rest of the plan
    works on."""
    from app.core.rbac import assert_permission

    assert_permission(state["ctx"].member, "create_content")
    audio = _audio()
    asset = await audio.create_audio_from_script(audio.GenerateAudioAssetRequest(**request), state["ctx"], state["reporter"])
    state["asset_id"] = asset.id
    return {"asset_id": asset.id, "version_count": getattr(asset, "version_count", None)}


async def _tool_cleanup(*, state: dict, settings: dict) -> Any:
    from app.pipelines.media.audio_cleanup import CleanupSettings

    out = await _inner("cleanup_audio_asset")(
        request=None, audio_asset_id=state["asset_id"], body=CleanupSettings(**settings), ctx=state["ctx"], run=state["reporter"],
    )
    return {"version": getattr(out, "version_count", None)}


async def _tool_transcribe(*, state: dict, language: Optional[str] = None) -> Any:
    out = await _inner("transcribe_audio_asset")(
        request=None, audio_asset_id=state["asset_id"], body=_audio().TranscribeRequest(language=language), ctx=state["ctx"],
        run=state["reporter"],
    )
    return {"words": len(getattr(out, "transcript", []) or [])}


async def _tool_chapters(*, state: dict) -> Any:
    out = await _inner("generate_chapters")(request=None, audio_asset_id=state["asset_id"], ctx=state["ctx"])
    return {"chapters": len(out.chapters), "reason": out.reason}


async def _tool_soundbites(*, state: dict) -> Any:
    out = await _inner("extract_soundbites")(request=None, audio_asset_id=state["asset_id"], ctx=state["ctx"])
    return {"soundbites": len(out)}


async def _tool_derivative(*, state: dict, output_type: str) -> Any:
    out = await _inner("generate_derivative")(
        request=None, audio_asset_id=state["asset_id"], body=_audio().DerivativeRequest(output_type=output_type), ctx=state["ctx"],
    )
    return {"output_type": out.output_type, "quality": out.quality}


async def _tool_sound_checks(*, state: dict) -> Any:
    out = await _inner("sound_checks")(request=None, audio_asset_id=state["asset_id"], ctx=state["ctx"])
    return out["checks"]


async def _tool_assemble(*, state: dict, plan: dict) -> Any:
    from app.pipelines.media.audio_assemble import AssemblePlan

    out = await _inner("assemble_audio_asset")(
        request=None, audio_asset_id=state["asset_id"], body=AssemblePlan(**plan), ctx=state["ctx"], run=state["reporter"],
    )
    return {"assemblies": len(getattr(out, "assemblies", []) or [])}


async def _tool_fit_script(*, state: dict, request: dict, target_seconds: int) -> Any:
    """Rewrite the script to about the chosen length. The narration request is changed in place, so the next step voices the fitted
    script; when the script cannot be fitted the original is kept."""
    from app.core.rbac import assert_permission

    assert_permission(state["ctx"].member, "create_content")
    script = (request.get("script") or "").strip()
    if not script:
        return {"status": "no_script"}
    audio = _audio()
    out = await _inner("fit_script")(
        request=None, body=audio.FitScriptRequest(script=script, target_seconds=target_seconds, words_per_minute=request.get("words_per_minute")),
        ctx=state["ctx"],
    )
    if out.get("script") and out.get("status") in ("fitted", "ok", "adjusted", "trimmed", "expanded"):
        request["script"] = out["script"]
    return {"status": out.get("status"), "estimated_seconds": out.get("estimated_seconds")}


async def _tool_localize(*, state: dict, language: str) -> Any:
    out = await _inner("localize_audio_asset")(
        request=None, audio_asset_id=state["asset_id"], body=_audio().LocalizeAudioAssetRequest(target_language=language), ctx=state["ctx"],
    )
    return {"asset_id": getattr(out, "id", None), "language": language}


async def _tool_clip_suggestions(*, state: dict) -> Any:
    out = await _inner("suggest_clips_for_audio_asset")(request=None, audio_asset_id=state["asset_id"], ctx=state["ctx"])
    return {"suggestions": len(getattr(out, "suggestions", []) or [])}


async def _tool_export(*, state: dict, export_format: str) -> Any:
    out = await _inner("export_audio_asset")(
        request=None, audio_asset_id=state["asset_id"], body=_audio().ExportAudioAssetRequest(export_format=export_format), ctx=state["ctx"],
    )
    return {"media_id": getattr(out, "id", None), "format": export_format}


REGISTRY = ToolRegistry(AGENT)
for _spec, _fn in (
    (ToolSpec("fit_script", "Rewrite the script to run about the chosen length", cost="llm", timeout_s=180), _tool_fit_script),
    (ToolSpec("localize", "Translate the recording into another language and voice it", cost="provider", timeout_s=900), _tool_localize),
    (ToolSpec("clip_suggestions", "Suggest short clips to review", cost="llm", timeout_s=180), _tool_clip_suggestions),
    (ToolSpec("export", "Export the recording as mp3 or wav", cost="free", timeout_s=300), _tool_export),
    (ToolSpec("narrate", "Voice a script into a new recording", cost="provider", timeout_s=900), _tool_narrate),
    (ToolSpec("cleanup", "Clean up the recording as a new version", cost="free", timeout_s=900), _tool_cleanup),
    (ToolSpec("transcribe", "Write out what was said with word timing", cost="llm", timeout_s=900), _tool_transcribe),
    (ToolSpec("chapters", "Find where the topic changes, checked against the transcript", cost="llm", timeout_s=120), _tool_chapters),
    (ToolSpec("soundbites", "Find the strongest short moments", cost="llm", timeout_s=300), _tool_soundbites),
    (ToolSpec("derivative", "Write show notes, captions or a summary from the transcript", cost="llm", timeout_s=180), _tool_derivative),
    (ToolSpec("sound_checks", "Measure loudness, peaks and long silences", cost="free", timeout_s=120), _tool_sound_checks),
    (ToolSpec("assemble", "Build the finished episode from the recording and the audio kit", cost="free", timeout_s=900), _tool_assemble),
):
    REGISTRY.register(_spec, _fn)


# ── skip rules: do not repeat what is already there ──────────────────────────────────────────────────────────────────────

async def _has_transcript(state: dict) -> Optional[str]:
    return "Already transcribed." if (await _doc(state)).get("transcript") else None


async def _no_transcript(state: dict) -> Optional[str]:
    return None if (await _doc(state)).get("transcript") else "There is no transcript to work from."


async def _chapters_not_needed(state: dict) -> Optional[str]:
    doc = await _doc(state)
    if not doc.get("transcript"):
        return "There is no transcript to find chapters in."
    return "Chapters are already there." if doc.get("chapters") else None


def _derivative_not_needed(kind: str):
    async def rule(state: dict) -> Optional[str]:
        doc = await _doc(state)
        if not doc.get("transcript"):
            return "There is no transcript to write from."
        return f"The {kind.replace('_', ' ')} is already written." if (doc.get("derivatives") or {}).get(kind) else None
    return rule


# ── the plan ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

def build_plan(settings: DirectorSettings) -> list[Step]:
    """The steps for a goal, in order. Rules, not a model: the same recording and goal always give the same plan."""
    steps: list[Step] = []
    if settings.goal == "make_episode":
        narration = settings.narration if settings.narration is not None else {}
        if settings.fit_seconds and narration.get("script"):
            steps.append(Step("fit_script", "Fitting the script to the length", {"request": narration, "target_seconds": settings.fit_seconds},
                              required=False))
        steps.append(Step("narrate", "Voicing the script", {"request": narration}))
    if settings.goal in ("make_episode", "publish_ready", "clean_and_check") and settings.cleanup:
        steps.append(Step("cleanup", "Cleaning up the recording", {"settings": settings.cleanup}))
    if settings.goal in ("make_episode", "publish_ready", "show_notes"):
        steps.append(Step("transcribe", "Writing out what was said", {"language": settings.language}, required=False,
                          skip_if=_has_transcript))
        steps.append(Step("chapters", "Finding chapters", required=False, skip_if=_chapters_not_needed))
        if settings.goal in ("make_episode", "publish_ready") and settings.soundbites:
            steps.append(Step("soundbites", "Finding the strongest moments", required=False, skip_if=_no_transcript))
        for kind in settings.outputs:
            steps.append(Step("derivative", f"Writing the {kind.replace('_', ' ')}", {"output_type": kind}, required=False,
                              skip_if=_derivative_not_needed(kind)))
    if settings.goal in ("make_episode", "publish_ready", "clean_and_check"):
        steps.append(Step("sound_checks", "Checking the sound", required=False))
    if settings.goal in ("make_episode", "publish_ready") and settings.assemble:
        steps.append(Step("assemble", "Building the episode", {"plan": settings.assemble}, required=False))
    if settings.goal in ("make_episode", "publish_ready"):
        for language in settings.localize:
            steps.append(Step("localize", f"Making the {language} version", {"language": language}, required=False))
        if settings.clip_suggestions:
            steps.append(Step("clip_suggestions", "Suggesting short clips", required=False, skip_if=_no_transcript))
        if settings.export_format:
            steps.append(Step("export", f"Exporting as {settings.export_format}", {"export_format": settings.export_format}, required=False))
    return steps


async def run_director(ctx: Any, payload: Any, reporter: Any) -> dict:
    """The job action body: plan, run, and hand back what was done and what is left for the member."""
    from fastapi import HTTPException

    settings: DirectorSettings = payload.settings
    if settings.goal == "make_episode":
        if not settings.narration:
            raise HTTPException(status_code=400, detail="Add the script and voice to narrate first.")
    elif not payload.audio_asset_id:
        raise HTTPException(status_code=400, detail="Choose a recording first.")
    steps = build_plan(settings)
    if payload.audio_asset_id:
        doc = await audio_assets.find_one({"id": payload.audio_asset_id, "workspace_id": ctx.workspace_id, "deleted": {"$ne": True}})
        if not doc:
            raise HTTPException(status_code=404, detail="Audio asset not found.")
    state = {"ctx": ctx, "asset_id": payload.audio_asset_id, "reporter": _Reporter(reporter)}

    async def before(label: str, index: int, total: int) -> None:
        await reporter.step(label)

    executor = PlanExecutor(
        REGISTRY, workspace_id=ctx.workspace_id, user_id=ctx.user_id, budget=Budget(llm_calls=10 + (2 if settings.fit_seconds else 0) + (1 if settings.clip_suggestions else 0),
                      provider_calls=2 + len(settings.localize), worker_jobs=0),
        before_step=before, subject={"audio_asset_id": payload.audio_asset_id, "goal": settings.goal},
    )
    outcomes: list[Outcome] = await executor.run(steps, state)
    done = [o for o in outcomes if o.status == "done"]
    failed = [o for o in outcomes if o.status in ("failed", "stopped")]
    asset_id = state["asset_id"]
    fresh = (await audio_assets.find_one({"id": asset_id, "workspace_id": ctx.workspace_id}) or {}) if asset_id else {}
    needs_signoff = (settings.goal in ("make_episode", "publish_ready") and bool(asset_id) and not failed
                     and fresh.get("approval_status") != "approved")
    if needs_signoff:
        await record_decision(
            workspace_id=ctx.workspace_id, user_id=ctx.user_id, agent=AGENT, step="signoff",
            summary="Everything is ready. Your sign-off is the one thing left before this recording can go out.",
            evidence={"audio_asset_id": asset_id}, outcome="needs_decision",
        )
    return {
        "goal": settings.goal, "asset_id": asset_id, "steps": [o.public() for o in outcomes],
        "done": len(done), "failed": len(failed), "needs_signoff": needs_signoff,
    }
