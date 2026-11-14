"""Nobody is left looking at a silent chat: "typing" is shown at once and kept up while a message is worked on, one short note says it is still going when it
takes long, and it all stops when the message is done. No network is called."""
import asyncio
import json

import httpx

from app.channels import engine, phrases, telegram, whatsapp
from app.channels.base import Inbound
from app.core.config import settings


class Chat:
    name = "test"

    def __init__(self):
        self.said: list[str] = []
        self.typing = 0

    async def send(self, to, text, buttons=None):
        self.said.append(text)
        return True

    async def working(self, to, message_id=None):
        self.typing += 1

    async def fetch_media(self, media):
        raise FileNotFoundError


def _msg(kind="text", text="write a post"):
    return Inbound(channel="telegram", sender="1", message_id="m1", kind=kind, text=text)


async def test_typing_starts_at_once_repeats_and_says_once_that_it_is_still_going(monkeypatch):
    monkeypatch.setattr(engine, "TYPING_EVERY", 0.05)
    monkeypatch.setattr(engine, "STILL_WORKING_AFTER", 0.12)

    async def slow(adapter, msg, **kw):
        kw["state"].update(language="en", linked=True)            # what the engine learns once the chat is known to be linked
        await asyncio.sleep(0.4)
        return "done"

    monkeypatch.setattr(engine, "_handle", slow)
    chat = Chat()
    assert await engine.handle(chat, _msg()) == "done"
    assert chat.typing >= 3
    assert chat.said == [phrases.known("still_working", "en")]


async def test_the_still_working_note_is_in_the_chats_language(monkeypatch):
    monkeypatch.setattr(engine, "TYPING_EVERY", 0.05)
    monkeypatch.setattr(engine, "STILL_WORKING_AFTER", 0.1)

    async def slow(adapter, msg, **kw):
        kw["state"].update(language="ta", linked=True)
        await asyncio.sleep(0.3)
        return "done"

    monkeypatch.setattr(engine, "_handle", slow)
    chat = Chat()
    await engine.handle(chat, _msg())
    assert chat.said == [phrases.known("still_working", "ta")]


async def test_a_chat_that_is_not_linked_gets_no_still_working_note(monkeypatch):
    monkeypatch.setattr(engine, "TYPING_EVERY", 0.05)
    monkeypatch.setattr(engine, "STILL_WORKING_AFTER", 0.1)

    async def slow(adapter, msg, **kw):
        await asyncio.sleep(0.3)
        return "hint"

    monkeypatch.setattr(engine, "_handle", slow)
    chat = Chat()
    await engine.handle(chat, _msg())
    assert chat.said == []


# ── the language a chat is answered in ───────────────────────────────────────

LINK = {"user_id": "u1", "workspace_id": "w1", "language": None}


async def test_a_message_that_is_clearly_in_a_language_is_answered_in_it_and_remembered():
    language, learned = await engine._language_for("எனக்கு ஒரு பதிவு எழுதி தர முடியுமா", {**LINK, "language": "en"})
    assert language == learned and language != "en"


async def test_a_short_word_keeps_the_language_the_chat_has_been_using():
    assert await engine._language_for("ok", {**LINK, "language": "ta"}) == ("ta", None)
    assert await engine._language_for("yes", {**LINK, "language": "hi"}) == ("hi", None)


async def test_a_brand_new_chat_with_a_short_message_is_answered_in_the_members_then_the_workspaces_language(monkeypatch):
    from app.shared import reader_language

    asked: dict = {}

    async def fake(audience, *, user_id=None, workspace_id=None, explicit=None, text=None):
        asked.update(audience=audience, user_id=user_id, workspace_id=workspace_id)
        return "hi"

    monkeypatch.setattr(reader_language, "reader_language", fake)
    assert await engine._language_for("ok", LINK) == ("hi", None)
    assert asked == {"audience": "member", "user_id": "u1", "workspace_id": "w1"}


async def test_when_nothing_is_known_the_chat_is_still_answered(monkeypatch):
    from app.shared import reader_language

    async def broken(*a, **k):
        raise RuntimeError("no database")

    monkeypatch.setattr(reader_language, "reader_language", broken)
    language, learned = await engine._language_for("", LINK)
    assert learned is None and language is None


async def test_a_quick_message_shows_typing_but_sends_no_still_working_note(monkeypatch):
    async def quick(adapter, msg, **kw):
        return "done"

    monkeypatch.setattr(engine, "_handle", quick)
    chat = Chat()
    await engine.handle(chat, _msg())
    await asyncio.sleep(0.1)
    assert chat.said == []


async def test_typing_stops_when_the_message_is_done(monkeypatch):
    monkeypatch.setattr(engine, "TYPING_EVERY", 0.03)

    async def quick(adapter, msg, **kw):
        await asyncio.sleep(0.1)
        return "done"

    monkeypatch.setattr(engine, "_handle", quick)
    chat = Chat()
    await engine.handle(chat, _msg())
    seen = chat.typing
    await asyncio.sleep(0.2)
    assert chat.typing == seen


async def test_a_button_press_shows_no_typing(monkeypatch):
    async def quick(adapter, msg, **kw):
        await asyncio.sleep(0.05)
        return "done"

    monkeypatch.setattr(engine, "_handle", quick)
    chat = Chat()
    await engine.handle(chat, _msg(kind="button", text=""))
    assert chat.typing == 0


async def test_a_service_with_no_typing_signal_still_works(monkeypatch):
    class Plain:
        name = "plain"

        async def send(self, to, text, buttons=None):
            return True

    async def quick(adapter, msg, **kw):
        return "done"

    monkeypatch.setattr(engine, "_handle", quick)
    assert await engine.handle(Plain(), _msg()) == "done"


async def test_telegram_asks_for_typing_in_the_right_chat(monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "bot")
    calls: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"ok": True})

    await telegram.TelegramAdapter(client=httpx.AsyncClient(transport=httpx.MockTransport(handler))).working("42")
    assert calls == [("/botbot/sendChatAction", {"chat_id": "42", "action": "typing"})]


async def test_whatsapp_marks_the_message_read_and_shows_typing_and_never_raises(monkeypatch):
    for name, value in (("WHATSAPP_ACCESS_TOKEN", "tok"), ("WHATSAPP_PHONE_NUMBER_ID", "111")):
        monkeypatch.setattr(settings, name, value)
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"success": True})

    adapter = whatsapp.WhatsAppAdapter(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await adapter.working("91", "wamid.in")
    assert bodies == [{"messaging_product": "whatsapp", "status": "read", "message_id": "wamid.in", "typing_indicator": {"type": "text"}}]
    await adapter.working("91", None)                        # nothing to mark: nothing is called
    assert len(bodies) == 1

    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "no"})

    await whatsapp.WhatsAppAdapter(client=httpx.AsyncClient(transport=httpx.MockTransport(refuse))).working("91", "wamid.in")
    assert whatsapp.WhatsAppAdapter.typing_seconds < 25
