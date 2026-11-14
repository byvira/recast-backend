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
import time
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

#: how often "typing" is shown again while a message is being worked on (Telegram keeps it for 5 seconds), and when a short note says it is still going
TYPING_EVERY = 4.0
STILL_WORKING_AFTER = 8.0
SHOWN_FOR = ("text", "voice", "image")


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


async def _keep_alive(adapter: Adapter, msg: Inbound, state: dict[str, Any]) -> None:
    """Shows the sender that Ekavo is on it, from the first moment: "typing" at once and again before it fades, and one short note when it takes long.
    A service with no such signal is simply not shown one. Stopped (cancelled) as soon as the message has been dealt with."""
    working = getattr(adapter, "working", None)
    every = float(getattr(adapter, "typing_seconds", TYPING_EVERY))
    waited, told = 0.0, False
    while True:
        if working is not None:
            await working(msg.sender, msg.message_id)
        step = min(every, max(STILL_WORKING_AFTER - waited, 0.05)) if not told else every
        await asyncio.sleep(step)
        waited += step
        if not told and waited >= STILL_WORKING_AFTER:
            told = True
            if state.get("linked"):                          # a chat that is not linked is only ever given its one hint
                await adapter.send(msg.sender, await phrases.tell("still_working", state.get("language"), localize=True))


async def handle(adapter: Adapter, msg: Inbound, *, services: Any = None, think: Any = None, embed: Any = None, transcribe: Optional[Transcribe] = None, wait: bool = False) -> str:
    """One message. Returns what happened (for tests and logs; never shown to the sender). With `wait`, a started run finishes before this returns.
    While it is worked on, the sender sees Ekavo typing, so nobody is left looking at a silent chat."""
    state: dict[str, Any] = {}
    started = time.monotonic()
    outcome = "error"
    keep: Optional[asyncio.Task] = asyncio.create_task(_keep_alive(adapter, msg, state)) if msg.kind in SHOWN_FOR else None
    try:
        outcome = await _handle(adapter, msg, services=services, think=think, embed=embed, transcribe=transcribe, wait=wait, state=state)
        return outcome
    finally:
        # how long a message took, from the moment it reached the engine to the last thing said (a run that goes on in the background is not counted)
        logger.info("channel message handled channel=%s kind=%s outcome=%s ms=%d", msg.channel, msg.kind, outcome, int((time.monotonic() - started) * 1000))
        if keep is not None:
            keep.cancel()
            await asyncio.gather(keep, return_exceptions=True)


async def _first_time(msg: Inbound, key: str) -> bool:
    """Records the message as seen. False when it was seen before (services resend), so nothing is ever done twice."""
    try:
        await seen().insert_one({"channel": msg.channel, "message_id": msg.message_id, "sender_hash": key, "at": _now()})
        return True
    except DuplicateKeyError:
        return False


async def _nothing() -> None:
    return None


def _confident(text: str) -> bool:
    """Whether a message is long enough, or written in letters outside the Latin alphabets, for its language to be trusted over what this chat has been using."""
    t = (text or "").strip()
    return len(t) >= 15 or any(ord(c) > 0x024F for c in t)


async def _language_for(text: str, link: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """(the language to answer in, the language to remember for this chat). The message's own language wins when it can be trusted. A short word such as "ok"
    keeps the language the chat has been using. A brand-new chat is answered in the member's language, then the workspace's, so nobody has to start in English."""
    detected = detect_language(text) if text else None
    remembered = link.get("language")
    if detected and (_confident(text) or not remembered):
        return detected, detected
    if remembered:
        return remembered, None
    try:
        from app.shared.reader_language import reader_language

        return await reader_language("member", user_id=link["user_id"], workspace_id=link["workspace_id"]), None
    except Exception:  # noqa: BLE001 - not knowing the language must never stop an answer
        return detected, None


async def _handle(adapter: Adapter, msg: Inbound, *, services: Any = None, think: Any = None, embed: Any = None, transcribe: Optional[Transcribe] = None, wait: bool = False,
                  state: Optional[dict[str, Any]] = None) -> str:
    from app.tools.services import LiveServices

    services = services or LiveServices()
    key = links.sender_hash(msg.channel, msg.sender)
    code = links.parse_link_message(msg.text) if msg.kind == "text" else None
    # the "seen before?" check and the "who is this?" lookup do not depend on each other, so they go out together and cost one wait, not two
    first_time, link = await asyncio.gather(_first_time(msg, key), links.find(msg.channel, msg.sender) if not code else _nothing())
    if not first_time:
        return "duplicate"
    if code:
        return await _link(adapter, msg, code, services)

    language = detect_language(msg.text) if msg.text else None
    if link is None:
        return await _hint(adapter, msg, key, language)

    workspace_id, user_id = link["workspace_id"], link["user_id"]
    language, learned = await _language_for(msg.text, link)
    if state is not None:
        state.update(language=language, linked=True)
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
    await links.touch(link["id"], learned)

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
    await adapter.send(msg.sender, await _welcome(made, language))
    return "linked"


async def _welcome(made: dict[str, Any], fallback: Optional[str]) -> str:
    """The welcome in the member's own language, then the workspace's, then English: the person has only said `LINK` so far, so the words they typed
    say nothing about the language they read. Anything that goes wrong falls back to what was typed, and never stops the link."""
    from app.db.mongo import workspaces
    from app.shared.reader_language import reader_language

    try:
        language = await reader_language("member", user_id=made["user_id"], workspace_id=made["workspace_id"])
        row = await workspaces.find_one({"id": made["workspace_id"]}, {"name": 1})
        return await phrases.linked_text(language, (row or {}).get("name"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not write the welcome in the member's language: %s", exc)
        return await phrases.linked_text(fallback, None)


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
        from app.billing import credits

        if credits.is_out_of_credits(exc):
            await adapter.send(msg.sender, await _credits_message(exc, language, think))
            return "out_of_credits"
        logger.info("A channel request could not be planned: %s", type(exc).__name__)
        await adapter.send(msg.sender, await phrases.tell("failed", language, localize=True, think=think))
        return "failed"
    return await _answer(adapter, msg, link, run, language, services, think, wait)


async def _credits_message(exc: BaseException, language: Optional[str], think: Any) -> str:
    """The credits refusal in plain words (what is left, when it starts again) and where to add more, in the person's language."""
    from app.ask import runs

    detail = getattr(exc, "detail", None)
    said = str(detail.get("message")) if isinstance(detail, dict) and detail.get("message") else "You have used all your credits."
    return await runs.localize(f"{said} You can add credits in the app: {app_link()}", language or "en", think=think)


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
    elif run.get("status") == "failed":
        why = _credits_reason(run)
        await adapter.send(msg.sender, f"{why} {app_link()}" if why else phrases.known("failed", language))
    elif run.get("status") == "partial":
        why = _credits_reason(run)
        await adapter.send(msg.sender, f"{phrases.known('partial', language)} {why} {app_link()}".replace("  ", " "))
    elif run.get("status") == "stopped":
        await adapter.send(msg.sender, phrases.known("failed", language))


def _credits_reason(run: dict[str, Any]) -> str:
    """The credits message of the first step that was refused for it, already in the person's language; nothing for any other kind of failure."""
    for step in (run.get("result") or {}).get("steps") or []:
        error = step.get("error") or {}
        if error.get("code") == "limit_reached" and error.get("message"):
            return str(error["message"])
    error = run.get("error") or {}
    return str(error["message"]) if error.get("code") == "limit_reached" and error.get("message") else ""


async def drain() -> None:
    """Wait for background runs to finish (tests)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)
