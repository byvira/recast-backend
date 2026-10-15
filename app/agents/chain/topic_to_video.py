"""One goal across four content types: a topic becomes a post, the post is voiced, a cover picture is made, and a narrated video is
built from them and rendered.

The chain is a plan of steps run by the shared plan executor, so it has the budget guard, the decision log and the run layer the
other agents have. Each step uses a tool from the cross-type toolkit (which checks permission, the kill switch and the cost class),
so the chain never reaches around them. What each step makes is linked to what it was made from (`derived_from`), and the whole
run is kept as a `chain_runs` record, so any item can say where it came from.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from app.agents.chain import lineage
from app.agents.runtime import Budget, Outcome, PlanExecutor, Step, ToolRegistry, ToolSpec, record_decision
from app.db.mongo import get_db

logger = logging.getLogger(__name__)

AGENT = "chain"
CHAIN = "topic_to_video"


class ChainSettings(BaseModel):
    topic: str = Field(..., min_length=3, max_length=1500)
    brand_id: str = Field(..., max_length=64)
    platform: str = Field(default="linkedin", max_length=24)
    language: Optional[str] = Field(default=None, max_length=12)
    size: Literal["vertical", "square", "landscape", "portrait"] = "vertical"
    cover: bool = True
    render: bool = True
    preset: Literal["standard", "high", "small"] = "standard"


def _toolkit():
    from app.agents.runtime import toolkit

    return toolkit


async def _use(state: dict, name: str, **kwargs: Any) -> Any:
    """One toolkit tool. A refusal or failure becomes an exception with the tool's own plain message, which the plan executor turns
    into a failed step."""
    result = await _toolkit().use(name, ctx=state["ctx"], reporter=state.get("reporter"), **kwargs)
    if not result.ok:
        raise RuntimeError(result.error or "That step did not finish.")
    return result.value


# ── steps ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────

async def _write(*, state: dict, settings: dict) -> Any:
    payload = {
        "source_type": "topic", "content": settings["topic"], "platforms": [settings["platform"]], "brand_id": settings["brand_id"],
        "language": settings.get("language"),
    }
    out = await _use(state, "text.write", payload=payload)
    pieces = getattr(out, "pieces", None) or (out.get("pieces") if isinstance(out, dict) else None) or []
    if not pieces:
        raise RuntimeError("No post could be written for that topic.")
    first = pieces[0]
    piece_id = getattr(first, "piece_id", None) or (first.get("piece_id") if isinstance(first, dict) else None)
    content = getattr(first, "content", None) or (first.get("content") if isinstance(first, dict) else "")
    if not piece_id or not (content or "").strip():
        raise RuntimeError("The post was written but could not be saved, so it cannot be voiced.")
    state["piece_id"], state["text"] = piece_id, content
    return {"piece_id": piece_id, "words": len(content.split())}


async def _narrate(*, state: dict, settings: dict) -> Any:
    request = {"title": settings["topic"][:80], "brand_id": settings["brand_id"], "source_piece_id": state["piece_id"]}
    if settings.get("language"):
        request["language"] = settings["language"]
    out = await _use(state, "audio.narrate", request=request)
    state["audio_id"], state["voice_media_id"] = out["asset_id"], out.get("media_id")
    await lineage.link("audio", state["audio_id"], state["ctx"].workspace_id, [lineage.ref("piece", state["piece_id"])])
    return {"asset_id": state["audio_id"], "origin": out.get("origin", "ai")}


async def _cover(*, state: dict, settings: dict) -> Any:
    out = await _use(state, "image.generate", topic=settings["topic"], brand_id=settings["brand_id"])
    state["cover_media_id"] = out.get("media_id")
    await lineage.link("image", state["cover_media_id"], state["ctx"].workspace_id, [lineage.ref("piece", state["piece_id"])])
    return {"media_id": state["cover_media_id"], "origin": out.get("origin", "ai"), "qa_flagged": out.get("qa_flagged")}


async def _seconds_of(workspace_id: str, media_id: Optional[str]) -> float:
    if not media_id:
        return 0.0
    row = await get_db()["media_assets"].find_one({"id": media_id, "workspace_id": workspace_id}, {"duration_s": 1})
    return float((row or {}).get("duration_s") or 0.0)


async def build_narrated_project(
    ctx: Any, *, title: str, text: str, voice_media_id: str, cover_media_id: Optional[str], brand_id: Optional[str], size: str,
    created_from: dict, parents: list, reporter: Any = None,
) -> tuple[str, float]:
    """An editable project from a narration: the cover (or the brand colour) held for the length of the voice, the voice on the audio
    track, and the words as captions. Used by the topic-to-video chain and by campaign video. Returns (project id, seconds)."""
    from app.video import edl
    from app.video.flows import registry as R
    from app.video.flows import runner
    from app.video.narrate import plan as P

    seconds = await _seconds_of(ctx.workspace_id, voice_media_id)
    if seconds <= 0:
        raise RuntimeError("The narration has no length, so a video cannot be built from it.")
    env = R.Env(workspace_id=ctx.workspace_id, user_id=ctx.user_id, reporter=reporter)
    brand = await R._brand(env, brand_id)
    accent, background = R._colors(brand)
    fps = {"num": 30, "den": 1}
    dur = max(1, edl.seconds_to_frames(seconds, fps))
    edit = edl.blank(size, fps)
    edit["background"] = background
    edit["video"] = [{"id": "c1", "media_id": cover_media_id, "at_f": 0, "dur_f": dur, "src_in_f": 0, "speed": 1.0, "fit": "cover", "fade_in_f": 0,
                      "fade_out_f": 0, "audio": False, "gain_db": 0.0, "motion": True}] if cover_media_id else []
    edit["audio"] = [{"id": "v1", "media_id": voice_media_id, "at_f": 0, "dur_f": dur, "src_in_f": 0, "speed": 1.0, "gain_db": 0.0,
                      "role": "voice", "fade_in_f": 0, "fade_out_f": 0, "duck": False}]
    words = P.caption_words(text, 0, int(seconds * 1000))
    edit["captions"] = {"enabled": bool(words), "accent": accent, "words": words}
    edit["duration_f"] = dur
    edit = edl.validate(edit)
    spec = {
        "title": title[:80], "edit": edit, "notes": [] if cover_media_id else ["No cover picture was made, so the brand colour is shown."],
        "created_from": {**created_from, "derived_from": [p for p in parents if p]},
    }
    made = await runner.create_projects(env, [spec])
    project_id = made[0]["project_id"]
    await lineage.link("project", project_id, ctx.workspace_id, parents)
    return project_id, seconds


async def _assemble(*, state: dict, settings: dict) -> Any:
    """The chain's own step: the project from the narration and the cover this run made."""
    cover = state.get("cover_media_id")
    parents = [lineage.ref("piece", state["piece_id"]), lineage.ref("audio", state["audio_id"]), lineage.ref("image", cover)]
    project_id, seconds = await build_narrated_project(
        state["ctx"], title=settings["topic"], text=state["text"], voice_media_id=state["voice_media_id"], cover_media_id=cover,
        brand_id=settings["brand_id"], size=settings["size"], parents=parents, reporter=state.get("reporter"),
        created_from={"kind": "chain", "chain": CHAIN, "chain_id": state["chain_id"]},
    )
    state["project_id"] = project_id
    return {"project_id": project_id, "seconds": round(seconds, 1)}


