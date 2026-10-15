"""One-click flows, saved recipes and batch runs. Each is one run in the activity dock; each video it makes is an ordinary editable
project that is rendered with the worker's quality gate. Nothing here makes a video the member has not asked for."""
import logging
from datetime import datetime, timezone
from typing import Any, Literal, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.db.mongo import get_db
from app.shared import pipeline_runs
from app.video.flows import registry as R
from app.video.flows import runner as RUN

logger = logging.getLogger(__name__)


async def no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(dependencies=[Depends(no_store)])


def _recipes():
    return get_db()["video_recipes"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def build_env(ctx: WorkspaceContext, reporter: Any = None) -> R.Env:
    """The flows' leaf calls wired to the member's own voice, the storage and the stock libraries."""
    from app.api.v1.video_narrate import _voice_tools
    from app.shared.storage import ContentType as UploadContentType, upload_file_detailed
    from app.video.stock import providers, rank
    from app.video.stock import service as stock_service

    make_speak, _store, _forget, _settings = await _voice_tools(ctx)

    async def store_audio(data: bytes) -> dict:
        return await upload_file_detailed(data, UploadContentType.AUDIO, ctx.user_id, filename="narration.mp3")

    async def stock_search(query: str):
        res = await providers.search(query)
        ranked = rank.rank(res["clips"], query, want="tall", min_seconds=3.0, query=query)
        return ranked[:1]

    async def stock_use(clip: dict, query: str):
        media = await stock_service.use_clip(workspace_id=ctx.workspace_id, user_id=ctx.user_id, provider=clip["provider"], clip_id=clip["id"], query=query)
        return media.id

    return R.Env(workspace_id=ctx.workspace_id, user_id=ctx.user_id, reporter=reporter, speak=make_speak(None), store_audio=store_audio,
                 stock_search=stock_search, stock_use=stock_use)


@router.get("/catalog")
@limiter.limit("120/minute")
async def catalog(request: Request, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    return {"flows": R.catalog()}


class RunBody(BaseModel):
    flow: str
    params: dict[str, Any] = Field(default_factory=dict)
    render: bool = True
    preset: Literal["standard", "high", "small"] = "standard"


async def _start(ctx: WorkspaceContext, title: str, active_key: str, steps_total: int, work_fn) -> dict[str, Any]:
    try:
        await pipeline_runs.assert_capacity(ctx.workspace_id)
        run = await pipeline_runs.create_run(
            workspace_id=ctx.workspace_id, user_id=ctx.user_id, kind="video", title=title, steps_total=steps_total, ref={},
            href="/dashboard/pipelines/video", active_key=active_key,
        )
    except pipeline_runs.RunAlreadyActive as exc:
        return {"run": pipeline_runs.public(exc.existing), "started": False}

    async def work(row: dict) -> dict:
        reporter = pipeline_runs.StepReporter(row["id"])
        result = await work_fn(reporter)
        return {"data": result, "href": "/dashboard/pipelines/video"}

    pipeline_runs.start(run, work, interrupt_on_cancel=True)
    return {"run": pipeline_runs.public(run), "started": True}


def _check_flow(flow: str, params: dict[str, Any]) -> None:
    spec = R.FLOWS.get(flow)
    if spec is None:
        raise HTTPException(status_code=404, detail="That flow does not exist.")
    missing = [k for k in spec["needs"] if not params.get(k)]
    if missing:
        raise HTTPException(status_code=400, detail=f"This flow needs: {', '.join(missing)}.")


@router.post("/run", status_code=202)
@limiter.limit("10/minute")
async def run_one(request: Request, body: RunBody, ctx: WorkspaceContext = Depends(require("create_content"))) -> dict[str, Any]:
    _check_flow(body.flow, body.params)
    label = R.FLOWS[body.flow]["label"]

    async def work(reporter):
        env = await build_env(ctx, reporter)
        return await RUN.run_flow(env, body.flow, body.params, render=body.render, preset=body.preset)

    return await _start(ctx, label, f"video-flow:{body.flow}:{uuid4().hex[:8]}", 40, work)


class BatchBody(BaseModel):
    flow: str
    items: list[dict[str, Any]] = Field(..., min_length=1, max_length=RUN.MAX_BATCH)
    shared: dict[str, Any] = Field(default_factory=dict)
    render: bool = True
    preset: Literal["standard", "high", "small"] = "standard"


@router.post("/batch", status_code=202)
@limiter.limit("5/minute")
async def run_batch(request: Request, body: BatchBody, ctx: WorkspaceContext = Depends(require("create_content"))) -> dict[str, Any]:
    if body.flow not in R.FLOWS:
        raise HTTPException(status_code=404, detail="That flow does not exist.")

    async def work(reporter):
        env = await build_env(ctx, reporter)
        return await RUN.run_batch(env, body.flow, body.items, render=body.render, preset=body.preset, shared=body.shared)

    return await _start(ctx, f"{R.FLOWS[body.flow]['label']} for {len(body.items)} items", f"video-batch:{uuid4().hex[:8]}", 20 * len(body.items), work)


class RecipeStep(BaseModel):
    flow: str
    params: dict[str, Any] = Field(default_factory=dict)


class RecipeBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    steps: list[RecipeStep] = Field(..., min_length=1, max_length=RUN.MAX_STEPS)


def _auto(a: Any) -> Any:
    if not a:
        return None
    out = {k: a.get(k) for k in ("enabled", "frequency", "autonomy", "paused", "paused_reason", "publish")}
    for k in ("next_run_at", "last_run_at"):
        v = a.get(k)
        out[k] = v.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z") if v else None
    return out


def _public(r: dict) -> dict:
    return {"id": r["id"], "name": r["name"], "steps": r["steps"], "runs": r.get("runs", 0), "automation": _auto(r.get("automation")), "created_at": r["created_at"].replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")}


@router.post("/recipes", status_code=201)
@limiter.limit("30/minute")
async def save_recipe(request: Request, body: RecipeBody, ctx: WorkspaceContext = Depends(require("create_content"))) -> dict[str, Any]:
    for s in body.steps:
        if s.flow not in R.FLOWS:
            raise HTTPException(status_code=400, detail=f"{s.flow} is not a flow.")
    if await _recipes().count_documents({"workspace_id": ctx.workspace_id}) >= 50:
        raise HTTPException(status_code=409, detail="A workspace can keep 50 recipes. Remove one first.")
    doc = {"id": str(uuid4()), "workspace_id": ctx.workspace_id, "user_id": ctx.user_id, "name": body.name.strip(),
           "steps": [s.model_dump() for s in body.steps], "runs": 0, "created_at": _now()}
    await _recipes().insert_one(dict(doc))
    return _public(doc)


@router.get("/recipes")
@limiter.limit("120/minute")
async def list_recipes(request: Request, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    rows = await _recipes().find({"workspace_id": ctx.workspace_id}).sort("created_at", -1).to_list(50)
    return {"recipes": [_public(r) for r in rows]}


@router.delete("/recipes/{recipe_id}", status_code=204)
@limiter.limit("30/minute")
async def delete_recipe(request: Request, recipe_id: str, ctx: WorkspaceContext = Depends(require("create_content"))) -> Response:
    res = await _recipes().delete_one({"id": recipe_id, "workspace_id": ctx.workspace_id})
    if not res.deleted_count:
        raise HTTPException(status_code=404, detail="That recipe was not found.")
    return Response(status_code=204)


class AutomationBody(BaseModel):
    enabled: bool
    frequency: Literal["daily", "weekly"] = "weekly"
    autonomy: str = "draft_and_wait"
    inputs: dict[str, Any] = Field(default_factory=dict)
    publish: Optional[dict[str, Any]] = None


@router.put("/recipes/{recipe_id}/automation")
@limiter.limit("20/minute")
async def set_recipe_automation(
    request: Request, recipe_id: str, body: AutomationBody, ctx: WorkspaceContext = Depends(require("create_content")),
) -> dict[str, Any]:
    """Make a recipe run by itself, daily or weekly. It builds the videos and waits for a person; it never publishes, and a level that
    would is refused."""
    from app.video import automation

    try:
        return await automation.configure(
            recipe_id, ctx.workspace_id, ctx.user_id, enabled=body.enabled, frequency=body.frequency, autonomy=body.autonomy, inputs=body.inputs,
            role=ctx.role, publish=body.publish,
        )
    except automation.AutomationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


class RecipeRunBody(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)
    inputs_list: list[dict[str, Any]] = Field(default_factory=list, max_length=RUN.MAX_BATCH)
    render: bool = True
    preset: Literal["standard", "high", "small"] = "standard"


@router.post("/recipes/{recipe_id}/run", status_code=202)
@limiter.limit("5/minute")
async def run_recipe(request: Request, recipe_id: str, body: RecipeRunBody, ctx: WorkspaceContext = Depends(require("create_content"))) -> dict[str, Any]:
    """Run a recipe once with `inputs`, or over many files with `inputs_list` (one pass of every step per file, one summary)."""
    recipe = await _recipes().find_one({"id": recipe_id, "workspace_id": ctx.workspace_id})
    if not recipe:
        raise HTTPException(status_code=404, detail="That recipe was not found.")
    runs = body.inputs_list or [body.inputs]

    async def work(reporter):
        env = await build_env(ctx, reporter)
        results = []
        for n, inputs in enumerate(runs, 1):
            try:
                results.append({"index": n - 1, "input": inputs, **await RUN.run_steps(env, recipe["steps"], inputs, render=body.render, preset=body.preset)})
            except Exception as exc:  # noqa: BLE001 - one file never stops the rest
                logger.warning("Recipe %s item %s failed: %s", recipe_id, n, exc)
                results.append({"index": n - 1, "input": inputs, "ok": False, "steps": [], "videos": 0, "error": "This item could not be processed."})
        await _recipes().update_one({"id": recipe_id}, {"$inc": {"runs": 1}})
        done = sum(1 for r in results if r["ok"])
        return {"recipe": recipe["name"], "total": len(results), "succeeded": done, "failed": len(results) - done, "results": results}

    return await _start(ctx, f"Recipe: {recipe['name']}", f"video-recipe:{recipe_id}:{uuid4().hex[:6]}", 40 * len(runs), work)
