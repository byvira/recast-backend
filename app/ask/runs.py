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

from app.ask import context, decisions, pipeline, repair, router, shortlist, threads, tongue
from app.pipelines.media import request_refs
from app.billing import costs as credit_costs
from app.billing import credits
from app.core import failures
from app.db.mongo import get_db
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
_HIDDEN = ("compose.", "changes.undo", "ask.")             # Ask never offers itself to its own planner

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


async def localize_fixed(key: str, text: str, language: str) -> str:
    """A sentence that never changes, in the person's language. Each one is translated once per language and kept, so the model is asked a single time
    and every later request reads it for free. The English sentence stands in when anything goes wrong."""
    if not language or language == "en":
        return text
    try:
        from app.shared.localized_strings import get_localized_string

        out = await get_localized_string(key, language, text.replace("{", "{{").replace("}", "}}"))
        return out.strip() or text
    except Exception:  # noqa: BLE001 - the English sentence is always correct
        return text


RESULT_WORDS = {"done": "Done.", "partial": "Some steps finished and some did not.", "failed": "That did not work. Nothing more was charged.", "stopped": "Stopped."}
RUN_FAILED = "Something went wrong while doing this. Finished steps are kept."


async def localize_steps(rows: list[dict[str, Any]], language: str) -> None:
    """Adds `summary_local` to each plan step: what the step does, in the person's language. The English `summary` stays."""
    if not language or language == "en":
        return
    for row in rows:
        if row.get("summary"):
            local = await localize_fixed(f"ask.tool.{row['tool']}", row["summary"], language)
            if local and local != row["summary"]:
                row["summary_local"] = local


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


async def _facts(workspace_id: str) -> dict[str, Any]:
    """A few short facts about the workspace for the context: the main brand's name."""
    try:
        brand = await get_db()["brand_profiles"].find_one({"workspace_id": workspace_id}, {"identity": 1}, sort=[("is_default", -1), ("created_at", 1)])
        identity = (brand or {}).get("identity") or {}
        name = identity.get("company_name") or identity.get("name")
        return {"Brand": name} if name else {}
    except Exception:  # noqa: BLE001 - facts are a convenience
        return {}


async def create(workspace_id: str, user_id: str, message: str, trust: str = "ask", *, think: Any = None, embed: Any = None, services: Any = None,
                 thread_id: Optional[str] = None, surface: str = "app", on_phase: Any = None, reference_ids: Optional[list[str]] = None) -> dict[str, Any]:
    """Plans a request. Inside a conversation (`thread_id`) the planner also reads the earlier turns, so "make it shorter" or "same for Instagram" means what the
    person means, and it may simply answer when no tool is needed. Without one it behaves exactly as before."""
    if not thread_id:
        return await _plan_run(workspace_id, user_id, message, trust, think=think, embed=embed, services=services, on_phase=on_phase, reference_ids=reference_ids)
    text = (message or "").strip()
    if not text:
        raise ToolFailure("input_invalid", "Say what you would like done.", 422)
    try:
        thread = await threads.load(workspace_id, user_id, thread_id)
    except threads.ThreadError as exc:
        raise ToolFailure("not_found", exc.message, exc.status)
    block = context.build(thread, await threads.recent(thread_id), await _facts(workspace_id))
    await threads.append(thread, threads.author_member(user_id), text, surface=surface)
    run = await _plan_run(workspace_id, user_id, text, trust, think=think, embed=embed, services=services, conversation=block, allow_reply=True, thread_id=thread_id, on_phase=on_phase, reference_ids=reference_ids)
    await _record_plan(thread, run, surface)
    fresh = await threads.threads().find_one({"id": thread_id}, {"_id": 0})
    if fresh and threads.needs_summary(fresh):
        task = asyncio.create_task(threads.roll_summary(thread_id, think or router.think))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    return {**run, "thread_id": thread_id}


async def _record_plan(thread: dict[str, Any], run: dict[str, Any], surface: str) -> None:
    """Ekavo's side of the turn in the conversation: the plan, the answer or the question. A run that already finished (a free read) records its result itself."""
    status = run.get("status")
    if status in ("running", "done", "partial", "failed", "stopped"):
        return
    kind = {"ready": "plan", "ask": "question"}.get(status, "answer")
    text = str(run.get("reply") or run.get("question") or "")
    if not text:
        return
    await threads.append(thread, threads.EKAVO, text, kind=kind, surface=surface, run_id=run.get("id"))


