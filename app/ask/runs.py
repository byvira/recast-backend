"""An Ask Ekavo run: what the person asked, the plan, what it costs, and how the work went.

A run moves through: planning, then one of
  answered   a free read was run and its result is in the run,
  ready      a plan with its price is waiting for the person's yes (or runs by itself when the person chose "act within limits" and it is small),
  ask        one short question is needed first,
  hand_off   the request needs the person's own go-ahead (publishing, deciding, deleting), so it is shown and never run,
  none       nothing the person's role allows fits.
A ready run then goes running, and ends done, partial or failed; the person can stop it between steps, or cancel it before it starts.

Ask acts as the member, with their role, through the same gatekeeper as every other way in: its credential can read and create, never publish or
approve. Each planning step costs one credit (the existing `ask_step` price), and every step the run does is priced and recorded like any other call."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional
from uuid import uuid4

from app.ask import pipeline, router, shortlist
from app.billing import costs as credit_costs
from app.billing import credits
from app.core import failures
from app.db.mongo import get_db
from app.shared.language import detect_language
from app.tools import compose
from app.tools.contract import Risk
from app.tools.executor import ToolFailure
from app.tools.gatekeeper import CONFIRM_ABOVE, Call, Gatekeeper, Refusal, visible_tools
from app.tools.registry import get_registry
from app.tools.services import LiveServices

logger = logging.getLogger(__name__)

MAX_MESSAGE = 2000
TRUSTS = ("suggest", "ask", "act")
#: "act within limits" runs by itself only up to this many credits and only when the price is exact
ACT_LIMIT = 20
#: while plans are not enforced the plan's number is shown but not applied; this safety cap still protects the shared free model allowance
DEFAULT_DAILY_STEPS = 50
KEEP_DAYS = 30
#: tools Ask never offers: the compound runner it uses itself, and undo (which has its own button)
_HIDDEN = ("compose.", "changes.undo")

_tasks: set[asyncio.Task] = set()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def runs():
    return get_db()["ask_runs"]


async def create_indexes() -> None:
    try:
        await runs().create_index([("workspace_id", 1), ("created_at", -1)])
        await runs().create_index("id", unique=True)
        await runs().create_index("created_at", expireAfterSeconds=KEEP_DAYS * 24 * 3600)
        await get_db()["ask_tool_vectors"].create_index([("space", 1), ("name", 1)], unique=True)
    except Exception as exc:  # noqa: BLE001 - optional
        logger.warning("Could not create the Ask indexes: %s", exc)


def credential_id(workspace_id: str, user_id: str) -> str:
    return f"ask:{workspace_id}:{user_id}"


# ── the daily step cap ───────────────────────────────────────────────────────

async def steps_used_today(workspace_id: str) -> int:
    start = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    rows = await runs().aggregate([
        {"$match": {"workspace_id": workspace_id, "created_at": {"$gte": start}}}, {"$group": {"_id": None, "n": {"$sum": {"$ifNull": ["$model_steps", 0]}}}},
    ]).to_list(length=1)
    return int(rows[0]["n"]) if rows else 0


async def daily_limit(workspace_id: str) -> int:
    """Planning steps a day for this workspace: the plan's number when plans are enforced, else a safety cap."""
    try:
        from app.shared import entitlements

        ent = await entitlements.entitlements_for(workspace_id)
        value = ent.limits.get("ask_steps_day")
        if ent.enforced and value is not None:
            return int(value)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not read the Ask step limit: %s", exc)
    return DEFAULT_DAILY_STEPS


# ── words ────────────────────────────────────────────────────────────────────

def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def say_ready(steps: int, credits_: int, exact: bool) -> str:
    cost = "It is free." if credits_ == 0 else f"It costs {'about ' if exact else 'at least '}{_plural(credits_, 'credit')}."
    return f"I can do this in {_plural(steps, 'step')}. {cost}"


