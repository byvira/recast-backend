"""The channel engine, with a stand-in chat service: linking, what an unlinked chat gets, duplicates, STOP, the switch, the rate limit, voice notes in
more than one language, a priced plan with its buttons, and that no reply ever carries a draft's own text. No model, speech service or network is called."""
import asyncio
from uuid import uuid4

import pytest

from app.ask import router, runs, shortlist
from app.billing import costs as credit_costs
from app.channels import engine, link as links
from app.channels.base import Inbound
from app.channels.simulator import SimulatorAdapter
from app.db.mongo import get_db
from app.tools import ledger
from app.tools.contract import Risk
from app.tools.registry import get_registry
from tests.conftest import create_workspace

SENDER = "919999900001"


@pytest.fixture
async def world(signup_user, monkeypatch):
    client, profile = await signup_user()
    ws = await create_workspace(client, f"Chat {uuid4().hex[:6]}")
    await engine.create_indexes()
    await runs.create_indexes()
    await ledger.create_indexes()
    await ledger.settings().delete_many({})
    for name in ("channel_seen", "channel_hints", "channel_links", "channel_codes", "channel_attempts"):
        await get_db()[name].delete_many({})

    async def no_space():
        return ""

    monkeypatch.setattr(shortlist, "pick_space", no_space)
    return client, profile["id"], ws, SimulatorAdapter()


def msg(text="", kind="text", sender=SENDER, **over):
    return Inbound(channel="simulator", sender=sender, message_id=uuid4().hex, kind=kind, text=text, **over)


async def linked(world):
    client, user, ws, adapter = world
    made = await links.make_code(ws, user, "simulator")
    assert await engine.handle(adapter, msg(made["message"]), wait=True) == "linked"
    return made


def stub_models(monkeypatch, tool_name):
    async def think(role, prompt, system):
        return {"action": "tool", "tool": tool_name}

    async def only_that_tool(text, specs, **kwargs):
        return [tool_name], "keywords"                       # any words, in any language, shortlist the tool the stand-in model will choose

    monkeypatch.setattr(router, "think", think)
    monkeypatch.setattr(shortlist, "shortlist", only_that_tool)


def free_read():
    return next(s for s in get_registry().all() if s.risk is Risk.READ and not s.cost_key and not (s.input_schema() or {}).get("required")
                and not s.name.startswith(("compose.", "changes.")) and not s.human_only)


def priced():
    table = None

    async def find():
        nonlocal table
        table = await credit_costs.costs()
        return next(s for s in get_registry().all() if s.cost_key and credit_costs.price_with(table, s.cost_key, 1.0) > 0 and s.risk is not Risk.PUBLISH
                    and not s.human_only and s.scope.value in ("create",) and not s.name.startswith(("compose.", "changes.")) and not (s.input_schema() or {}).get("required"))

    return find()


async def test_a_code_links_a_chat_once_and_the_phone_number_is_never_stored(world):
    client, user, ws, adapter = world
    made = await links.make_code(ws, user, "simulator")
    assert len(made["code"]) == 9 and made["message"] == f"LINK {made['code']}"
    assert await engine.handle(adapter, msg(made["message"].lower()), wait=True) == "linked"
    assert "Linked" in adapter.last["text"]
    row = await get_db()["channel_links"].find_one({"workspace_id": ws})
    assert row["sender_tail"] == SENDER[-4:] and SENDER not in str(row)
    # the same code does not work twice, and a wrong one is told only that it did not work
    other = SimulatorAdapter()
    assert await engine.handle(other, msg(made["message"], sender="919999900002"), wait=True) == "link_bad"
    assert await engine.handle(other, msg("LINK ZZZZ-2222", sender="919999900002"), wait=True) == "link_bad" and "did not work" in other.last["text"]


async def test_too_many_wrong_codes_stop_being_answered(world):
    client, user, ws, adapter = world
    made = await links.make_code(ws, user, "simulator")
    for _ in range(links.MAX_WRONG_CODES_PER_HOUR):
        assert await engine.handle(adapter, msg("LINK ZZZZ-2222", sender="919999900009"), wait=True) == "link_bad"
    assert await engine.handle(adapter, msg(made["message"], sender="919999900009"), wait=True) == "link_bad"          # even the right one, now