async def _record_result(run_id: str) -> None:
    """When a run in a conversation ends, add its result: a short answer for a read, what was made (ids and titles) for anything else."""
    from app.channels import answer

    run = await runs().find_one({"id": run_id}, {"_id": 0})
    if not run or not run.get("thread_id"):
        return
    thread = await threads.threads().find_one({"id": run["thread_id"]}, {"_id": 0})
    if thread is None:
        return
    status = str(run.get("status"))
    made = context.made_from_result(run.get("result") or {})
    said = answer.short_answer(run) if status == "done" else ""
    word = await localize_fixed(f"ask.result.{status}", RESULT_WORDS[status], str(run.get("language") or "")) if status in RESULT_WORDS else status
    await threads.append(thread, threads.EKAVO, said or word, kind="result", run_id=run_id, refs={"made": made} if made else None)


PHASES = {
    "find": "Finding the right tools", "decide": "Understanding your request", "fill": "Working out the details",
    "plan": "Planning the steps", "price": "Checking the price and your limits",
}


async def _tell(on_phase: Any, key: str) -> None:
    """Tell a listener (the live stream) which stage the planner has reached. A listener that fails never stops the planner."""
    if on_phase is None:
        return
    try:
        await on_phase(key)
    except Exception:  # noqa: BLE001
        logger.debug("A phase listener failed")


async def _plan_run(workspace_id: str, user_id: str, message: str, trust: str = "ask", *, think: Any = None, embed: Any = None, services: Any = None,
                    conversation: str = "", allow_reply: bool = False, thread_id: Optional[str] = None, on_phase: Any = None,
                    reference_ids: Optional[list[str]] = None) -> dict[str, Any]:
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
    thread_doc = await threads.threads().find_one({"id": thread_id}, {"language": 1, "script": 1}) if thread_id else None
    language, script = await tongue.resolve(text, thread_doc, user_id)
    await tongue.remember(thread_id, language, script)
    run: dict[str, Any] = {
        "id": run_id, "workspace_id": workspace_id, "user_id": user_id, "message": text, "language": language, "script": script, "trust": trust, "status": "planning",
        "model_steps": 0, "created_at": _now(), "updated_at": _now(), "plan": None, "reply": "", "stop_requested": False,
    }
    if thread_id:
        run["thread_id"] = thread_id
    if reference_ids:
        run["reference_ids"] = request_refs.check_ids(reference_ids)
    limit = await daily_limit(workspace_id)
    if await steps_used_today(workspace_id) >= limit:
        run.update(status="none", reply=await localize_fixed("ask.message.cap", MESSAGES["cap"], language), reason="daily_cap")
        await runs().insert_one(dict(run))
        return public(run)

    cred = await services.credential(credential_id(workspace_id, user_id))
    registry = get_registry()
    specs = [s for s in visible_tools(registry, cred, permissions) if not s.name.startswith(_HIDDEN)]
    await _tell(on_phase, "find")
    space = await shortlist.pick_space()
    names, how = await shortlist.shortlist(text, specs, embed=embed, space=space)

    @asynccontextmanager
    async def charge(step: str) -> AsyncIterator[Any]:
        # one planning credit, given back when the model step fails or turns out to be a plain answer; the daily count is what was actually used
        await _tell(on_phase, step)
        async with credits.spend(workspace_id, user_id, "ask_step", step=f"ask:{run_id}:{step}") as hold:
            yield hold
            run["model_steps"] += 1

    try:
        understood = await pipeline.understand(text, specs, language, think=think, charge=charge, shortlisted=names, how=how, conversation=conversation, allow_reply=allow_reply, script=script)
    except Exception as exc:  # noqa: BLE001
        if credits.is_out_of_credits(exc):
            raise
        retry = int(getattr(exc, "retry_after", 60) or 60)
        run.update(status="failed", error=failures.ai_busy(retry).body())
        run["reply"] = await localize_fixed("ask.message.think_failed", "I could not think that through just now. Nothing was charged for the failed step. Try again in a minute.", language)
        await runs().insert_one(dict(run))
        return public(run)

    run["understanding"] = {"action": understood.action, "tool": understood.tool, "reason": understood.reason, "shortlist": understood.shortlist, "how": understood.how, "problems": understood.problems}
    run["updated_at"] = _now()
    shadow = await decisions.shadow_name()
    if shadow:                                          # a candidate decider tried beside the live one: recorded, never acted on
        by_name = {s.name: s for s in specs}
        run["shadow"] = await decisions.run_shadow(shadow, text, [by_name[n] for n in names if n in by_name], language, understood.tool)

    if understood.action == "ask":
        run.update(status="ask", question=understood.question, reply=understood.question or "")
    elif understood.action == "reply":
        run.update(status="answered", reply=understood.answer)
    elif understood.action == "hand_off":
        spec = registry.get(understood.tool or "")
        run.update(status="hand_off", plan={"hand_off": {"tool": understood.tool, "summary": spec.summary if spec else ""}}, reply=await localize_fixed("ask.message.hand_off", MESSAGES["hand_off"], language))
    elif understood.steps:
        gate = Gatekeeper(registry, services)
        cred_id = credential_id(workspace_id, user_id)
        await _tell(on_phase, "price")
        await repair.repair_steps(understood.steps, registry, workspace_id)     # one value where a list is needed, a different spelling, a placeholder brand
        problems = await _check_steps(gate, cred_id, understood.steps)
        step_rows, total, exact = await _price(understood.steps)
        await localize_steps(step_rows, language)
        specs_used = [registry.get(s.tool) for s in understood.steps]
        run["plan"] = {"steps": [{**r, "arguments": understood.steps[i].arguments} for i, r in enumerate(step_rows)], "credits": total, "exact": exact, "problems": problems,
                       "confirm_needed": total > CONFIRM_ABOVE}
        blocking = [p for p in problems if not p.get("optional")]
        if blocking and blocking[0].get("code") == "input_invalid":
            # the plan was understood but a value in it still cannot be read: ask, in plain words, instead of showing a technical refusal
            logger.info("A plan step had an input that could not be used: %s", [(p.get("label"), p.get("code")) for p in blocking])
            need = await localize_fixed("ask.message.need_more", "I could not set that up from what you wrote. Tell me a little more, for example what it is about and where it should go.", language)
            run.update(status="ask", question=need, reply=need)
        elif blocking:
            run.update(status="none", reply=await localize(blocking[0]["message"], language, think=think))
        elif _is_free_read(specs_used) and trust != "suggest":
            run["status"] = "ready"
            await runs().insert_one(dict(run))
            return await confirm(workspace_id, user_id, run_id, services=services, wait=True)
        else:
            fixed = say_ready(len(understood.steps), total, exact)
            facts = fixed + " " + " ".join(f"Step {i + 1}: {r['summary']}" for i, r in enumerate(step_rows) if r.get("summary"))
            spoken = await tongue.say(facts, language, script, think)
            run.update(status="ready", reply=spoken or await localize(fixed, language, think=think))
            if trust == "act" and total <= ACT_LIMIT and exact:
                await runs().insert_one(dict(run))
                return await confirm(workspace_id, user_id, run_id, services=services)
    else:
        run.update(status="none", reply=await localize_fixed("ask.message.none", MESSAGES["none"], language))
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
    claimed = await runs().update_one({"id": run_id, "status": "ready"}, {"$set": {"status": "running", "started_at": _now(), "updated_at": _now(), "accepted": True}})
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
    """Run the steps. While they run, the look of any pictures the person sent with the request is added to each picture made (see `request_refs.py`)."""
    row = await runs().find_one({"id": run_id}, {"reference_ids": 1}) or {}
    token = request_refs.CURRENT.set(await request_refs.line_for(workspace_id, user_id, row.get("reference_ids") or []))
    try:
        await _execute_steps(workspace_id, user_id, run_id, steps, services)
    finally:
        request_refs.CURRENT.reset(token)


