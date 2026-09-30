"""AI help for support staff. Small, bounded and never in charge.

What is here, and what is not:

* ``diagnose`` is plain code (no AI). It reads the context snapshot and lists
  the facts most likely to explain the ticket: an expired connection, paused
  generation, a hit usage limit, recent failures.
* ``draft_reply`` asks the model for a suggested reply. It returns text only.
  It cannot send anything, change a status or call any tool. A person reads it,
  edits it and presses Send.
* ``ai_category`` is the fallback when the keyword rules cannot place a ticket.

Safety rules that apply to every call: the ticket text is untrusted and is
fenced off in the prompt; emails, phone numbers and secret-looking strings are
masked before anything is sent; every call is capped per call, per staff member
per hour and per day; tokens are counted against a platform bucket (never a
customer's AI budget); each call is logged with its prompt version.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

from fastapi import HTTPException

from app.db.mongo import support_ai_usage
from app.prompts.registry import load_prompt
from app.shared.localized_strings import looks_leaked
from app.shared import support_guides
from app.shared.llm import GroqModel, call_llm, usage_workspace

logger = logging.getLogger(__name__)

# Tokens are counted here, not against the member's workspace budget.
USAGE_BUCKET = "support-platform"

DRAFT_PROMPT = "support/draft_reply"
DRAFT_PROMPT_VERSION = "support/draft_reply@1"
CATEGORY_PROMPT = "support/category"
CATEGORY_PROMPT_VERSION = "support/category@1"

MAX_OUTPUT_TOKENS_DRAFT = 500
MAX_OUTPUT_TOKENS_CATEGORY = 20
MAX_THREAD_MESSAGES = 12
MAX_MESSAGE_CHARS = 1200
CALLS_PER_STAFF_PER_HOUR = 30
CALLS_PER_DAY = 200

CATEGORIES = ["Brand voice", "Connecting accounts", "Publishing", "Billing", "Something's broken"]

# ── Masking ──────────────────────────────────────────────────────────────────
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{7,}\d(?!\w)")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_KEYED = re.compile(r"\b(?:sk|pk|gsk|ghp|xox[baprs]|AIza)[-_A-Za-z0-9]{10,}")
_PASSWORD = re.compile(r"(?i)\b(password|passcode|secret|token|api[_ -]?key)\b\s*[:=]\s*\S+")
_LONG_TOKEN = re.compile(r"\b[A-Za-z0-9+/_=-]{32,}\b")
_URL = re.compile(r"https?://\S+")


def redact(text: str) -> str:
    """Mask emails, phone numbers and secret-looking strings."""
    text = _PASSWORD.sub(lambda m: f"{m.group(1)}: [hidden]", text)
    text = _BEARER.sub("[hidden]", text)
    text = _KEYED.sub("[hidden]", text)
    text = _EMAIL.sub("[email]", text)
    text = _PHONE.sub("[phone]", text)
    text = _LONG_TOKEN.sub("[hidden]", text)
    return text


# ── Limits and logging ───────────────────────────────────────────────────────
async def _enforce_caps(staff_id: Optional[str]) -> None:
    now = datetime.now(timezone.utc)
    if await support_ai_usage.count_documents({"created_at": {"$gte": now - timedelta(days=1)}}) >= CALLS_PER_DAY:
        raise HTTPException(status_code=429, detail="The daily limit for AI help has been reached. Try again tomorrow, or write the reply by hand.")
    if staff_id and await support_ai_usage.count_documents(
        {"staff_id": staff_id, "created_at": {"$gte": now - timedelta(hours=1)}}
    ) >= CALLS_PER_STAFF_PER_HOUR:
        raise HTTPException(status_code=429, detail="You have asked for a lot of drafts this hour. Please wait a little.")


async def _log(kind: str, ticket_id: str, staff_id: Optional[str], prompt_version: str, model: str, ok: bool,
               chars_in: int, chars_out: int) -> None:
    try:
        await support_ai_usage.insert_one(
            {
                "id": str(uuid4()), "kind": kind, "ticket_id": ticket_id, "staff_id": staff_id,
                "prompt_version": prompt_version, "model": model, "ok": ok,
                "chars_in": chars_in, "chars_out": chars_out, "created_at": datetime.now(timezone.utc),
            }
        )
    except Exception:
        logger.warning("Couldn't log a support AI call", exc_info=True)


# ── Diagnosis (no AI) ────────────────────────────────────────────────────────
_HEALTH_TEXT = {
    "expired": "needs to be reconnected because its access has expired",
    "expiring_soon": "will need reconnecting soon, its access is about to expire",
    "disconnected": "is disconnected",
}


async def diagnose(ticket: dict, snapshot: Optional[dict]) -> list[dict]:
    """Facts from the context snapshot that may explain the ticket. Each item
    is {"level": "problem" | "note", "text": str}. Plain code, no AI."""
    findings: list[dict] = []
    if not snapshot:
        return findings
    workspace = snapshot.get("workspace") or {}
    platforms = snapshot.get("platforms") or []
    source = ticket.get("source_context") or {}
    named = source.get("id") if source.get("type") == "platform" else None

    for p in platforms:
        text = _HEALTH_TEXT.get(p.get("health", ""))
        if text:
            label = str(p.get("platform") or "An account").capitalize()
            level = "problem" if p.get("health") in ("expired", "disconnected") else "note"
            asked = " (the one the member reported)" if named and str(p.get("platform")) == named else ""
            findings.append({"level": level, "text": f"The {label} connection {text}{asked}."})
    if named and not any(str(p.get("platform")) == named for p in platforms):
        findings.append({"level": "problem", "text": f"The member reported {named}, but no {named} account is connected."})
    if not platforms and ticket.get("category") in ("Publishing", "Connecting accounts"):
        findings.append({"level": "problem", "text": "No accounts are connected to this workspace."})

    if workspace.get("generation_halted"):
        findings.append({"level": "problem", "text": "New generation is paused for this workspace by its owner."})

    try:
        from app.agents.supervisor.service import assert_ai_budget_available

        await assert_ai_budget_available(ticket["workspace_id"])
    except HTTPException:
        findings.append({"level": "problem", "text": "The workspace has used up its AI writing limit, so new writing is paused until usage drops."})
    except Exception:
        logger.debug("Couldn't check the AI limit for the diagnosis", exc_info=True)

    errors = snapshot.get("errors") or []
    if errors:
        latest = errors[0].get("title") or "a failure"
        findings.append(
            {"level": "problem" if len(errors) >= 3 else "note",
             "text": f"{len(errors)} thing{'s' if len(errors) != 1 else ''} failed in the last 24 hours. The latest: {latest}."}
        )
    if not findings:
        findings.append({"level": "note", "text": "Nothing obvious stands out in the workspace's connections, limits or recent activity."})
    return findings


# ── Draft reply (AI) ─────────────────────────────────────────────────────────
_DASHES = re.compile(r"\s*[—–]\s*")


_TICKET_TAG = re.compile(r"<\s*/?\s*ticket_data\s*>", re.IGNORECASE)


def _safe(text: str) -> str:
    """Member text made ready for the prompt: private details masked, and any copy of the prompt's own
    fence tag removed so the text cannot close the fence and speak as the instructions."""
    return _TICKET_TAG.sub("", redact(text or ""))


def _clean_output(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```[a-z]*\n?|```$", "", text).strip()
    text = _URL.sub("", text)
    text = _DASHES.sub(", ", text)
    return text[:3000].strip()


def _thread_for_prompt(ticket: dict) -> list[dict]:
    rows: list[dict] = []
    for m in [m for m in ticket.get("messages", []) if not m.get("is_internal") and not m.get("is_deleted")][-MAX_THREAD_MESSAGES:]:
        who = "Recast" if m.get("sender") == "staff" else "Member"
        rows.append({"who": who, "text": _safe(m.get("text", ""))[:MAX_MESSAGE_CHARS]})
    return rows


async def draft_reply(ticket: dict, snapshot: Optional[dict], staff_id: str) -> dict:
    """Suggest a reply. Raises HTTPException with a plain message on failure."""
    await _enforce_caps(staff_id)
    findings = await diagnose(ticket, snapshot)
    thread = _thread_for_prompt(ticket)
    latest = next((m["text"] for m in reversed(thread) if m["who"] == "Member"), ticket.get("subject", ""))
    guides = support_guides.search(f"{ticket.get('subject', '')} {latest}", limit=3)
    first_name = (ticket.get("created_by_name") or "").strip().split(" ")[0] or "there"

    prompt = load_prompt(
        DRAFT_PROMPT,
        first_name=_safe(first_name),
        subject=_safe(ticket.get("subject", "")),
        category=ticket.get("category", ""),
        thread=thread,
        findings=[f["text"] for f in findings],
        guides=[{"title": g["title"], "body": g["body"]} for g in guides],
        is_follow_up=any(m["who"] == "Recast" for m in thread),
    )
    try:
        async with usage_workspace(USAGE_BUCKET):
            raw = await call_llm(
                prompt, model=GroqModel.FAST, temperature=0.3, max_tokens=MAX_OUTPUT_TOKENS_DRAFT
            )
    except HTTPException:
        await _log("draft", ticket["id"], staff_id, DRAFT_PROMPT_VERSION, GroqModel.FAST.value, False, len(prompt), 0)
        raise HTTPException(status_code=503, detail="The drafting help isn't available right now. You can write the reply by hand.")
    except Exception:
        logger.warning("Support draft failed for ticket %s", ticket.get("id"), exc_info=True)
        await _log("draft", ticket["id"], staff_id, DRAFT_PROMPT_VERSION, GroqModel.FAST.value, False, len(prompt), 0)
        raise HTTPException(status_code=503, detail="The drafting help isn't available right now. You can write the reply by hand.")

    draft = _clean_output(raw or "")
    # A reply that repeats our own instructions is never shown.
    if draft and looks_leaked(draft, latest):
        draft = ""
    ok = bool(draft)
    await _log("draft", ticket["id"], staff_id, DRAFT_PROMPT_VERSION, GroqModel.FAST.value, ok, len(prompt), len(draft))
    if not ok:
        raise HTTPException(status_code=502, detail="The drafting help came back empty. Try again, or write the reply by hand.")
    return {
        "draft": draft,
        "prompt_version": DRAFT_PROMPT_VERSION,
        "findings": findings,
        "guides": [{"id": g["id"], "title": g["title"]} for g in guides],
    }


# ── Category fallback (AI) ───────────────────────────────────────────────────
async def ai_category(ticket: dict) -> Optional[str]:
    """Only used when the keyword rules found nothing. Returns one of CATEGORIES
    or None. Never raises."""
    try:
        await _enforce_caps(None)
        first_text = next((m["text"] for m in ticket.get("messages", []) if not m.get("is_internal")), "")
        prompt = load_prompt(
            CATEGORY_PROMPT,
            areas=CATEGORIES,
            subject=_safe(ticket.get("subject", "")),
            body=_safe(first_text)[:MAX_MESSAGE_CHARS],
        )
        async with usage_workspace(USAGE_BUCKET):
            raw = await call_llm(prompt, model=GroqModel.FAST, temperature=0, max_tokens=MAX_OUTPUT_TOKENS_CATEGORY)
        answer = (raw or "").strip().strip(".").strip()
        # Only ever accept an exact area from the list; anything else is ignored.
        match = next((c for c in CATEGORIES if c.lower() == answer.lower()), None)
        await _log("category", ticket["id"], None, CATEGORY_PROMPT_VERSION, GroqModel.FAST.value, match is not None, len(prompt), len(answer))
        return match
    except Exception:
        logger.debug("AI category suggestion skipped", exc_info=True)
        return None
