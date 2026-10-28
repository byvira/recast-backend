"""Nodes of the image agent.

Each node runs one registered tool from the existing image pipeline (`app.pipelines.media.image_generation`), so the gates,
the provider chain and the checks are exactly the ones that were already there; what changes is that they now run as a planned,
logged, traced workflow with one place for budgets and decisions. The functions are looked up at call time, so nothing here
holds a stale copy of them.

Modes: "prompt" returns the picture's bytes (the caller composes and saves it); "topic" also checks the picture against the
brand, stamps the logo, stores it and records the library item.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.agents.base import BaseAgentState
from app.agents.runtime import Budget, BudgetExceeded, PlanError, ToolRegistry, ToolSpec, record_decision

logger = logging.getLogger(__name__)

AGENT = "image"


def _ig():
    from app.pipelines.media import image_generation as ig

    return ig


# ── tools: the existing functions, wrapped so the registry decides what a plan may call ──────────────────────────────────

async def _tool_build_prompt(*, topic: str, brand: dict) -> str:
    return _ig()._build_raw_prompt(topic, brand)


async def _tool_gate(*, raw_prompt: str, brand: dict, avoid: Any, feedback: str) -> dict:
    """One pass of the gates on a prompt: polish, safety, anti-generic, brand fit. The same checks, in the same order, as
    `_run_gates`; the graph decides what a failure means."""
    ig = _ig()
    polished = await ig._polish_prompt(raw_prompt + ig._avoid_clause(avoid) + feedback)
    # The two model checks run at the same time when the brand-fit answer can matter (the local generic check passes), instead
    # of one after the other. The results are still judged in the same order, so every decision is the one it always was.
    specific = ig._anti_generic_gate(polished)
    if specific:
        verdict, fits = await asyncio.gather(ig._safety_verdict(polished), ig._brand_fit_gate(polished, brand))
    else:
        verdict, fits = await ig._safety_verdict(polished), True
    if verdict != "safe":
        return {"polished": polished, "verdict": verdict, "passed": False, "reason": "safety"}
    if not specific:
        return {"polished": polished, "verdict": verdict, "passed": False, "reason": "generic",
                "feedback": " The last attempt was too generic. Make the scene specific and distinctive."}
    if not fits:
        return {"polished": polished, "verdict": verdict, "passed": False, "reason": "brand_fit",
                "feedback": " The last attempt did not match the brand's visual style. Follow the brand style more closely."}
    return {"polished": polished, "verdict": verdict, "passed": True}


async def _tool_generate(*, prompt: str) -> dict:
    ig = _ig()
    data = await ig._generate_text_free(prompt)
    # the reason is read here, in the same task that set it
    return {"bytes": data, "failure": None if data else ig.last_failure_reason()}


async def _tool_qa(*, image: bytes, brand: dict) -> dict:
    flagged, reason = await _ig()._qa_gate(image, brand)
    return {"flagged": flagged, "reason": reason}


async def _tool_publish(*, image: bytes, brand: dict, workspace_id: str, user_id: str, flagged: bool, reason: Any) -> Any:
    """The logo stamp, the upload and the library item: what `generate_brand_image` has always done after the picture exists."""
    from app.db.mongo import media_assets
    from app.models.media import MediaAsset, MediaKind, MediaSource
    from app.shared.heavy_work import run_heavy
    from app.shared.storage import ContentType as UploadContentType, upload_file

    ig = _ig()
    logo = await ig._fetch_logo(((brand.get("visual_identity") or {}).get("logo_url")) or "")
    if logo:
        image = await run_heavy(ig.stamp_logo, image, logo)
    from app.agents.content_guard.media import assert_image_ok

    await assert_image_ok(image, "image/jpeg", workspace_id=workspace_id, where="image agent")
    url = await upload_file(image, UploadContentType.IMAGE, user_id)
    asset = MediaAsset(
        id=str(uuid4()), workspace_id=workspace_id, kind=MediaKind.IMAGE, url=url, mime_type="image/jpeg",
        width=ig.IMAGE_SIZE[0], height=ig.IMAGE_SIZE[1], source=MediaSource.AI_GENERATED, created_by=user_id,
        created_at=datetime.now(timezone.utc), qa_flagged=flagged, qa_flag_reason=reason,
    )
    await media_assets.insert_one(asset.model_dump())
    return asset


REGISTRY = ToolRegistry(AGENT)
REGISTRY.register(ToolSpec("build_prompt", "Write the raw picture prompt from a topic and the brand look", cost="free",
                           inputs=("topic", "brand"), outputs=("raw_prompt",)), _tool_build_prompt)
REGISTRY.register(ToolSpec("gate", "Polish the prompt and run the safety, anti-generic and brand-fit checks", cost="llm", timeout_s=90,
                           inputs=("raw_prompt", "brand"), outputs=("polished", "verdict")), _tool_gate)
REGISTRY.register(ToolSpec("generate", "Make the picture through the provider chain, with no writing in it", cost="provider",
                           timeout_s=240, inputs=("prompt",), outputs=("bytes",)), _tool_generate)
REGISTRY.register(ToolSpec("qa", "Check the finished picture against the brand's visual style", cost="llm", timeout_s=60,
                           inputs=("image", "brand"), outputs=("flagged", "reason")), _tool_qa)
REGISTRY.register(ToolSpec("publish", "Stamp the logo, store the picture and record the library item", cost="free", timeout_s=120,
                           inputs=("image",), outputs=("asset",)), _tool_publish)


def _raw(state: BaseAgentState) -> dict:
    raw = state["raw_input"]
    return raw if isinstance(raw, dict) else {}


def _io(state: BaseAgentState) -> dict:
    return state["intermediate_outputs"]


# ── nodes ────────────────────────────────────────────────────────────────────────────────────────────────────────────────

async def plan_node(state: BaseAgentState) -> dict:
    mode = _raw(state).get("mode", "prompt")
    steps = ["build_prompt", "gate", "generate"] if mode == "topic" else ["gate", "generate"]
    if mode == "topic":
        steps += ["qa", "publish"]
    try:
        plan = REGISTRY.validate_plan(steps)
    except PlanError as exc:
        return {"plan": [], "errors": [*state["errors"], str(exc)], "status": "failed", "current_step": "finish"}
    return {"plan": plan, "current_step": plan[0], "intermediate_outputs": {**_io(state), "budget": Budget(llm_calls=8, provider_calls=3).snapshot()}}


async def build_prompt_node(state: BaseAgentState) -> dict:
    raw = _raw(state)
    res = await REGISTRY.run("build_prompt", topic=raw.get("topic", ""), brand=state["brand"])
    if not res.ok:
        return {"errors": [*state["errors"], res.error or "build_prompt failed"], "status": "failed", "current_step": "finish"}
    return {"intermediate_outputs": {**_io(state), "raw_prompt": res.value}, "current_step": "gate"}


async def gate_node(state: BaseAgentState) -> dict:
    """One attempt at the gates. The loop and its stop rules are in the graph's edges and in `settle_node`."""
    io, raw = _io(state), _raw(state)
    ig = _ig()
    raw_prompt = io.get("raw_prompt") or raw.get("prompt", "")
    attempt = int(io.get("gate_attempt", 0))
    res = await REGISTRY.run("gate", raw_prompt=raw_prompt, brand=state["brand"], avoid=raw.get("avoid"),
                             feedback=io.get("gate_feedback", ""))
    new = {**io, "gate_attempt": attempt + 1}
    if not res.ok:
        new.update({"verdict": "error", "gate_error": res.error})
    else:
        out = res.value
        new["verdict"] = out["verdict"]
        if out["verdict"] == "safe":
            new["safe_prompt"] = out["polished"]
        if out["passed"]:
            new["final_prompt"] = ig.enforce_no_text(out["polished"])
        elif out.get("feedback"):
            new["gate_feedback"] = out["feedback"]
        new["gate_log"] = [*io.get("gate_log", []), {"attempt": attempt + 1, "passed": out["passed"], "reason": out.get("reason")}]
    return {"intermediate_outputs": new, "current_step": "gate",
            "tool_calls": [*state["tool_calls"], {"node": "gate", "attempt": attempt + 1}]}