async def _execute_steps(workspace_id: str, user_id: str, run_id: str, steps: list[compose.Step], services: Any) -> None:
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
        language = str((await runs().find_one({"id": run_id}, {"language": 1}) or {}).get("language") or "")
        for step in summary["steps"]:
            error = step.get("error")
            if error and error.get("message") and language not in ("", "en"):
                error["message"] = await localize(str(error["message"]), language)
        await runs().update_one({"id": run_id}, {"$set": {"status": "stopped" if stopped and status != "done" else status, "result": summary, "updated_at": _now()}})
        if status != "running":
            await _record_result(run_id)
    except Exception as exc:  # noqa: BLE001 - the run records what happened; nothing escapes a background task
        logger.warning("An Ask run failed: %s", type(exc).__name__)
        said = await localize_fixed("ask.message.run_failed", RUN_FAILED, str((await runs().find_one({"id": run_id}, {"language": 1}) or {}).get("language") or ""))
        await runs().update_one({"id": run_id}, {"$set": {"status": "failed", "error": {"code": "internal_error", "message": said, "retryable": True}, "updated_at": _now()}})


async def stop(workspace_id: str, user_id: str, run_id: str) -> dict[str, Any]:
    """Cancels a run that has not started, or asks a running one to stop before its next step (a step already under way finishes)."""
    run = await _load(workspace_id, run_id, user_id)
    if run["status"] in ("ready", "ask", "hand_off"):
        await runs().update_one({"id": run_id}, {"$set": {"status": "cancelled", "updated_at": _now(), "accepted": False}})
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
    out = {k: run.get(k) for k in ("id", "message", "language", "trust", "status", "reply", "question", "plan", "result", "error", "created_at", "updated_at", "started_at", "thread_id")}
    out["understanding"] = {"how": (run.get("understanding") or {}).get("how")}
    out["planning_steps"] = int(run.get("model_steps") or 0)
    return {k: v for k, v in out.items() if v not in (None, "")}
