"""Runs the automatic video recipes that are due. Called every few minutes by the scheduler; each recipe is run on its own so one
failure never holds the rest. The recipe runs as the member who switched it on, and only while that person is still in the
workspace. The owner's kill switch holds a recipe where it is: it stays due and runs once the switch is released."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app.agents.supervisor.service import assert_generation_allowed
from app.core.scheduler_lock import distributed_job_lock
from app.db.mongo import get_db
from app.shared.activity import record_system
from app.video import automation

logger = logging.getLogger(__name__)

MAX_DUE_PER_TICK = 20


async def _caption_lines(project_ids: list[str], workspace_id: str) -> list[str]:
    lines: list[str] = []
    for doc in await get_db()["video_projects"].find({"id": {"$in": project_ids}, "workspace_id": workspace_id}, {"edit": 1}).to_list(length=50):
        words = [w.get("w", "") for w in (doc.get("edit", {}).get("captions", {}) or {}).get("words", [])]
        text = " ".join(w for w in words if w).strip()
        if text:
            lines.append(text[:600])
    return lines[:40]


async def voice_check_lines(lines: list[str], ctx: Any, recipe: dict) -> str:
    """Remy's verdict on the captions: in_voice, drifts, or off. The real model call; tests replace it."""
    from app.agents.supervisor.service import assert_ai_budget_available
    from app.video.create import generate as G
    from app.video.create import voice as voice_mod

    await assert_ai_budget_available(ctx.workspace_id)
    voice = await voice_mod.load(ctx.workspace_id, recipe.get("brand_id"))
    result, _meta = await G.voice_check(lines, voice.get("context", "") if voice.get("known") else "", language="en")
    return str(result.get("verdict") or "")


async def run_recipe_once(recipe: dict, *, voice_check=voice_check_lines) -> dict[str, Any]:
    from app.pipelines.campaigns.media import _context
    from app.api.v1.video_flows import build_env
    from app.video.flows import runner as RUN

    auto = recipe.get("automation") or {}
    try:
        ctx = await _context(recipe["workspace_id"], auto.get("set_by") or "")
    except PermissionError:
        return await automation.run_one(recipe, run=_gone)

    async def run(r: dict, render: bool) -> dict:
        env = await build_env(ctx)
        item = await RUN.run_steps(env, r["steps"], auto.get("inputs") or {}, render=render, preset="standard")
        await get_db()["video_recipes"].update_one({"id": r["id"]}, {"$inc": {"runs": 1}})
        return {**item, "results": [item]}

    async def checks(r: dict, result: dict) -> list[str]:
        problems = await automation.default_checks(r, result)
        if problems:
            return problems
        project_ids = [p["project_id"] for s in result["steps"] for p in s.get("projects") or [] if p.get("project_id")]
        lines = await _caption_lines(project_ids, r["workspace_id"])
        if not lines:
            return []
        try:
            verdict = await voice_check(lines, ctx, r)
        except Exception as exc:  # noqa: BLE001 - a check that cannot run is not a pass
            logger.warning("Voice check for recipe %s could not run: %s", r.get("id"), exc)
            return ["the voice check could not run, so the video was not approved"]
        return [] if verdict == "in_voice" else ["the captions do not sound like your voice"]

    return await automation.run_one(recipe, run=run, checks=checks)


async def _gone(recipe: dict, render: bool) -> dict:
    raise RuntimeError("the person who set this recipe up is no longer in the workspace")


@distributed_job_lock("run_due_video_recipes", ttl_seconds=240)
async def run_due_video_recipes() -> None:
    now = datetime.now(timezone.utc)
    due = await get_db()["video_recipes"].find({
        "automation.enabled": True, "automation.paused": {"$ne": True}, "automation.next_run_at": {"$lte": now},
    }).to_list(length=MAX_DUE_PER_TICK)
    for recipe in due:
        try:
            await assert_generation_allowed(recipe["workspace_id"])
        except HTTPException:
            await record_system(
                workspace_id=recipe["workspace_id"], key=f"video_recipe_held:{recipe['id']}:{now.strftime('%Y-%m-%d')}", actor_name="Video automation",
                category="content_generated", title=f"{recipe.get('name') or 'A recipe'} is waiting",
                description="Generation is switched off for this workspace, so the recipe did not run. It runs once it is switched back on.",
                status="warning", target_id=recipe["id"], target_type="Video Recipe", target_label=recipe.get("name"), href="/dashboard/pipelines/video",
            )
            continue
        try:
            await run_recipe_once(recipe)
        except Exception as exc:  # noqa: BLE001
            logger.error("Automatic recipe %s failed: %s", recipe.get("id"), exc)