async def settle_node(state: BaseAgentState) -> dict:
    """The gates are done. A prompt that was safe but judged generic or off style is still used, because the alternative is a flat
    card where a picture was asked for; one that never passed the safety check is a hard stop."""
    io = _io(state)
    if io.get("final_prompt"):
        return {}
    if io.get("safe_prompt"):
        return {"intermediate_outputs": {**io, "final_prompt": _ig().enforce_no_text(io["safe_prompt"]), "style_gate_waived": True}}
    message = ("The safety check could not run, so no picture was made." if io.get("verdict") == "error"
               else "The picture description did not pass the safety check.")
    return {"intermediate_outputs": {**io, "failure": message}, "status": "failed", "current_step": "finish"}


async def generate_node(state: BaseAgentState) -> dict:
    io = _io(state)
    res = await REGISTRY.run("generate", prompt=io["final_prompt"])
    if not res.ok:
        return {"intermediate_outputs": {**io, "failure": "The picture could not be made because of an unexpected error."},
                "status": "failed", "current_step": "finish", "errors": [*state["errors"], res.error or ""]}
    data = res.value["bytes"]
    new = {**io, "image": data}
    if not data:
        new["failure"] = res.value.get("failure") or "No picture was made."
    return {"intermediate_outputs": new, "current_step": "qa" if data else "finish",
            "tool_calls": [*state["tool_calls"], {"node": "generate", "tool": "provider chain"}]}


