"""Doing the work for a call the gatekeeper admitted.

Three ways a tool runs, all through code that already exists, so every rule of the app (its own permission check, plan limits, credits,
safety, activity) still applies:

* an action tool (`action:<name>`) is started on the job layer and answers with its run;
* a route tool (`route:<METHOD path>`) is answered by the app's own route, called inside the server as the acting member, with that
  member's current rights and one rate bucket per credential;
* a tool with its own handler (`HANDLERS`) is answered by that handler: the typed reads and the picture and video edits.

A tool that is none of these answers `tool_not_wired` and is shown as unavailable in the catalog until it has a handler.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Optional

import httpx

from app.core.auth import create_access_token
from app.shared import jobs
from app.core import failures
from app.tools import ledger, undo
from app.tools.contract import Risk, ToolSpec
from app.tools.gatekeeper import Admission, Call, Refusal

#: a tool's own way of running: (admission) -> the answer as plain data
Handler = Callable[[Admission], Awaitable[dict[str, Any]]]
HANDLERS: dict[str, Handler] = {}

REQUEST_SECONDS = 90.0
_PLACEHOLDER = re.compile(r"\{([^}/]+)\}")


def handler(tool: str) -> Callable[[Handler], Handler]:
    def wrap(fn: Handler) -> Handler:
        HANDLERS[tool] = fn
        return fn

    return wrap


class ToolFailure(Exception):
    """The work was started and could not be done. The message is in plain words and never carries a provider's own text. `retryable` and
    `retry_after_seconds` say whether trying again can help; `next` says what to do."""

    def __init__(self, code: str, message: str, status: int = 400, *, retryable: bool = False, retry_after_seconds: Optional[int] = None, next: str = ""):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status
        self.retryable, self.retry_after_seconds, self.next = retryable, retry_after_seconds, next

    @classmethod
    def of(cls, failure: failures.Failure) -> "ToolFailure":
        return cls(failure.code, failure.message, failure.status, retryable=failure.retryable, retry_after_seconds=failure.retry_after_seconds, next=failure.next)

    def body(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message, "retryable": self.retryable}
        if self.retry_after_seconds is not None:
            out["retry_after_seconds"] = self.retry_after_seconds
        if self.next:
            out["next"] = self.next
        return out


@dataclass(frozen=True)
class Outcome:
    data: Any
    #: set when the tool started background work; poll it with runs.get
    run: Optional[dict[str, Any]] = None
    status: int = 200
    #: how to take this change back (see undo.py): its id, the way, and whether it can be done
    change: Optional[dict[str, Any]] = None
    #: things that worked but not fully (a picture that fell back to a text card, say), in plain words
    warnings: tuple[dict[str, str], ...] = ()


def load_handlers() -> None:
    """Make sure every tool's own runner is registered (they register themselves when their module is imported)."""
    from app.tools import ask_tool, changes, compose, handlers, runners_edit  # noqa: F401


def is_wired(tool: ToolSpec) -> bool:
    """True when something can run this tool."""
    load_handlers()
    return tool.name in HANDLERS or tool.source.startswith("action:") or (tool.source.startswith("route:") and not tool.coarse)


def _message(detail: Any) -> str:
    if isinstance(detail, str):
        return detail
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("detail") or "That could not be done.")
    if isinstance(detail, list) and detail:
        first = detail[0]
        if isinstance(first, dict):
            where = ".".join(str(p) for p in first.get("loc", ())[1:])
            return f"{where}: {first.get('msg')}" if where else str(first.get("msg") or "Some of what was sent is not valid.")
    return "That could not be done."


async def execute(admission: Admission, call: Call) -> Outcome:
    load_handlers()
    tool = admission.tool
    if tool.name in HANDLERS:
        return Outcome(await HANDLERS[tool.name](admission))
    if tool.source.startswith("action:"):
        return await _start_action(admission, call)
    if tool.source.startswith("route:") and not tool.coarse:
        return await _call_route(admission)
    raise ToolFailure("tool_not_wired", "That tool is listed but cannot be run from outside the app yet. Do it in Ekavo.", 501)