async def _render(*, state: dict, settings: dict) -> Any:
    out = await _use(state, "video.render", project_id=state["project_id"], preset=settings["preset"])
    media_id = (out or {}).get("media_id")
    state["video_media_id"] = media_id
    ctx = state["ctx"]
    await lineage.link("media", media_id, ctx.workspace_id, [lineage.ref("project", state["project_id"]),
                                                           lineage.ref("audio", state.get("audio_id")),
                                                           lineage.ref("image", state.get("cover_media_id"))])
    return {"media_id": media_id}


REGISTRY = ToolRegistry(AGENT)
for _spec, _fn in (
    (ToolSpec("write", "Write the post for the topic in the brand's voice", cost="llm", timeout_s=300), _write),
    (ToolSpec("narrate", "Voice the post into a recording", cost="provider", timeout_s=900), _narrate),
    (ToolSpec("cover", "Make a cover picture for the topic", cost="provider", timeout_s=300), _cover),
    (ToolSpec("assemble", "Build an editable video project from the narration and the cover", cost="free", timeout_s=120), _assemble),
    (ToolSpec("render", "Render the project and wait for the finished video", cost="worker", timeout_s=7800), _render),
):
    REGISTRY.register(_spec, _fn)


#: Steps the chain cannot go on without. The cover and the render are optional: the project is kept either way.
REQUIRED = {"write", "narrate", "assemble"}