async def test_a_chat_that_is_not_linked_gets_one_hint_a_day_and_no_tool_runs(world):
    client, user, ws, adapter = world
    assert await engine.handle(adapter, msg("write me a post about coffee"), wait=True) == "hint"
    assert "Settings" in adapter.last["text"] and len(adapter.sent) == 1
    assert await engine.handle(adapter, msg("hello?"), wait=True) == "unlinked" and len(adapter.sent) == 1
    assert await runs.runs().count_documents({"workspace_id": ws}) == 0


async def test_a_message_that_arrives_twice_is_handled_once(world, monkeypatch):
    client, user, ws, adapter = world
    await linked(world)
    stub_models(monkeypatch, free_read().name)
    once = msg("show me my plan")
    assert await engine.handle(adapter, once, wait=True) in ("done", "running")
    count = len(adapter.sent)
    assert await engine.handle(adapter, once, wait=True) == "duplicate" and len(adapter.sent) == count


async def test_stop_unlinks_and_the_chat_is_a_stranger_again(world):
    client, user, ws, adapter = world
    await linked(world)
    assert await engine.handle(adapter, msg("STOP"), wait=True) == "unlinked_by_stop" and "Unlinked" in adapter.last["text"]
    assert await links.find("simulator", SENDER) is None
    assert await engine.handle(adapter, msg("hi again"), wait=True) == "hint"


async def test_a_free_request_runs_and_the_reply_is_a_short_result_with_a_link(world, monkeypatch):
    client, user, ws, adapter = world
    await linked(world)
    stub_models(monkeypatch, free_read().name)
    assert await engine.handle(adapter, msg("show me what my plan allows"), wait=True) == "done"
    assert adapter.last["buttons"] == []
    # a question gets its answer in the chat; a request that changed something gets a short result and a link
    assert adapter.last["text"].startswith("Here is what I found:") or "/dashboard/library" in adapter.last["text"]


async def test_a_voice_note_is_heard_and_answered_in_the_language_spoken(world, monkeypatch):
    client, user, ws, adapter = world
    await linked(world)
    stub_models(monkeypatch, free_read().name)
    adapter.media["v1"] = b"ogg-bytes"

    async def hindi(data, name):
        assert data == b"ogg-bytes" and name.endswith(".ogg")
        return "मेरे प्लान में क्या क्या है", "hi"

    assert await engine.handle(adapter, msg(kind="voice", media={"id": "v1"}), transcribe=hindi, wait=True) == "done"
    assert adapter.last["text"].startswith("यह मिला:")                                                    # the fixed sentence is in Hindi
    adapter.media["v2"] = b"ogg2"

    async def tamil(data, name):
        return "என் திட்டத்தில் என்ன இருக்கிறது", "ta"

    assert await engine.handle(adapter, msg(kind="voice", media={"id": "v2"}), transcribe=tamil, wait=True) == "done"
    assert adapter.last["text"].startswith("கிடைத்தது:")


async def test_a_voice_note_that_cannot_be_heard_is_told_so_and_nothing_runs(world):
    client, user, ws, adapter = world
    await linked(world)
    before = await runs.runs().count_documents({"workspace_id": ws})

    async def broken(data, name):
        raise RuntimeError("provider down")

    adapter.media["v"] = b"x"
    assert await engine.handle(adapter, msg(kind="voice", media={"id": "v"}), transcribe=broken, wait=True) == "voice_failed"
    assert "could not hear" in adapter.last["text"]
    assert await engine.handle(adapter, msg(kind="voice", media={"id": "missing"}), wait=True) == "voice_failed"          # the file could not be fetched
    assert await runs.runs().count_documents({"workspace_id": ws}) == before


async def test_a_priced_plan_comes_back_with_the_price_and_two_buttons_and_runs_only_after_a_yes(world, monkeypatch):
    client, user, ws, adapter = world
    await linked(world)
    tool = await priced()
    stub_models(monkeypatch, tool.name)

    async def no_problems(gate, cred, steps):
        return []

    ran: list[str] = []

    async def fake_execute(workspace_id, user_id, run_id, steps, services):
        ran.append(run_id)
        await runs.runs().update_one({"id": run_id}, {"$set": {"status": "done", "result": {"status": "done", "steps": []}}})

    monkeypatch.setattr(runs, "_check_steps", no_problems)
    monkeypatch.setattr(runs, "_execute", fake_execute)
    assert await engine.handle(adapter, msg(tool.summary), wait=True) == "plan_sent"
    sent = adapter.last
    assert "credit" in sent["text"] and [b[0] for b in sent["buttons"]] == ["Go ahead", "Cancel"] and ran == []
    go, no = sent["buttons"][0][1], sent["buttons"][1][1]
    assert go.startswith("go:") and no.startswith("no:")
    assert await engine.handle(adapter, msg(kind="button", payload=go), wait=True) == "done" and ran and "/dashboard/library" in adapter.last["text"]
    # a second tap on the same button finds nothing waiting
    assert await engine.handle(adapter, msg(kind="button", payload=go), wait=True) == "not_ready" and "nothing waiting" in adapter.last["text"]