MESSAGES = {
    "none": "I could not find a way to do that with the tools you have. Try saying it a little differently.",
    "hand_off": "That needs your own go-ahead, so I will not do it for you. Open it in the app and check it first.",
    "answered": "Here is what I found.",
    "cap": "You have used today's planning steps for this workspace. They come back tomorrow, and you can do the same thing from the app meanwhile.",
}


async def localize(text: str, language: str, *, think: Any = None) -> str:
    """The sentence in the person's language when it is not English, using the small model at no credit cost; the English sentence when anything goes wrong."""
    if not language or language == "en":
        return text
    think = think or router.think
    try:
        raw = await think("respond", f"Translate into the language with code {language}. Keep numbers. Return JSON {{\"text\": \"...\"}}.\n\n" + pipeline.fence.fence(text, 400), pipeline.fence.RULES)
        out = pipeline.fence.clean(str(raw.get("text") or ""), 400)
        return out or text
    except Exception:  # noqa: BLE001 - the English sentence is always correct
        return text


# ── planning ─────────────────────────────────────────────────────────────────

def _is_free_read(specs: list) -> bool:
    return bool(specs) and all(s.risk is Risk.READ and not s.cost_key for s in specs)


async def _price(steps: list[compose.Step]) -> tuple[list[dict[str, Any]], int, bool]:
    """Per-step base prices and their total. A step priced by length, or that uses an earlier step's result, makes the total a floor (`exact` False)."""
    table = await credit_costs.costs()
    registry = get_registry()
    rows, total, exact = [], 0, True
    for step in steps:
        spec = registry.get(step.tool)
        price = credit_costs.price_with(table, spec.cost_key, 1.0) if spec and spec.cost_key else 0
        sure = not ((table.get(spec.cost_key) or {}).get("per") if spec and spec.cost_key else False) and "${" not in str(step.arguments)
        exact = exact and sure
        total += price
        rows.append({"label": step.label, "tool": step.tool, "summary": spec.summary if spec else "", "credits": price, "exact": sure, "optional": step.optional,
                     "changes": bool(spec and spec.risk is not Risk.READ)})
    return rows, total, exact


async def _check_steps(gate: Gatekeeper, cred_id: str, steps: list[compose.Step]) -> list[dict[str, str]]:
    """Dry-runs every step that does not depend on an earlier result, so a wrong argument or a refused step shows on the card before anything runs."""
    problems: list[dict[str, str]] = []
    for step in steps:
        if "${" in str(step.arguments):
            continue
        try:
            await gate.admit_and_record(Call(credential_id=cred_id, tool=step.tool, arguments=step.arguments, dry_run=True))
        except Refusal as refusal:
            body = refusal.body()
            problems.append({"label": step.label, "code": str(body.get("code")), "message": str(body.get("message")), "optional": step.optional})
    return problems


