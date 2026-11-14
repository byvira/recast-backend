"""The channel engine: one incoming chat message, from any service, to a reply.

In order, and stopping at the first that applies:

1. A message seen before is ignored (services resend), so nothing is ever done twice.
2. `LINK ABCD-2345` links the chat to the member who made the code.
3. A chat that is not linked gets one short hint a day and nothing else: no tool runs, and nothing says whether a member exists.
4. A switched-off channel says so. STOP unlinks. More than 30 messages in an hour are answered once with "slow down" and then ignored.
5. A voice note is turned into text with the app's own speech chain (the language is detected, so any language the chain hears works).
6. The words go to Ask Ekavo as the linked member, so the same planner, prices, plan limits, safety and approvals apply. A priced plan comes back as
   a short message with the price and two buttons; a yes (the button) starts it, and the result comes back as a short message and a link.

Nothing here can publish. The reply is short and carries a link to the app, never a private draft's full text."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from pymongo.errors import DuplicateKeyError

from app.ask import threads
from app.channels import answer, reminders
from app.channels import link as links
from app.channels import phrases
from app.channels.base import Adapter, Inbound
from app.core.config import settings
from app.db.mongo import get_db
from app.shared.language import detect_language

logger = logging.getLogger(__name__)

PER_HOUR = 30
MAX_VOICE_BYTES = 10 * 1024 * 1024
STOP_WORDS = {"stop", "unlink", "unsubscribe"}
NEW_WORDS = {"new", "new chat", "/new"}
IDLE_HOURS = 12
GO, NO = "go:", "no:"
POLL_SECONDS = 2.0
POLL_LIMIT_SECONDS = 600
_tasks: set[asyncio.Task] = set()

Transcribe = Callable[[bytes, str], Awaitable[tuple[str, Optional[str]]]]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def seen():
    return get_db()["channel_seen"]


def hints():
    return get_db()["channel_hints"]


async def create_indexes() -> None:
    await seen().create_index([("channel", 1), ("message_id", 1)], unique=True)
    await seen().create_index([("channel", 1), ("sender_hash", 1), ("at", -1)])
    await seen().create_index("at", expireAfterSeconds=30 * 24 * 3600)
    await hints().create_index([("channel", 1), ("sender_hash", 1)], unique=True)
    await hints().create_index("at", expireAfterSeconds=24 * 3600)
    await links.create_indexes()


def app_link(path: str = "/dashboard/library") -> str:
    return f"{settings.FRONTEND_URL.rstrip('/')}{path}"


async def default_transcribe(data: bytes, name: str) -> tuple[str, Optional[str]]:
    """The words in a voice note and the language heard, using the same speech chain as the app. Raises when every provider is down."""
    from app.pipelines.audio.transcriber import transcribe_audio_detailed

    words, language = await transcribe_audio_detailed(data, name, strict=True)
    return " ".join(w.word for w in words).strip(), language


async def handle(adapter: Adapter, msg: Inbound, *, services: Any = None, think: Any = None, embed: Any = None, transcribe: Optional[Transcribe] = None, wait: bool = False) -> str:
    """One message. Returns what happened (for tests and logs; never shown to the sender). With `wait`, a started run finishes before this returns."""
    from app.tools.services import LiveServices

    services = services or LiveServices()
    key = links.sender_hash(msg.channel, msg.sender)
    try:
        await seen().insert_one({"channel": msg.channel, "message_id": msg.message_id, "sender_hash": key, "at": _now()})
    except DuplicateKeyError:
        return "duplicate"

    code = links.parse_link_message(msg.text) if msg.kind == "text" else None
    if code:
        return await _link(adapter, msg, code, services)

    link = await links.find(msg.channel, msg.sender)
    language = detect_language(msg.text) if msg.text else None
    if link is None:
        return await _hint(adapter, msg, key, language)

    workspace_id, user_id = link["workspace_id"], link["user_id"]
    language = language or link.get("language")
    off = await services.switched_off(workspace_id, f"channel.{msg.channel}")
    if off:
        await adapter.send(msg.sender, await phrases.tell("off", language, localize=True, think=think))
        return "off"
    if msg.kind == "text" and msg.text.strip().lower() in STOP_WORDS:
        await links.unlink_sender(msg.channel, msg.sender)
        await adapter.send(msg.sender, await phrases.tell("stopped", language))
        return "unlinked_by_stop"
    count = await seen().count_documents({"channel": msg.channel, "sender_hash": key, "at": {"$gte": _now() - timedelta(hours=1)}})
    if count > PER_HOUR:
        if count == PER_HOUR + 1:
            await adapter.send(msg.sender, await phrases.tell("slow_down", language, localize=True, think=think))
        return "limit"
    permissions = await services.member_permissions(workspace_id, user_id)
    if permissions is None or "create_content" not in permissions:
        await links.unlink_sender(msg.channel, msg.sender)
        return "not_allowed"
    await links.touch(link["id"], language)

    if msg.kind == "button":
        return await _button(adapter, msg, link, language, services, wait)
    wanted = reminders.parse_word(msg.text) if msg.kind == "text" else None
    if wanted is not None:
        if wanted and not await reminders.opt_in(link, msg.sender):
            await adapter.send(msg.sender, await phrases.tell("remind_unavailable", language, localize=True, think=think))
            return "reminders_unavailable"
        if not wanted:
            await reminders.opt_out(link["id"])
        await adapter.send(msg.sender, await phrases.tell("remind_on" if wanted else "remind_off", language, localize=True, think=think))
        return "reminders_on" if wanted else "reminders_off"
    if msg.kind == "text" and msg.text.strip().lower() in NEW_WORDS:
        await threads.start_new(workspace_id, user_id, msg.channel, key)
        await adapter.send(msg.sender, await phrases.tell("fresh", language, localize=True, think=think))
        return "new_conversation"
    text = msg.text
    if msg.kind == "voice":
        text, heard = await _hear(adapter, msg, language, transcribe or default_transcribe, think)
        if not text:
            return "voice_failed"
        language = (heard or "")[:2].lower() or detect_language(text) or language
    elif msg.kind == "image":
        return await _reference(adapter, msg, link, key, language, think)
    elif msg.kind != "text" or not text.strip():
        return "ignored"
    thread = await threads.for_contact(workspace_id, user_id, msg.channel, key, idle_hours=IDLE_HOURS)
    return await _ask(adapter, msg, link, text, language, services, think, embed, wait, thread["id"])


# ── the pieces ─────────────────────────────────────────────────────────────

async def _link(adapter: Adapter, msg: Inbound, code: str, services: Any) -> str:
    made = await links.redeem(msg.channel, msg.sender, code, services)
    language = detect_language(msg.text)
    if made is None:
        await adapter.send(msg.sender, phrases.known("link_bad", language))
        return "link_bad"
    await adapter.send(msg.sender, phrases.known("linked", language))
    return "linked"


async def _hint(adapter: Adapter, msg: Inbound, key: str, language: Optional[str]) -> str:
    """One short hint a day to a chat that is not linked, and nothing else."""
    try:
        await hints().insert_one({"channel": msg.channel, "sender_hash": key, "at": _now()})
    except DuplicateKeyError:
        return "unlinked"
    await adapter.send(msg.sender, phrases.known("hint", language))
    return "hint"


async def _reference(adapter: Adapter, msg: Inbound, link: dict[str, Any], key: str, language: Optional[str], think: Any) -> str:
    """A picture sent to follow the look of: only its look is kept, and it guides the next request (and a caption, if there is one, is that request)."""
    from app.pipelines.media import request_refs

    try:
        data, _name = await adapter.fetch_media(msg.media or {})
        made = await request_refs.add(link["workspace_id"], link["user_id"], data)
    except request_refs.RefError as exc:
        await adapter.send(msg.sender, exc.message)
        return "reference_refused"
    except Exception as exc:  # noqa: BLE001 - the person is told plainly
        logger.info("A reference picture could not be read: %s", type(exc).__name__)
        await adapter.send(msg.sender, await phrases.tell("ref_bad", language, localize=True, think=think))
        return "reference_failed"
    thread = await threads.for_contact(link["workspace_id"], link["user_id"], msg.channel, key, idle_hours=IDLE_HOURS)
    await threads.add_pending_ref(thread["id"], made["id"])
    await adapter.send(msg.sender, await phrases.tell("ref_saved", language, localize=True, think=think))
    return "reference_saved"


async def _hear(adapter: Adapter, msg: Inbound, language: Optional[str], transcribe: Transcribe, think: Any) -> tuple[str, Optional[str]]:
    try:
        data, name = await adapter.fetch_media(msg.media or {})
        if not data or len(data) > MAX_VOICE_BYTES:
            raise ValueError("voice note missing or too large")
        text, heard = await transcribe(data, name)
    except Exception as exc:  # noqa: BLE001 - the person is told, plainly
        logger.info("A voice note could not be read: %s", type(exc).__name__)
        text, heard = "", None
    if not text:
        await adapter.send(msg.sender, await phrases.tell("voice_failed", language, localize=True, think=think))
    return text, heard


async def _ask(adapter: Adapter, msg: Inbound, link: dict[str, Any], text: str, language: Optional[str], services: Any, think: Any, embed: Any, wait: bool,
               thread_id: Optional[str] = None) -> str:
    refs = await threads.take_pending_refs(thread_id) if thread_id else []
    from app.ask import runs
    from app.tools.executor import ToolFailure

    try:
        run = await runs.create(link["workspace_id"], link["user_id"], text, "ask", think=think, embed=embed, services=services, thread_id=thread_id, surface=msg.channel, reference_ids=refs)
    except ToolFailure as failure:
        await adapter.send(msg.sender, failure.message)
        return "refused"
    except Exception as exc:  # noqa: BLE001 - out of credits, or the planner is down
        logger.info("A channel request could not be planned: %s", type(exc).__name__)
        await adapter.send(msg.sender, await phrases.tell("failed", language, localize=True, think=think))
        return "failed"
    return await _answer(adapter, msg, link, run, language, services, think, wait)


async def _answer(adapter: Adapter, msg: Inbound, link: dict[str, Any], run: dict[str, Any], language: Optional[str], services: Any, think: Any, wait: bool) -> str:
    status = run.get("status")
    reply = str(run.get("reply") or run.get("question") or "")
    run_language = run.get("language") or language
    if status == "ready" and (run.get("plan") or {}).get("steps") and run.get("trust") != "suggest":
        buttons = [(phrases.known("go", run_language), GO + run["id"]), (phrases.known("cancel", run_language), NO + run["id"])]
        await adapter.send(msg.sender, reply, buttons)
        return "plan_sent"
    if status in ("running", "done"):
        return await _finish(adapter, msg, link, run, run_language, services, think, wait)
    if reply:
        await adapter.send(msg.sender, reply)
    return status or "none"


async def _button(adapter: Adapter, msg: Inbound, link: dict[str, Any], language: Optional[str], services: Any, wait: bool) -> str:
    from app.ask import runs
    from app.tools.executor import ToolFailure

    action, _, run_id = msg.payload.partition(":")
    if action not in ("go", "no") or not run_id:
        return "ignored"
    try:
        if action == "no":
            await runs.stop(link["workspace_id"], link["user_id"], run_id)
            await adapter.send(msg.sender, phrases.known("cancelled", language))
            return "cancelled"
        run = await runs.confirm(link["workspace_id"], link["user_id"], run_id, services=services, wait=wait)
    except ToolFailure as failure:
        await adapter.send(msg.sender, failure.message if failure.code != "not_ready" else phrases.known("nothing_waiting", language))
        return failure.code
    return await _finish(adapter, msg, link, run, language, services, None, wait)


async def _finish(adapter: Adapter, msg: Inbound, link: dict[str, Any], run: dict[str, Any], language: Optional[str], services: Any, think: Any, wait: bool) -> str:
    """Report a run that is done, or watch a running one in the background and report when it ends."""
    if run.get("status") == "running" and not wait:
        task = asyncio.create_task(_watch(adapter, msg, link, run["id"], language))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        return "started"
    await _report(adapter, msg, run, language)
    return "done" if run.get("status") == "done" else str(run.get("status"))


async def _watch(adapter: Adapter, msg: Inbound, link: dict[str, Any], run_id: str, language: Optional[str]) -> None:
    from app.ask import runs

    waited = 0.0
    while waited < POLL_LIMIT_SECONDS:
        await asyncio.sleep(POLL_SECONDS)
        waited += POLL_SECONDS
        try:
            run = await runs.get(link["workspace_id"], link["user_id"], run_id)
        except Exception:  # noqa: BLE001
            return
        if run.get("status") != "running":
            await _report(adapter, msg, run, language)
            return


async def _report(adapter: Adapter, msg: Inbound, run: dict[str, Any], language: Optional[str]) -> None:
    """A short result and a link to open it in the app. Never the draft's own text."""
    if run.get("status") == "done":
        said = answer.short_answer(run)
        if said:                                            # a question is answered in the chat itself
            await adapter.send(msg.sender, phrases.known("found", language) + chr(10) + said)
        else:
            await adapter.send(msg.sender, f"{phrases.known('done', language)} {app_link()}")
    elif run.get("status") in ("failed", "stopped"):
        await adapter.send(msg.sender, phrases.known("failed", language))


async def drain() -> None:
    """Wait for background runs to finish (tests)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)
