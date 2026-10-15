"""The video director: a goal for some footage, a recording, a post or a carousel becomes a validated plan, run by the shared plan
executor.

The tools are the flows' own builders and the render service (the same code the Video page uses), so nothing is rewritten. The
director adds what a flow does not have by itself: a source check that gives a plain reason before any work, a voice check on the
titles it made, a budget, a decision entry for each step, a limit on how many renders one request may start, and a hand-back to
the member for the review before anything goes out.

It is started through the job layer as the `video.director` action, so it shows in the activity dock with cancel like every other
long piece of work.
"""
from __future__ import annotations

import logging
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from app.agents.runtime import Budget, Outcome, PlanExecutor, Step, ToolRegistry, ToolSpec, record_decision

logger = logging.getLogger(__name__)

AGENT = "video"
FlowName = Literal["video_to_clips", "recording_to_shorts", "post_to_video", "carousel_to_slideshow"]


class DirectorSettings(BaseModel):
    flow: FlowName
    # What the flow takes: media_id, audio_asset_id, text or image_asset_id, plus its own options (brand_id, language, size ...).
    params: dict[str, Any] = Field(default_factory=dict)
    render: bool = True
    preset: Literal["standard", "high", "small"] = "standard"
    # One request never starts more renders than this; the rest stay as projects to render when the member chooses.
    max_renders: int = Field(default=3, ge=1, le=6)
    voice_check: bool = True


def _flows():
    from app.video.flows import registry as R

    return R


def _toolkit():
    from app.agents.runtime import toolkit

    return toolkit


def _env(state: dict):
    R = _flows()
    ctx = state["ctx"]
    return state.setdefault("env", R.Env(workspace_id=ctx.workspace_id, user_id=ctx.user_id, reporter=state.get("reporter")))


# ── tools ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

async def _check_source(*, state: dict, settings: dict) -> Any:
    """A plain reason, before any work, when the flow cannot start from what it was given."""
    R = _flows()
    spec = R.FLOWS[settings["flow"]]
    params = settings["params"]
    missing = [k for k in spec["needs"] if not params.get(k)]
    if missing:
        raise RuntimeError(f"This needs: {', '.join(missing).replace('_', ' ')}.")
    if params.get("media_id"):
        try:
            await R._words(_env(state), params["media_id"])
        except R.FlowError as exc:
            raise RuntimeError(str(exc))
    return {"flow": settings["flow"], "label": spec["label"]}


async def _build(*, state: dict, settings: dict) -> Any:
    """The flow's own builder, then the projects it describes. Nothing is rendered here."""
    from app.video.flows import runner

    R = _flows()
    env = _env(state)
    env.notes = []
    try:
        specs = await R.FLOWS[settings["flow"]]["fn"](env, settings["params"])
    except R.FlowError as exc:
        raise RuntimeError(str(exc))
    if not specs:
        raise RuntimeError("Nothing could be made from that.")
    state["projects"] = await runner.create_projects(env, specs)
    state["notes"] = list(env.notes)
    return {"projects": len(state["projects"])}


async def _voice_check(*, state: dict, settings: dict) -> Any:
    """Remy reads each project's title against the member's voice. It only reports; nothing is changed."""
    checks = []
    for p in state.get("projects", [])[:6]:
        result = await _toolkit().use("remy.check_voice", ctx=state["ctx"], reporter=state.get("reporter"), text=p["title"], pipeline_type="video",
                                      target=settings["flow"])
        if result.ok:
            checks.append({"project_id": p["project_id"], "check": result.value})
    state["voice_checks"] = checks
    return {"checked": len(checks)}


async def _render(*, state: dict, settings: dict) -> Any:
    from app.video import service

    done = 0
    for p in state.get("projects", [])[: settings["max_renders"]]:
        try:
            out = await service.render_project_and_wait(
                workspace_id=state["ctx"].workspace_id, user_id=state["ctx"].user_id, project_id=p["project_id"], preset=settings["preset"],
                reporter=state.get("reporter"),
            )
            p["media_id"] = out["media_id"]
            done += 1
        except service.RenderFailed as exc:
            p["error"] = str(exc)
    if not done:
        raise RuntimeError("None of the videos could be rendered. The projects are kept, so they can be rendered again.")
    return {"rendered": done, "of": len(state.get("projects", [])), "limit": settings["max_renders"]}


REGISTRY = ToolRegistry(AGENT)
for _spec, _fn in (
    (ToolSpec("check_source", "Check the flow can start from what it was given", cost="free", timeout_s=30), _check_source),
    (ToolSpec("build", "Build editable projects with the chosen flow", cost="llm", timeout_s=900), _build),
    (ToolSpec("voice_check", "Check the titles made against the member's voice", cost="llm", timeout_s=120), _voice_check),
    (ToolSpec("render", "Render the projects, up to the limit for one request", cost="worker", timeout_s=7800), _render),
):
    REGISTRY.register(_spec, _fn)


def build_plan(settings: DirectorSettings) -> list[Step]:
    """Fixed rules, not a model: the same flow and options always give the same plan."""
    args = {"settings": settings.model_dump()}
    steps = [Step("check_source", "Checking the source", args), Step("build", "Building the projects", args)]
    if settings.voice_check:
        steps.append(Step("voice_check", "Checking the titles against your voice", args, required=False))
    if settings.render:
        steps.append(Step("render", "Rendering", args, required=False))
    return steps


async def run_director(ctx: Any, payload: Any, reporter: Any) -> dict:
    """The job action body: plan, run, and hand back what was made and what is left for the member."""
    settings: DirectorSettings = payload.settings
    state: dict[str, Any] = {"ctx": ctx, "reporter": reporter}

    async def before(label: str, index: int, total: int) -> None:
        await reporter.step(label)

    executor = PlanExecutor(
        REGISTRY, workspace_id=ctx.workspace_id, user_id=ctx.user_id, before_step=before,
        budget=Budget(llm_calls=8, worker_jobs=settings.max_renders if settings.render else 0),
        subject={"flow": settings.flow},
    )
    outcomes: list[Outcome] = await executor.run(build_plan(settings), state)
    required = {"check_source", "build"}
    blocked = [o for o in outcomes if o.status in ("failed", "stopped") and o.tool in required]
    projects = state.get("projects", [])
    rendered = [p for p in projects if p.get("media_id")]
    needs_review = bool(projects) and not blocked
    if needs_review:
        await record_decision(
            workspace_id=ctx.workspace_id, user_id=ctx.user_id, agent=AGENT, step="review",
            summary="The videos are ready for your review. Nothing goes out until you have looked at them.",
            evidence={"projects": [p["project_id"] for p in projects], "rendered": len(rendered)}, outcome="needs_decision",
        )
    return {
        "flow": settings.flow, "ok": not blocked, "steps": [o.public() for o in outcomes], "projects": projects,
        "notes": state.get("notes", []), "voice_checks": state.get("voice_checks", []), "needs_review": needs_review,
        "error": blocked[0].detail if blocked else None,
    }