async def create(workspace_id: str, user_id: str, message: str, trust: str = "ask", *, think: Any = None, embed: Any = None, services: Any = None) -> dict[str, Any]:
    """Plans a request and stores the run. Raises ToolFailure for a request that cannot be planned at all (empty, too long, switched off, over the daily cap)."""
    text = (message or "").strip()
    if not text:
        raise ToolFailure("input_invalid", "Say what you would like done.", 422)
    if len(text) > MAX_MESSAGE:
        raise ToolFailure("input_invalid", f"Keep it under {MAX_MESSAGE} characters.", 422)
    if trust not in TRUSTS:
        raise ToolFailure("input_invalid", "Trust must be suggest, ask or act.", 422)
    services = services or LiveServices()
    think = think or router.think
    embed = embed or shortlist.default_embed

    off = await services.switched_off(workspace_id, "ask")
    if off:
        raise ToolFailure("switched_off", off, 403)
    permissions = await services.member_permissions(workspace_id, user_id)
    if permissions is None:
        raise ToolFailure("not_a_member", "You are not an active member of this workspace.", 403)

    run_id = uuid4().hex
    language = detect_language(text) or ""
    run: dict[str, Any] = {
        "id": run_id, "workspace_id": workspace_id, "user_id": user_id, "message": text, "language": language, "trust": trust, "status": "planning",
        "model_steps": 0, "created_at": _now(), "updated_at": _now(), "plan": None, "reply": "", "stop_requested": False,
    }
    limit = await daily_limit(workspace_id)
    if await steps_used_today(workspace_id) >= limit:
        run.update(status="none", reply=await localize(MESSAGES["cap"], language, think=think), reason="daily_cap")
        await runs().insert_one(dict(run))
        return public(run)

    cred = await services.credential(credential_id(workspace_id, user_id))
    registry = get_registry()
    specs = [s for s in visible_tools(registry, cred, permissions) if not s.name.startswith(_HIDDEN)]
    space = await shortlist.pick_space()
    names, how = await shortlist.shortlist(text, specs, embed=embed, space=space)

    @asynccontextmanager
    async def charge(step: str) -> AsyncIterator[None]:
        # one planning credit, given back when the model step fails; the daily count is what was actually used
        async with credits.spend(workspace_id, user_id, "ask_step", step=f"ask:{run_id}:{step}"):
            yield
            run["model_steps"] += 1

    try:
        understood = await pipeline.understand(text, specs, language, think=think, charge=charge, shortlisted=names, how=how)
    except Exception as exc:  # noqa: BLE001
        if credits.is_out_of_credits(exc):
            raise
        retry = int(getattr(exc, "retry_after", 60) or 60)
        run.update(status="failed", error=failures.ai_busy(retry).body())
        run["reply"] = await localize("I could not think that through just now. Nothing was charged for the failed step. Try again in a minute.", language, think=think)
        await runs().insert_one(dict(run))
        return public(run)

    run["understanding"] = {"action": understood.action, "tool": understood.tool, "reason": understood.reason, "shortlist": understood.shortlist, "how": understood.how, "problems": understood.problems}
    run["updated_at"] = _now()

    if understood.action == "ask":
        run.update(status="ask", question=understood.question, reply=understood.question or "")
    elif understood.action == "hand_off":
        spec = registry.get(understood.tool or "")
        run.update(status="hand_off", plan={"hand_off": {"tool": understood.tool, "summary": spec.summary if spec else ""}}, reply=await localize(MESSAGES["hand_off"], language, think=think))
    elif understood.steps:
        gate = Gatekeeper(registry, services)
        cred_id = credential_id(workspace_id, user_id)
        problems = await _check_steps(gate, cred_id, understood.steps)
        step_rows, total, exact = await _price(understood.steps)
        specs_used = [registry.get(s.tool) for s in understood.steps]
        run["plan"] = {"steps": [{**r, "arguments": understood.steps[i].arguments} for i, r in enumerate(step_rows)], "credits": total, "exact": exact, "problems": problems,
                       "confirm_needed": total > CONFIRM_ABOVE}
        blocking = [p for p in problems if not p.get("optional")]
        if blocking:
            run.update(status="none", reply=blocking[0]["message"])
        elif _is_free_read(specs_used) and trust != "suggest":
            run["status"] = "ready"
            await runs().insert_one(dict(run))
            return await confirm(workspace_id, user_id, run_id, services=services, wait=True)
        else:
            run.update(status="ready", reply=await localize(say_ready(len(understood.steps), total, exact), language, think=think))
            if trust == "act" and total <= ACT_LIMIT and exact:
                await runs().insert_one(dict(run))
                return await confirm(workspace_id, user_id, run_id, services=services)
    else:
        run.update(status="none", reply=await localize(MESSAGES["none"], language, think=think))
    await runs().insert_one(dict(run))
    return public(run)


# ── running ──────────────────────────────────────────────────────────────────

async def _load(workspace_id: str, run_id: str, user_id: Optional[str] = None) -> dict[str, Any]:
    query: dict[str, Any] = {"id": run_id, "workspace_id": workspace_id}
    if user_id:
        query["user_id"] = user_id  # a run belongs to the person who asked
    row = await runs().find_one(query, {"_id": 0})
    if row is None:
        raise ToolFailure("not_found", "That request was not found. Requests are kept for 30 days.", 404)
    return row


