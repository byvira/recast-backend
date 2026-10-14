"""Running flows: make the projects a flow built, render each one on the worker with its quality gate, and keep going when one item
fails. A recipe is a named list of flows; a batch is one flow over many inputs. Both give one summary, and a failure is a line in it,
never a stop."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from app.video import service
from app.video.flows import registry as R

logger = logging.getLogger(__name__)

MAX_BATCH = 20
MAX_STEPS = 8


async def create_projects(env: R.Env, specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from app.api.v1.video_projects import _projects, _write_version

    out = []
    for spec in specs:
        now = datetime.now(timezone.utc)
        doc = {
            "id": str(uuid.uuid4()), "workspace_id": env.workspace_id, "title": spec["title"][:200] or "Untitled video", "edit": spec["edit"],
            "initial_edit": spec["edit"], "version": 1, "status": "draft", "created_from": spec["created_from"], "created_by": env.user_id,
            "created_at": now, "updated_at": now, "last_render": None,
        }
        await _projects().insert_one(dict(doc))
        await _write_version(doc, "save", "Start", env.user_id)
        out.append({"project_id": doc["id"], "title": doc["title"], "notes": list(spec.get("notes") or [])})
    return out


async def run_flow(env: R.Env, flow: str, params: dict[str, Any], *, render: bool = True, preset: str = "standard") -> dict[str, Any]:
    """One flow, start to finish. Returns {"flow", "ok", "projects": [{project_id, title, media_id | error, notes}], "notes", "error"}.
    A flow that cannot start returns ok False with a plain message; a render that fails leaves its project in place."""
    spec = R.FLOWS.get(flow)
    if spec is None:
        return {"flow": flow, "ok": False, "projects": [], "notes": [], "error": "That flow does not exist."}
    missing = [k for k in spec["needs"] if not params.get(k)]
    if missing:
        return {"flow": flow, "ok": False, "projects": [], "notes": [], "error": f"This flow needs: {', '.join(missing)}."}
    env.notes = []
    try:
        specs = await spec["fn"](env, params)
    except R.FlowError as exc:
        return {"flow": flow, "ok": False, "projects": [], "notes": env.notes, "error": str(exc)}
    except edl_error() as exc:
        return {"flow": flow, "ok": False, "projects": [], "notes": env.notes, "error": str(exc)}
    projects = await create_projects(env, specs)
    results = []
    for n, proj in enumerate(projects, 1):
        item = dict(proj)
        if render:
            await env.step(f"Rendering {n} of {len(projects)}")
            try:
                done = await service.render_project_and_wait(
                    workspace_id=env.workspace_id, user_id=env.user_id, project_id=proj["project_id"], preset=preset, reporter=env.reporter)
                item["media_id"] = done["media_id"]
            except service.RenderFailed as exc:
                item["error"] = str(exc)
        results.append(item)
    ok = all("error" not in r for r in results)
    return {"flow": flow, "ok": ok, "projects": results, "notes": list(env.notes), "error": None if ok else "Some videos were not rendered."}


def edl_error():
    from app.video import edl

    return edl.EditError


async def run_steps(env: R.Env, steps: list[dict[str, Any]], inputs: dict[str, Any], *, render: bool = True, preset: str = "standard") -> dict[str, Any]:
    """A recipe: its steps in order, each with its own settings and the shared inputs merged in (an input overrides a setting)."""
    results = []
    for n, step in enumerate(steps[:MAX_STEPS], 1):
        await env.step(f"Step {n} of {min(len(steps), MAX_STEPS)}: {R.FLOWS.get(step.get('flow'), {}).get('label', step.get('flow'))}")
        results.append(await run_flow(env, step["flow"], {**(step.get("params") or {}), **inputs}, render=render, preset=preset))
    return {"steps": results, "ok": all(r["ok"] for r in results), "videos": sum(len(r["projects"]) for r in results)}


async def run_batch(env: R.Env, flow: str, items: list[dict[str, Any]], *, render: bool = True, preset: str = "standard",
                    shared: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """One flow over many inputs, in turn. An item that fails is listed with its reason and the rest carry on."""
    results = []
    for n, item in enumerate(items[:MAX_BATCH], 1):
        await env.step(f"Item {n} of {min(len(items), MAX_BATCH)}")
        try:
            r = await run_flow(env, flow, {**(shared or {}), **item}, render=render, preset=preset)
        except Exception as exc:  # noqa: BLE001 - one bad file must never stop a batch
            logger.warning("Batch item %s failed: %s", n, exc)
            r = {"flow": flow, "ok": False, "projects": [], "notes": [], "error": "This item could not be processed."}
        results.append({"index": n - 1, "input": item, **r})
    done = sum(1 for r in results if r["ok"])
    return {"flow": flow, "total": len(results), "succeeded": done, "failed": len(results) - done, "results": results,
            "skipped": max(0, len(items) - MAX_BATCH)}