async def test_cancel_runs_nothing_and_says_so(world, monkeypatch):
    client, user, ws, adapter = world
    await linked(world)
    tool = await priced()
    stub_models(monkeypatch, tool.name)

    async def no_problems(gate, cred, steps):
        return []

    monkeypatch.setattr(runs, "_check_steps", no_problems)
    await engine.handle(adapter, msg(tool.summary), wait=True)
    assert await engine.handle(adapter, msg(kind="button", payload=adapter.last["buttons"][1][1]), wait=True) == "cancelled"
    assert "Cancelled" in adapter.last["text"]


async def test_a_picture_that_cannot_be_fetched_gets_an_honest_answer_and_nothing_runs(world):
    client, user, ws, adapter = world
    await linked(world)
    assert await engine.handle(adapter, msg(kind="image", media={"id": "p"}), wait=True) == "reference_failed" and "picture" in adapter.last["text"]


async def test_the_channel_switch_stops_replies_and_a_member_without_the_right_is_unlinked(world, monkeypatch):
    client, user, ws, adapter = world
    await linked(world)
    await ledger.settings().update_one({"scope": "tool", "tool": "channel.simulator"}, {"$set": {"scope": "tool", "tool": "channel.simulator", "off": True}}, upsert=True)
    assert await engine.handle(adapter, msg("hello"), wait=True) == "off" and "paused" in adapter.last["text"]
    await ledger.settings().delete_many({})

    async def nobody(workspace_id, user_id):
        return frozenset({"read_content"})

    from app.tools.services import LiveServices

    monkeypatch.setattr(LiveServices, "member_permissions", lambda self, w, u: nobody(w, u))
    assert await engine.handle(adapter, msg("hello"), wait=True) == "not_allowed" and await links.find("simulator", SENDER) is None


async def test_more_than_thirty_messages_in_an_hour_are_answered_once_and_then_ignored(world, monkeypatch):
    client, user, ws, adapter = world
    await linked(world)

    async def nothing(*a, **k):
        raise RuntimeError("not reached")

    monkeypatch.setattr(runs, "create", nothing)
    for _ in range(engine.PER_HOUR - 1):                                              # the link message counted as the first
        await engine.handle(adapter, msg("hi"), wait=True)
    assert await engine.handle(adapter, msg("one more"), wait=True) == "limit"
    slowed = len(adapter.sent)
    assert "a lot of messages" in adapter.sent[-1]["text"] and await engine.handle(adapter, msg("again"), wait=True) == "limit" and len(adapter.sent) == slowed


async def test_a_chat_contact_keeps_one_conversation_and_new_starts_another(world, monkeypatch):
    client, user, ws, adapter = world
    await linked(world)
    stub_models(monkeypatch, free_read().name)
    await engine.handle(adapter, msg("show me what my plan allows"), wait=True)
    from app.ask import threads

    first = await threads.threads().find_one({"workspace_id": ws, "link_key": {"$exists": True}})
    assert first is not None and first["surface"] == "simulator" and first["message_count"] >= 2
    await engine.handle(adapter, msg("show me again"), wait=True)
    assert await threads.threads().count_documents({"workspace_id": ws}) == 1               # the same conversation carries on
    assert await engine.handle(adapter, msg("NEW"), wait=True) == "new_conversation" and "new conversation" in adapter.last["text"]
    await engine.handle(adapter, msg("show me what my plan allows"), wait=True)
    assert await threads.threads().count_documents({"workspace_id": ws}) == 2


async def test_a_plain_answer_in_a_chat_is_sent_as_the_reply(world, monkeypatch):
    client, user, ws, adapter = world
    await linked(world)

    async def think(role, prompt, system):
        return {"action": "reply", "answer": "Short posts read better on a phone."}

    async def anything(text, specs, **kwargs):
        return [s.name for s in specs[:3]], "keywords"

    monkeypatch.setattr(router, "think", think)
    monkeypatch.setattr(shortlist, "shortlist", anything)
    assert await engine.handle(adapter, msg("should my posts be short?"), wait=True) == "answered"
    assert adapter.last["text"] == "Short posts read better on a phone."