async def confirm(workspace_id: str, user_id: str, run_id: str, *, services: Any = None, wait: bool = False) -> dict[str, Any]:
    """Starts a ready run. The price is worked out again and must still be what the person saw; if it changed they are told and nothing runs."""
    services = services or LiveServices()
    run = await _load(workspace_id, run_id, user_id)
    if run["status"] != "ready" or not (run.get("plan") or {}).get("steps"):
        raise ToolFailure("not_ready", "There is nothing waiting for your yes on that request.", 409)
    if run.get("trust") == "suggest":
        raise ToolFailure("suggest_only", "This request was set to suggestions only. Ask again with 'ask before acting' to run it.", 409)
    steps = [compose.Step(label=s["label"], tool=s["tool"], arguments=s["arguments"], optional=bool(s.get("optional"))) for s in run["plan"]["steps"]]
    _, total, exact = await _price(steps)
    if total != run["plan"]["credits"] or exact != run["plan"]["exact"]:
        await runs().update_one({"id": run_id}, {"$set": {"plan.credits": total, "plan.exact": exact, "updated_at": _now()}})
        raise ToolFailure("price_changed", f"The price changed to {total} credits since you looked. Check it and confirm again.", 409)
    claimed = await runs().update_one({"id": run_id, "status": "ready"}, {"$set": {"status": "running", "started_at": _now(), "updated_at": _now()}})
    if not claimed.modified_count:
        raise ToolFailure("not_ready", "That request is already running.", 409)
    if wait:
        await _execute(workspace_id, user_id, run_id, steps, services)
    else:
        task = asyncio.create_task(_execute(workspace_id, user_id, run_id, steps, services))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    return public(await _load(workspace_id, run_id, user_id))


async def _execute(workspace_id: str, user_id: str, run_id: str, steps: list[compose.Step], services: Any) -> None:
    gate = Gatekeeper(get_registry(), services)
    cred_id = credential_id(workspace_id, user_id)

    async def run_tool(call: Call):
        row = await runs().find_one({"id": run_id}, {"stop_requested": 1})
        if row and row.get("stop_requested"):
            raise ToolFailure("stopped", "You stopped this request, so this step did not run.", 409)
        return await compose._default_run_tool(gate, call)

    async def created(compose_id: str) -> None:
        await runs().update_one({"id": run_id}, {"$set": {"compose_run_id": compose_id}})

    try:
        summary = await compose.start(workspace_id, cred_id, user_id, steps, run_tool=run_tool, on_created=created)
        status = {"done": "done", "waiting": "running", "partial": "partial", "failed": "failed"}.get(summary["status"], "partial")
        stopped = any((s.get("error") or {}).get("code") == "stopped" for s in summary["steps"])
        await runs().update_one({"id": run_id}, {"$set": {"status": "stopped" if stopped and status != "done" else status, "result": summary, "updated_at": _now()}})
    except Exception as exc:  # noqa: BLE001 - the run records what happened; nothing escapes a background task
        logger.warning("An Ask run failed: %s", type(exc).__name__)
        await runs().update_one({"id": run_id}, {"$set": {"status": "failed", "error": {"code": "internal_error", "message": "Something went wrong while doing this. Finished steps are kept.", "retryable": True}, "updated_at": _now()}})


async def stop(workspace_id: str, user_id: str, run_id: str) -> dict[str, Any]:
    """Cancels a run that has not started, or asks a running one to stop before its next step (a step already under way finishes)."""
    run = await _load(workspace_id, run_id, user_id)
    if run["status"] in ("ready", "ask", "hand_off"):
        await runs().update_one({"id": run_id}, {"$set": {"status": "cancelled", "updated_at": _now()}})
    elif run["status"] == "running":
        await runs().update_one({"id": run_id}, {"$set": {"stop_requested": True, "updated_at": _now()}})
    return public(await _load(workspace_id, run_id, user_id))