async def qa_node(state: BaseAgentState) -> dict:
    io = _io(state)
    res = await REGISTRY.run("qa", image=io["image"], brand=state["brand"])
    flagged, reason = (res.value["flagged"], res.value["reason"]) if res.ok else (False, None)
    return {"intermediate_outputs": {**io, "qa_flagged": flagged, "qa_reason": reason},
            "quality_scores": {"brand_fit": 0.4 if flagged else 1.0}, "current_step": "publish"}


async def publish_node(state: BaseAgentState) -> dict:
    io, raw = _io(state), _raw(state)
    res = await REGISTRY.run(
        "publish", image=io["image"], brand=state["brand"], workspace_id=raw["workspace_id"], user_id=raw["user_id"],
        flagged=bool(io.get("qa_flagged")), reason=io.get("qa_reason"),
    )
    new = {**io, "asset": res.value if res.ok else None}
    if not res.ok:
        logger.error("image publish failed for workspace %s: %s", raw.get("workspace_id"), res.error)
    return {"intermediate_outputs": new, "current_step": "finish"}


async def finish_node(state: BaseAgentState) -> dict:
    io, raw = _io(state), _raw(state)
    ok = bool(io.get("image")) and (raw.get("mode") != "topic" or io.get("asset") is not None)
    outputs = {
        "image": io.get("image"), "asset": io.get("asset"), "failure": None if ok else (io.get("failure") or "No picture was made."),
        "polished_prompt": io.get("final_prompt"), "qa_flagged": io.get("qa_flagged", False), "gate_log": io.get("gate_log", []),
    }
    workspace_id = raw.get("workspace_id")
    if workspace_id:
        await record_decision(
            workspace_id=workspace_id, user_id=raw.get("user_id"), agent=AGENT, step="generate_picture",
            summary="Made a picture." if ok else f"No picture was made: {outputs['failure']}",
            evidence={"mode": raw.get("mode"), "gates": outputs["gate_log"], "style_gate_waived": bool(io.get("style_gate_waived")),
                      "qa_flagged": outputs["qa_flagged"], "plan": state["plan"]},
            outcome="ok" if ok else "failed",
        )
    return {"outputs": outputs, "status": "completed" if ok else "failed", "completed_at": datetime.now(timezone.utc).isoformat()}
