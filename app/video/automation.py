"""Automatic recipes: a saved recipe that runs by itself, daily or weekly, on inputs the member chose.

Autonomy is a level the member picks per recipe, and the default is the safest one:

* ``draft_and_wait``  the videos are built as editable projects and nothing is rendered. A decision waits for a person.
* ``render_and_wait`` the videos are also rendered and checked. A decision waits for a person.
* ``auto_publish``    the videos are rendered and checked, then scheduled as posts a few minutes ahead (see auto_publish.py). Only an
  owner or admin can choose it, it names the brand and platforms, it has a daily cap, and the decision in the Activity Log can pull
  the posts before they go out.

After every run the checks are made; a failed check stops the recipe, writes a decision for a person to resolve, and tells Odette.
Every automatic step also writes a decision record. The owner's kill switch holds recipes where they are."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from app.db.mongo import brand_profiles, get_db, workspace_members
from app.shared.activity import actors
from app.shared.activity.store import LANE_ACTIVE, upsert_entry

logger = logging.getLogger(__name__)

LEVELS = ("draft_and_wait", "render_and_wait", "auto_publish")
DEFAULT_LEVEL = "draft_and_wait"
FREQUENCIES = {"daily": timedelta(days=1), "weekly": timedelta(days=7)}
SOURCE_KIND = "video_decision"
HREF = "/dashboard/pipelines/video"
NOT_A_LEVEL = "That is not a level this recipe can run at."


class AutomationError(ValueError):
    """A refusal with a plain message for the member."""


def _recipes():
    return get_db()["video_recipes"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def configure(
    recipe_id: str, workspace_id: str, user_id: str, *, enabled: bool, frequency: str = "weekly", autonomy: str = DEFAULT_LEVEL,
    inputs: Optional[dict[str, Any]] = None, now: Optional[datetime] = None, role: Optional[str] = None, publish: Optional[dict] = None,
) -> dict[str, Any]:
    """Turn a recipe's automation on or off, or change it. Switching it on again clears a stop. Choosing auto_publish needs an owner or
    admin (`role`) and the publish settings."""
    if autonomy not in LEVELS:
        raise AutomationError(NOT_A_LEVEL)
    publish_cfg: Optional[dict] = None
    if autonomy == "auto_publish" and enabled:
        from app.video import auto_publish

        try:
            publish_cfg = auto_publish.clean_settings(publish, role)
        except ValueError as exc:
            raise AutomationError(str(exc))
        if not await brand_profiles.find_one({"id": publish_cfg["brand_id"], "workspace_id": workspace_id}):
            raise AutomationError("That brand was not found in this workspace.")
    if frequency not in FREQUENCIES:
        raise AutomationError("A recipe can run daily or weekly.")
    recipe = await _recipes().find_one({"id": recipe_id, "workspace_id": workspace_id})
    if not recipe:
        raise AutomationError("That recipe was not found.")
    if enabled and not (recipe.get("automation") or {}).get("enabled"):
        # Switching one on must fit the plan's video automations; changing one that is already on never counts again.
        from app.shared import entitlements

        await entitlements.require_room(await entitlements.entitlements_for(workspace_id), "video_automations")
    when = now or _now()
    state = {
        "enabled": bool(enabled), "frequency": frequency, "autonomy": autonomy, "inputs": dict(inputs or {}), "paused": False,
        "paused_reason": None, "publish": publish_cfg, "set_by": user_id, "next_run_at": when + FREQUENCIES[frequency] if enabled else None,
        "last_run_at": (recipe.get("automation") or {}).get("last_run_at"),
    }
    await _recipes().update_one({"id": recipe_id}, {"$set": {"automation": state}})
    return state


async def _decision(
    recipe: dict, kind: str, title: str, description: str, *, stamp: str, project_ids: list[str], status: str = "warning",
) -> None:
    """One 'Needs your decision' row in the Activity Log. `kind` is review (videos are ready) or stopped (a check failed)."""
    await upsert_entry({
        "_id": f"{SOURCE_KIND}:{recipe['id']}:{stamp}", "workspace_id": recipe["workspace_id"], "lane": LANE_ACTIVE, "visibility": "workspace",
        "source": {"kind": SOURCE_KIND, "id": f"{recipe['id']}:{stamp}", "type": kind}, "actor": dict(actors.system_actor("Video automation")),
        "category": "recommendation", "title": title, "description": description, "target_id": recipe["id"], "target_type": "Video Recipe",
        "target_label": recipe.get("name"), "href": f"{HREF}?project={project_ids[0]}" if project_ids else HREF, "status": status,
        "metadata": {"recipeId": recipe["id"], "kind": kind, "projectCount": len(project_ids)}, "occurred_at": _now(),
    })


async def _record(recipe: dict, step: str, summary: str, outcome: str, evidence: dict) -> None:
    try:
        from app.agents.runtime.decisions import record_decision

        await record_decision(workspace_id=recipe["workspace_id"], user_id=(recipe.get("automation") or {}).get("set_by") or "system",
                              agent="video", step=step, summary=summary, evidence=evidence, outcome=outcome)
    except Exception as exc:  # noqa: BLE001 - the record is evidence, never a blocker
        logger.warning("Decision record for recipe %s failed: %s", recipe.get("id"), exc)


async def _stop(recipe: dict, reason: str, stamp: str, project_ids: list[str]) -> dict[str, Any]:
    await _recipes().update_one({"id": recipe["id"]}, {"$set": {"automation.paused": True, "automation.paused_reason": reason[:300]}})
    await _decision(recipe, "stopped", f"{recipe.get('name') or 'A recipe'} stopped", f"A check failed: {reason} Nothing was published. "
                    "Open it to fix the problem, then switch the recipe on again.", stamp=stamp, project_ids=project_ids)
    from app.video import activity

    await activity.emit("automation_paused", recipe["workspace_id"], (recipe.get("automation") or {}).get("set_by"), f"{recipe['id']}:{stamp}",
                        recipe_id=recipe["id"], title=recipe.get("name") or "", detail=reason[:300])
    await _record(recipe, "automation.stopped", f"Stopped {recipe.get('name')}: {reason}", "blocked", {"recipe_id": recipe["id"]})
    return {"status": "stopped", "reason": reason}


Checker = Callable[[dict, dict], Awaitable[list[str]]]


async def default_checks(recipe: dict, result: dict) -> list[str]:
    """What must hold before a person is asked to look: every step worked, every video that was asked for was made, and the
    quality report of each rendered video passed. Returns plain reasons; empty means every check passed."""
    problems: list[str] = []
    for n, item in enumerate(result.get("results") or [result], 1):
        for step in item.get("steps") or []:
            if not step.get("ok"):
                problems.append(step.get("error") or f"a step did not finish (item {n})")
            for project in step.get("projects") or []:
                if project.get("error"):
                    problems.append(project["error"])
    if not (result.get("videos") or 0) and not problems:
        problems.append("it made no video")
    return problems


async def _default_publisher(recipe: dict, project_ids: list[str], when: datetime) -> dict[str, Any]:
    from app.video import auto_publish

    return await auto_publish.schedule_video_posts(recipe, project_ids, user_id=(recipe.get("automation") or {}).get("set_by") or "", now=when)


async def _publish(recipe: dict, project_ids: list[str], stamp: str, publisher) -> dict[str, Any]:
    """After every check passed: schedule the posts, then leave a decision that says what was scheduled, what was held and why, and
    lets a person pull the posts before they go out."""
    from app.video import auto_publish

    when = _now()
    out = await publisher(recipe, project_ids, when)
    scheduled, held = out.get("scheduled") or [], out.get("held") or []
    auto = recipe.get("automation") or {}
    await _recipes().update_one({"id": recipe["id"]}, {"$set": {
        "automation.last_run_at": when, "automation.next_run_at": when + FREQUENCIES.get(auto.get("frequency"), FREQUENCIES["weekly"])}})
    name = recipe.get("name") or "A recipe"
    at = (when + timedelta(minutes=auto_publish.UNDO_MINUTES)).strftime("%H:%M")
    if scheduled:
        title = f"{name} scheduled {len(scheduled)} {'post' if len(scheduled) == 1 else 'posts'}"
        text = (f"Going out at about {at} UTC. Pull them before then if you do not want them to go." +
                (f" Held back: {'; '.join(held)}." if held else ""))
        status = "success"
    else:
        title, status = f"{name} made videos but posted nothing", "warning"
        text = f"Nothing was scheduled. {'; '.join(held) if held else 'There was nothing to post.'}."
    await upsert_entry({
        "_id": f"{SOURCE_KIND}:{recipe['id']}:{stamp}", "workspace_id": recipe["workspace_id"], "lane": LANE_ACTIVE, "visibility": "workspace",
        "source": {"kind": SOURCE_KIND, "id": f"{recipe['id']}:{stamp}", "type": "auto_publish"}, "actor": dict(actors.system_actor("Video automation")),
        "category": "recommendation", "title": title, "description": text, "target_id": recipe["id"], "target_type": "Video Recipe",
        "target_label": name, "href": "/dashboard/calendar", "status": status,
        "metadata": {"recipeId": recipe["id"], "kind": "auto_publish", "pieceIds": scheduled, "projectCount": len(project_ids)}, "occurred_at": when,
    })
    await _record(recipe, "automation.published", f"{name} scheduled {len(scheduled)} posts", "ok" if scheduled else "partial",
                  {"recipe_id": recipe["id"], "scheduled": len(scheduled), "held": len(held)})
    return {"status": "published", "scheduled": scheduled, "held": held}


async def run_one(
    recipe: dict, *, run: Callable[[dict, bool], Awaitable[dict]], checks: Checker = default_checks, now: Optional[datetime] = None,
    publisher=None,
) -> dict[str, Any]:
    """One automatic run of one recipe. `run(recipe, render)` does the work and returns the recipe result; `checks` returns the
    reasons it failed. The recipe's next run is set whatever happens, unless a check stopped it."""
    auto = recipe.get("automation") or {}
    when = now or _now()
    stamp = when.strftime("%Y%m%d%H%M")
    level = auto.get("autonomy")
    if level == "auto_publish":
        from app.video import auto_publish

        member = await workspace_members.find_one({"workspace_id": recipe["workspace_id"], "user_id": auto.get("set_by"), "status": {"$ne": "removed"}})
        if not member or not auto_publish.can_enable(member.get("role")):
            # Whoever allowed it is no longer an owner or admin: it falls back to waiting for a person, and says so.
            await _recipes().update_one({"id": recipe["id"]}, {"$set": {"automation.autonomy": "render_and_wait", "automation.publish": None}})
            await _decision(recipe, "stopped", f"{recipe.get('name') or 'A recipe'} no longer publishes by itself",
                            "The person who allowed it is no longer an owner or admin. It now waits for your decision instead.",
                            stamp=f"{stamp}-demoted", project_ids=[])
            recipe = {**recipe, "automation": {**auto, "autonomy": "render_and_wait", "publish": None}}
            auto, level = recipe["automation"], "render_and_wait"
    render = level in ("render_and_wait", "auto_publish")
    try:
        result = await run(recipe, render)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Automatic recipe %s failed: %s", recipe.get("id"), exc)
        return await _stop(recipe, "the recipe could not run just now.", stamp, [])
    project_ids = [p["project_id"] for item in (result.get("results") or [result]) for s in item.get("steps") or [] for p in s.get("projects") or []
                   if p.get("project_id")]
    problems = await checks(recipe, result)
    if problems:
        return await _stop(recipe, "; ".join(dict.fromkeys(problems))[:300], stamp, project_ids)
    await _recipes().update_one({"id": recipe["id"]}, {"$set": {
        "automation.last_run_at": when, "automation.next_run_at": when + FREQUENCIES.get(auto.get("frequency"), FREQUENCIES["weekly"])}})
    count = len(project_ids)
    if level == "auto_publish":
        return await _publish(recipe, project_ids, stamp, publisher or _default_publisher)
    noun = "video is" if count == 1 else "videos are"
    await _decision(recipe, "review", f"{recipe.get('name') or 'A recipe'} made {count} {'video' if count == 1 else 'videos'}",
                    f"{count} {noun} ready for you to look at. Nothing was published." + ("" if render else " They are drafts: open one to render it."),
                    stamp=stamp, project_ids=project_ids, status="success")
    await _record(recipe, "automation.ready", f"{recipe.get('name')} made {count} for review", "ok", {"recipe_id": recipe["id"], "projects": count})
    return {"status": "ready", "projects": project_ids}


async def apply_decision(recipe_id: str, workspace_id: str, decision: str, metadata: Optional[dict] = None) -> None:
    """A person's answer to a decision row. Accepting a stopped recipe switches it back on; dismissing leaves it stopped. Dismissing the
    row of an automatic publish pulls its scheduled posts."""
    if (metadata or {}).get("kind") == "auto_publish":
        if decision == "dismiss":
            from app.video import auto_publish

            await auto_publish.pull(workspace_id, list((metadata or {}).get("pieceIds") or []))
        return
    recipe = await _recipes().find_one({"id": recipe_id, "workspace_id": workspace_id})
    if not recipe or decision != "accept" or not (recipe.get("automation") or {}).get("paused"):
        return
    auto = recipe["automation"]
    await _recipes().update_one({"id": recipe_id}, {"$set": {
        "automation.paused": False, "automation.paused_reason": None, "automation.next_run_at": _now() + FREQUENCIES.get(auto.get("frequency"), FREQUENCIES["weekly"])}})