# ── actions ──────────────────────────────────────────────────────────────

async def _start_action(admission: Admission, call: Call) -> Outcome:
    credential = admission.credential
    ctx = await jobs.build_context(credential.workspace_id, credential.user_id or "")
    payload = admission.arguments.model_dump(mode="json", exclude={"confirm"})
    try:
        run, already = await jobs.submit(action_name=admission.tool.source.split(":", 1)[1], payload=payload, ctx=ctx, client_key=call.idempotency_key)
    except Exception as exc:  # noqa: BLE001
        status = getattr(exc, "status_code", None)
        if status is None:
            raise
        found = failures.ai_busy(int(getattr(exc, "retry_after", 60))) if getattr(exc, "code", "") == "ai_busy" else failures.from_upstream(status, getattr(exc, "detail", None))
        raise ToolFailure.of(found)
    return Outcome({"run": run, "already_running": already}, run=run, status=202)


# ── routes ───────────────────────────────────────────────────────────────

def _split(route_key: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Any], Optional[dict[str, Any]]]:
    """The path with its ids filled in, the query options, and the body: each field goes where the route declares it."""
    from app.tools.from_routes import route_index

    method, path = route_key.split(" ", 1)
    route = route_index()[route_key]
    path_names = {p.name for p in route.dependant.path_params}
    query_names = {p.name for p in route.dependant.query_params}
    for name in path_names:
        if name not in arguments:
            raise ToolFailure("input_invalid", f"{name} is needed.", 422)
    filled = _PLACEHOLDER.sub(lambda m: str(arguments[m.group(1)]), path)
    query = {k: v for k, v in arguments.items() if k in query_names and v is not None}
    rest = {k: v for k, v in arguments.items() if k not in path_names and k not in query_names}
    body = rest if method in ("POST", "PUT", "PATCH") and (rest or route.dependant.body_params) else None
    return filled, query, body


def _app():
    from app.main import app

    return app


def failure_from_response(response: httpx.Response) -> failures.Failure:
    """The shared error fields the app puts on every failure (`code`, `retryable`, `retry_after_seconds`), or, for an answer without them, the
    same thing worked out from the status. The app's own plain message is kept; a provider's is replaced."""
    try:
        body = response.json()
    except ValueError:
        body = {}
    detail = body.get("detail") if isinstance(body, dict) else None
    header = response.headers.get("retry-after")
    after = int(header) if header and header.isdigit() else (body.get("retry_after_seconds") if isinstance(body, dict) else None)
    found = failures.from_upstream(response.status_code, detail, after)
    named = body.get("code") if isinstance(body, dict) and isinstance(body.get("code"), str) else None
    if named == "ai_busy":
        found = failures.ai_busy(after or 60)
    elif named and named not in ("server_error", "rate_limited") and found.code in ("failed", "input_invalid", "not_found", "conflict", "not_allowed"):
        found = failures.Failure(named, found.message, found.retryable, found.retry_after_seconds, found.next, found.status)
    return found


#: how long a read waits between its own retries (replaced in tests)
RETRY_WAITS = failures.RETRY_WAITS


async def _send_safely(credential, method: str, path: str, query, body, *, may_retry: bool) -> httpx.Response:
    """One call to the app, with a short, bounded retry for a call that cannot change anything. A call that writes or spends is never repeated
    here: a repeat could do it twice. It reports `retryable` instead, and the caller repeats it with the same idempotency key."""
    import asyncio

    attempts = 1 + (failures.AUTO_RETRIES if may_retry else 0)
    last: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            response = await _send(credential, method, path, query, body)
        except httpx.TimeoutException as exc:
            last = exc
            if attempt + 1 >= attempts:
                raise ToolFailure.of(failures.timed_out())
        except httpx.TransportError as exc:
            last = exc
            if attempt + 1 >= attempts:
                raise ToolFailure.of(failures.Failure("service_unavailable", "Ekavo could not be reached just now. Nothing was charged.", True, 15, "Try again in a moment.", 502))
        else:
            if response.status_code in (502, 503, 504) and attempt + 1 < attempts:
                await asyncio.sleep(RETRY_WAITS[min(attempt, len(RETRY_WAITS) - 1)])
                continue
            return response
        await asyncio.sleep(RETRY_WAITS[min(attempt, len(RETRY_WAITS) - 1)])
    raise ToolFailure.of(failures.timed_out()) from last


