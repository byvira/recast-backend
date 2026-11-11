"""What a failure looks like from outside: one shape for every surface (API, CLI, MCP, assistants).

Every failure carries a stable `code`, a plain `message`, whether trying again can help (`retryable`), how long to wait (`retry_after_seconds`),
and what to do next. Provider text, stack traces and internal names never leave: a model that ran out of quota or a backend that broke is
described in plain words, and the technical detail stays in the error log under the request id.

Two things live here besides the wording: a short cool-down when the writing service keeps failing (so a flood of calls does not pile onto a
provider that is already struggling), and the rule for when Ekavo itself may retry (only a call that changes nothing)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.db.mongo import get_db

AI_COOLDOWN_AFTER = 3          # this many writing-service failures within the window start a cool-down
AI_WINDOW_SECONDS = 120
AI_COOLDOWN_SECONDS = 60
AUTO_RETRIES = 2
RETRY_WAITS = (1.0, 3.0)


@dataclass(frozen=True)
class Failure:
    code: str
    message: str
    retryable: bool = False
    retry_after_seconds: Optional[int] = None
    #: what to do next, in plain words
    next: str = ""
    status: int = 400
    extra: dict[str, Any] = field(default_factory=dict)

    def body(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message, "retryable": self.retryable}
        if self.retry_after_seconds is not None:
            out["retry_after_seconds"] = self.retry_after_seconds
        if self.next:
            out["next"] = self.next
        return {**out, **self.extra}


# the writing service (any model) being busy, out of quota or down: said in plain words, never with the provider's own text
_AI_WORDS = re.compile(r"\b(llm|language model|rate limit|quota|token|groq|gemini|openai|mistral|model)\b", re.IGNORECASE)


#: the services that can be busy on their own, with the words for each. Writing keeps the old code `ai_busy`; the others are named for what is busy.
SERVICES: dict[str, tuple[str, str]] = {
    "writing": ("ai_busy", "The writing service is busy right now."),
    "picture": ("picture_busy", "The picture service is busy right now. The rest of your request is not affected."),
    "voice": ("voice_busy", "The voice service is busy right now. The rest of your request is not affected."),
    "transcription": ("transcription_busy", "The speech-to-text service is busy right now."),
}


def ai_busy(retry_after: int = 60, kind: str = "writing") -> Failure:
    code, words = SERVICES.get(kind, SERVICES["writing"])
    return Failure(code, f"{words} Nothing was lost and you were not charged for it. Try again in about a minute.", True, retry_after,
                   "Try the same call again after the wait. A repeat with the same idempotency key is safe.", 503)


def timed_out() -> Failure:
    return Failure("timed_out", "That took longer than expected. The work may still be running, so check it before starting it again.", True, 20,
                   "Look at runs.list or the run's status first. Starting it again too soon could make it twice.", 504)


def internal_error() -> Failure:
    return Failure("internal_error", "Something went wrong on our side. Nothing was charged. It has been logged and we will look at it.", True, 30,
                   "Try again in a little while. If it keeps happening, quote the request id.", 500)


def from_upstream(status: int, detail: Any, retry_after: Optional[int] = None) -> Failure:
    """The app's own answer, turned into the shape callers see. A message the app wrote for a person is kept; anything that mentions a
    provider or a model is replaced."""
    text = detail.get("message") if isinstance(detail, dict) else detail if isinstance(detail, str) else ""
    text = str(text or "").strip()
    if status == 503 and (not text or _AI_WORDS.search(text)):
        return ai_busy(retry_after or 60)
    if status in (502, 503, 504):
        return timed_out() if status == 504 else Failure("service_unavailable", "A service we depend on did not answer. Nothing was charged.", True, retry_after or 30,
                                                          "Try again in a moment.", status)
    if status >= 500:
        return internal_error()
    if status == 429:
        return Failure("too_many_requests", text or "Too many requests. Wait a moment and try again.", True, retry_after or 30, "Wait, then try again.", 429)
    if status == 402:
        code = detail.get("limit") if isinstance(detail, dict) else None
        return Failure("credits_or_plan_limit" if code != "credits_month" else "credits_exhausted", text or "A limit has been reached.", False, None,
                       "Wait for the limit to reset or ask the owner to raise it. Retrying will not help.", 402)
    if status == 409:
        return Failure("conflict", text or "That conflicts with what is there now.", False, None, "Look at the current state, then repeat your change.", 409)
    if status == 404:
        return Failure("not_found", text or "That was not found.", False, None, "Check the id.", 404)
    if status in (401, 403):
        return Failure("not_allowed", text or "That is not allowed.", False, None, "Ask the owner for the right permission.", status)
    if status in (400, 422):
        return Failure("input_invalid", text or "Some of what was sent is not valid.", False, None, "Fix the input and send it again.", status)
    return Failure("failed", text or "That could not be done.", False, None, "", status)


# how Ekavo's own refusals (see gatekeeper) answer the two questions: may the caller try again, and when
_REFUSAL_RETRY: dict[str, tuple[bool, Optional[int], str]] = {
    "rate_limited": (True, 30, "Wait, then try again."),
    "ai_busy": (True, 60, "Try again after the wait."),
    "request_in_progress": (True, 5, "The same request is still running. Check its result in a few seconds."),
    "confirm_required": (False, None, "Send the call again with confirmed_credits set to the amount shown."),
    "estimate_changed": (False, None, "Send the call again with the new amount."),
    "approval_required": (False, None, "A person has to say yes in Ekavo. Collect the approval with the request id, then repeat the call."),
    "key_cap": (False, None, "This key's monthly credits are used. Wait for next month or ask the owner to raise the cap."),
    "credits_short": (False, None, "Credits are used up. Wait for them to reset or ask the owner."),
    "plan_limit": (False, None, "Wait for the limit to reset or ask the owner."),
    "tool_off": (True, 300, "It was switched off for now. Try again later."),
}


def for_refusal(code: str) -> dict[str, Any]:
    retryable, after, nxt = _REFUSAL_RETRY.get(code, (False, None, ""))
    out: dict[str, Any] = {"retryable": retryable}
    if after is not None:
        out["retry_after_seconds"] = after
    if nxt:
        out["next"] = nxt
    return out


def safe_to_repeat(risk: str) -> bool:
    """Ekavo itself may retry a failed call only when repeating it cannot change anything or spend anything."""
    return risk == "read"


# ── a short cool-down when the writing service keeps failing ─────────────

def _failures():
    return get_db()["tool_ai_failures"]


def kind_of_code(code: str) -> str:
    return next((k for k, (c, _) in SERVICES.items() if c == code), "writing")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def create_indexes() -> None:
    await _failures().create_index("at", expireAfterSeconds=AI_WINDOW_SECONDS * 3)


async def record_ai_failure(kind: str = "writing") -> None:
    await _failures().insert_one({"at": _now(), "kind": kind})


async def ai_cooldown_left(kind: str = "writing") -> int:
    """Seconds left of the cool-down, or 0. Starts when the writing service failed several times in a row, so new calls wait instead of
    piling onto a provider that is already struggling. A success ends it early."""
    since = _now() - timedelta(seconds=AI_WINDOW_SECONDS)
    recent = await _failures().find({"at": {"$gte": since}, "kind": kind}, {"at": 1}).sort("at", -1).to_list(length=AI_COOLDOWN_AFTER + 2)
    if len(recent) < AI_COOLDOWN_AFTER:
        return 0
    last = recent[0]["at"] if recent[0]["at"].tzinfo else recent[0]["at"].replace(tzinfo=timezone.utc)
    left = AI_COOLDOWN_SECONDS - int((_now() - last).total_seconds())
    return max(0, left)


async def record_ai_success(kind: Optional[str] = None) -> None:
    """A success ends that service's cool-down (or every service's, when no kind is given)."""
    await _failures().delete_many({"kind": kind} if kind else {})


# ── the one named error for "the writing service could not answer" ───────

from fastapi import HTTPException  # noqa: E402


class AIUnavailable(HTTPException):
    """Every model was tried (the main one, the fast one, the backup) and none could answer. A 503 like before, with the same words for the
    screens that already read them, but named, so every caller (screens, tools, agents) can tell it from any other 503 and knows trying
    again in a minute can help."""

    code = "ai_busy"
    retryable = True

    def __init__(self, detail: str = "LLM service unavailable.", retry_after: int = 60, kind: str = "writing"):
        super().__init__(status_code=503, detail=detail)
        self.retry_after = retry_after
        self.code = SERVICES.get(kind, SERVICES["writing"])[0]