async def answer(workspace_id: str, user_id: str, run_id: str, reply: str, *, think: Any = None, embed: Any = None, services: Any = None) -> dict[str, Any]:
    """The person's answer to a question: planned again from the original request and the answer, as a new run (the question's run is closed)."""
    run = await _load(workspace_id, run_id, user_id)
    if run["status"] != "ask":
        raise ToolFailure("not_ready", "That request is not waiting for an answer.", 409)
    combined = f"{run['message']}\n(Question: {run.get('question') or ''})\nAnswer: {(reply or '').strip()}"
    await runs().update_one({"id": run_id}, {"$set": {"status": "cancelled", "updated_at": _now()}})
    return await create(workspace_id, user_id, combined[:MAX_MESSAGE], run.get("trust", "ask"), think=think, embed=embed, services=services)


async def get(workspace_id: str, user_id: str, run_id: str) -> dict[str, Any]:
    run = await _load(workspace_id, run_id, user_id)
    if run["status"] == "running" and run.get("compose_run_id"):
        try:
            live = compose.summarise(await compose.load(workspace_id, run["compose_run_id"]))
            run["result"] = live
            if live["status"] != "waiting" and not any(s["state"] in ("running", "pending") for s in live["steps"]) and run["status"] == "running":
                pass
        except ToolFailure:
            pass
    return public(run)


#: the filters the history offers, each a group of statuses
FILTERS = {
    "needs": ["ready", "ask", "hand_off"],
    "running": ["planning", "running"],
    "done": ["done", "answered"],
    "stopped": ["stopped", "cancelled", "partial"],
    "problems": ["failed", "none"],
}


async def history(workspace_id: str, user_id: str, *, limit: int = 20, q: str = "", group: str = "", before: Optional[datetime] = None) -> dict[str, Any]:
    """The person's own requests, newest first, optionally narrowed to a group of statuses and to those whose words contain `q`. `before` pages back
    from the last request of the previous page. Requests are kept for 30 days."""
    import re

    query: dict[str, Any] = {"workspace_id": workspace_id, "user_id": user_id}
    if group:
        if group not in FILTERS:
            raise ToolFailure("input_invalid", "That filter is not one of: " + ", ".join(FILTERS) + ".", 422)
        query["status"] = {"$in": FILTERS[group]}
    text = (q or "").strip()[:80]
    if text:
        query["message"] = {"$regex": re.escape(text), "$options": "i"}
    if before is not None:
        query["created_at"] = {"$lt": before}
    size = max(1, min(limit, 50))
    rows = await runs().find(query, {"_id": 0}).sort("created_at", -1).limit(size + 1).to_list(length=size + 1)
    more = len(rows) > size
    rows = rows[:size]
    return {"items": [public(r) for r in rows], "next_before": rows[-1]["created_at"].isoformat() if more and rows else None}


async def recent(workspace_id: str, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
    return (await history(workspace_id, user_id, limit=limit))["items"]


async def reconcile_after_restart() -> int:
    """A run that was under way when the server stopped cannot go on by itself. It is marked, and its finished steps are kept."""
    done = await runs().update_many({"status": "running"}, {"$set": {"status": "partial", "error": {"code": "interrupted", "message": "The server restarted while this was running. Finished steps are kept.", "retryable": True}, "updated_at": _now()}})
    return done.modified_count


def public(run: dict[str, Any]) -> dict[str, Any]:
    """The run as the person sees it. No internals, no model text, no provider detail."""
    out = {k: run.get(k) for k in ("id", "message", "language", "trust", "status", "reply", "question", "plan", "result", "error", "created_at", "updated_at", "started_at")}
    out["understanding"] = {"how": (run.get("understanding") or {}).get("how")}
    out["planning_steps"] = int(run.get("model_steps") or 0)
    return {k: v for k, v in out.items() if v not in (None, "")}