async def request_route(credential, method: str, path: str, query: Optional[dict[str, Any]], body: Optional[dict[str, Any]]) -> Any:
    """One call to the app's own route as the person behind the credential. Returns the JSON answer; a refusal from the route becomes a
    ToolFailure with the shared fields. A read is retried a couple of times on a busy answer; anything else is not."""
    response = await _send_safely(credential, method, path, query, body, may_retry=method == "GET")
    if response.status_code >= 400:
        raise ToolFailure.of(failure_from_response(response))
    if response.status_code == 204 or not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        raise ToolFailure("upstream_unreadable", "The answer could not be read.", 502)


async def _send(credential, method: str, path: str, query: Optional[dict[str, Any]], body: Optional[dict[str, Any]]) -> httpx.Response:
    headers = {
        "Authorization": f"Bearer {create_access_token({'sub': credential.user_id})}",
        "X-Workspace-Id": credential.workspace_id,
        # the app counts its rate limits by the last address in this header: one bucket for each credential, not one for the whole server
        "X-Forwarded-For": f"tool-{credential.id}",
        "User-Agent": "ekavo-tools",
    }
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://tools.internal", timeout=REQUEST_SECONDS) as client:
        return await client.request(method, path, params=query or None, json=body, headers=headers)


async def _call_route(admission: Admission) -> Outcome:
    credential, tool = admission.credential, admission.tool
    route_key = tool.source.split(":", 1)[1]
    method = route_key.split(" ", 1)[0]
    arguments = admission.arguments.model_dump(mode="json", exclude={"confirm"}, exclude_none=True)
    path, query, body = _split(route_key, arguments)
    response = await _send_safely(credential, method, path, query, body, may_retry=failures.safe_to_repeat(tool.risk.value))
    if response.status_code >= 400:
        raise ToolFailure.of(failure_from_response(response))
    if response.status_code == 204 or not response.content:
        return Outcome({}, status=200)
    try:
        data = response.json()
    except ValueError:
        raise ToolFailure("upstream_unreadable", "The answer could not be read.", 502)
    run = data if isinstance(data, dict) and data.get("status") in ("queued", "running") and data.get("id") else None
    return Outcome(data, run=run, status=202 if response.status_code == 202 else 200)


# ── the whole path of one call ───────────────────────────────────────────

async def run_call(gate, call: Call) -> tuple[Admission, Optional[Outcome]]:
    """Admit the call, do the work, and settle the records: the idempotent answer is kept, a failure frees the key for a retry, and the
    ledger row says how the call ended. A dry run, or a repeat that is answered from the first, does no work."""
    admission = await gate.admit_and_record(call)
    if admission.replay is not None or admission.dry_run:
        return admission, None
    credential = admission.credential
    rule, before, arguments = None, None, admission.arguments.model_dump(mode="json", exclude={"confirm"}, exclude_none=True)
    try:
        rule = undo.rule_for(admission.tool.name)
        if rule is not None and rule.kind in (undo.Kind.VERSION, undo.Kind.SNAPSHOT) and admission.tool.name != "changes.undo":
            before = await undo.capture(rule, admission, arguments, request_route)
    except Exception:  # noqa: BLE001 - not being able to take a copy never stops the call; the change is then recorded as not undoable
        import logging

        logging.getLogger(__name__).exception("could not take what undo needs for %s", admission.tool.name)
    try:
        outcome = await execute(admission, call)
    except (ToolFailure, Refusal) as failure:
        if getattr(failure, "code", "") in {c for c, _ in failures.SERVICES.values()}:
            await _note_ai(False, code=failure.code)
        await _settle(call, admission, ok=False, code=failure.code)
        raise
    except Exception:
        await _settle(call, admission, ok=False, code="internal_error")
        raise
    await _note_ai(True, admission.tool)
    change = None
    if rule is not None and admission.tool.risk is not Risk.READ and admission.tool.name != "changes.undo":
        try:
            change = await undo.record(rule, admission, arguments, before, outcome.data, admission.call_id)
        except Exception:  # noqa: BLE001 - the history never changes what the caller is told
            import logging

            logging.getLogger(__name__).exception("change not recorded for %s", admission.tool.name)
    outcome = replace(outcome, change=change) if change else outcome
    warnings = degraded(outcome.data)
    if warnings:
        outcome = replace(outcome, warnings=warnings)
    await _settle(call, admission, ok=True, result={"data": outcome.data, "status": outcome.status, "change": change})
    return admission, outcome