def build_plan(settings: ChainSettings) -> list[Step]:
    """Fixed rules, not a model: the same goal always gives the same plan. The cover and the render are optional; a failed cover
    leaves the brand colour behind the words, and a failed render leaves the project to open and render again."""
    args = {"settings": settings.model_dump()}
    steps = [Step("write", "Writing the post", args), Step("narrate", "Voicing the post", args)]
    if settings.cover:
        steps.append(Step("cover", "Making the cover picture", args, required=False))
    steps.append(Step("assemble", "Building the video", args))
    if settings.render:
        steps.append(Step("render", "Rendering the video", args, required=False))
    return steps


async def run_chain(ctx: Any, payload: Any, reporter: Any) -> dict:
    """The job action body: plan, run, keep the run record, and hand back what was made."""
    settings: ChainSettings = payload.settings
    chain_id = str(uuid.uuid4())
    state: dict[str, Any] = {"ctx": ctx, "reporter": reporter, "chain_id": chain_id}

    async def before(label: str, index: int, total: int) -> None:
        await reporter.step(label)

    executor = PlanExecutor(
        REGISTRY, workspace_id=ctx.workspace_id, user_id=ctx.user_id, run_id=chain_id, before_step=before,
        budget=Budget(llm_calls=4, provider_calls=2 if settings.cover else 1, worker_jobs=1 if settings.render else 0),
        subject={"chain": CHAIN, "topic": settings.topic[:120]},
    )
    outcomes: list[Outcome] = await executor.run(build_plan(settings), state)
    failed = [o for o in outcomes if o.status in ("failed", "stopped")]
    blocking = [o for o in failed if o.tool in REQUIRED or o.status == "stopped"]
    made = {k: state.get(k) for k in ("piece_id", "audio_id", "cover_media_id", "project_id", "video_media_id")}
    record = {
        "id": chain_id, "workspace_id": ctx.workspace_id, "user_id": ctx.user_id, "chain": CHAIN, "settings": settings.model_dump(),
        "steps": [o.public() for o in outcomes], "made": made, "ok": not blocking, "partial": bool(failed) and not blocking,
        "created_at": datetime.now(timezone.utc),
    }
    try:
        await get_db()["chain_runs"].insert_one(dict(record))
    except Exception as exc:  # noqa: BLE001 - the work is done; only its record is missing
        logger.warning("Could not keep the chain run record %s: %s", chain_id, exc)
    if blocking:
        await record_decision(
            workspace_id=ctx.workspace_id, user_id=ctx.user_id, agent=AGENT, step="chain", run_id=chain_id, outcome="needs_decision",
            summary=f"The video chain stopped at: {blocking[0].label.lower()}. What was made so far is kept.", evidence={"made": made},
        )
    elif failed:
        await record_decision(
            workspace_id=ctx.workspace_id, user_id=ctx.user_id, agent=AGENT, step="chain", run_id=chain_id, outcome="needs_decision",
            summary=f"The video was made, but this step did not finish: {failed[0].label.lower()}. It can be done again.",
            evidence={"made": made},
        )
    return {"chain_id": chain_id, "ok": not blocking, "partial": record["partial"], "steps": record["steps"], **made}