#: which outside service a price row depends on, so a run of failures of one kind starts a short cool-down for that kind only, and one success ends it
SERVICE_OF_COST: dict[str, str] = {
    **{k: "writing" for k in ("text_post", "text_refine", "reader_review", "audio_script", "video_script", "video_plan", "hooks_angles", "draft_feedback",
                              "analytics_answer", "ask_step", "campaign_plan", "voice_analysis", "campaign_topics", "preset_suggest", "publish_details", "long_form")},
    **{k: "picture" for k in ("picture", "default_picture", "picture_hd", "remove_background", "mascot", "mascot_first")},
    **{k: "voice" for k in ("narration", "dialogue")},
    **{k: "transcription" for k in ("transcription", "captions")},
}
AI_COST_KEYS = frozenset(SERVICE_OF_COST)


def service_of(tool: ToolSpec) -> Optional[str]:
    return SERVICE_OF_COST.get(tool.cost_key or "")


async def _note_ai(ok: bool, tool: Optional[ToolSpec] = None, code: str = "ai_busy") -> None:
    try:
        if not ok:
            await failures.record_ai_failure(failures.kind_of_code(code))
        elif tool is not None and service_of(tool):
            await failures.record_ai_success(service_of(tool))
    except Exception:  # noqa: BLE001 - the cool-down is a courtesy; it never changes a call's answer
        pass


def degraded(data: Any) -> tuple[dict[str, str], ...]:
    """Work that was done but not fully: a picture that fell back to a text card, or one with a part missing. The app records why on the
    item (`qa_flag_reason`); a caller must be told, or it will think the picture it asked for is the picture it got."""
    found: list[dict[str, str]] = []
    pools = [data] if isinstance(data, dict) else []
    pools += [v for v in (data.values() if isinstance(data, dict) else []) if isinstance(v, dict)]
    for pool in pools:
        reason = pool.get("qa_flag_reason")
        if pool.get("qa_flagged") and isinstance(reason, str) and reason.strip():
            found.append({"code": "degraded", "message": reason.strip()[:300]})
    return tuple(dict.fromkeys(tuple(w.items()) for w in found) and found)


async def _settle(call: Call, admission: Admission, *, ok: bool, code: Optional[str] = None, result: Optional[dict[str, Any]] = None) -> None:
    credential = admission.credential
    try:
        if admission.call_id:
            await ledger.finish(admission.call_id, outcome="done" if ok else "failed",
                                charged_credits=admission.estimate.credits if ok else 0, code=code)
        if call.idempotency_key:
            if ok:
                await ledger.complete(credential.id, call.tool, call.idempotency_key, result or {})
            else:
                await ledger.abandon(credential.id, call.tool, call.idempotency_key)
    except Exception:  # noqa: BLE001 - the records never change what the caller is told
        import logging

        logging.getLogger(__name__).exception("tool call not settled in the ledger")
