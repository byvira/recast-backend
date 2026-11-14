"""Ask Ekavo as tools (L25): registered with the right shape, never offered to Ask's own planner, usable only from a person's own sign-in, a conversation carries on with its id,
and the same tools work from the public API (which the command line and assistants use). No model is called."""
import pytest

from app.ask import router, runs, shortlist, threads
from app.db.mongo import get_db
from app.tools.contract import Risk, Scope
from app.tools.registry import get_registry
from tests.test_public_ask import answer_with, token  # noqa: F401
from tests.test_tools_public_api import P, _auth, _key, world  # noqa: F401  (world is a fixture)


@pytest.fixture
async def ready(world, monkeypatch):  # noqa: F811
    client, ws, h = world
    await runs.create_indexes()
    await threads.create_indexes()
    await get_db()["ask_threads"].delete_many({})

    async def no_space():
        return ""

    monkeypatch.setattr(shortlist, "pick_space", no_space)
    return client, ws, h


def test_the_three_ask_tools_are_registered_with_the_right_risk_scope_and_undo_words():
    reg = get_registry()
    say, confirm, status = reg.get("ask.say"), reg.get("ask.confirm"), reg.get("ask.status")
    assert (say.risk, say.scope) == (Risk.WRITE, Scope.CREATE) and (confirm.risk, confirm.scope) == (Risk.WRITE, Scope.CREATE) and (status.risk, status.scope) == (Risk.READ, Scope.READ)
    assert "ask.confirm" in say.cost_note and "price" in confirm.cost_note.lower()
    assert "own undo" in say.catalog_entry()["undo"] and "own undo" in confirm.catalog_entry()["undo"]
    assert say.input_schema()["required"] == ["message"]


def test_ask_never_offers_its_own_tools_to_its_own_planner():
    assert all(name.startswith(runs._HIDDEN) for name in ("ask.say", "ask.confirm", "ask.status"))


async def test_a_signed_in_assistant_can_say_carry_on_and_read_the_status(ready, monkeypatch):
    client, ws, h = ready
    answer_with(monkeypatch)
    tok = await token(client, h, ws, kind="mcp")
    first = await client.post(f"{P}/tools/ask.say/call", headers=_auth(tok), json={"arguments": {"message": "Should my posts be short?"}})
    assert first.status_code == 200, first.text
    data = first.json()["data"]
    assert data["run"]["status"] == "answered" and data["thread_id"]
    second = await client.post(f"{P}/tools/ask.say/call", headers=_auth(tok), json={"arguments": {"message": "and on weekends?", "thread_id": data["thread_id"]}})
    assert second.json()["data"]["thread_id"] == data["thread_id"]
    saved = await threads.page(data["thread_id"])
    assert len(saved) == 4 and {m["surface"] for m in saved} == {"mcp"}
    seen = await client.post(f"{P}/tools/ask.status/call", headers=_auth(tok), json={"arguments": {"run_id": data["run"]["id"]}})
    assert seen.status_code == 200 and seen.json()["data"]["run"]["id"] == data["run"]["id"]


async def test_a_service_key_is_refused_and_a_missing_request_is_not_found(ready, monkeypatch):
    client, ws, h = ready
    answer_with(monkeypatch)
    key = await _key(client, h, scopes=("read", "create"))
    refused = await client.post(f"{P}/tools/ask.say/call", headers=_auth(key), json={"arguments": {"message": "hi"}})
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "not_allowed"
    tok = await token(client, h, ws)
    gone = await client.post(f"{P}/tools/ask.confirm/call", headers=_auth(tok), json={"arguments": {"run_id": "no-such-run"}})
    assert gone.status_code == 404
    assert (await client.post(f"{P}/tools/ask.say/call", headers=_auth(tok), json={"arguments": {"message": ""}})).status_code in (400, 422)


async def test_a_read_only_sign_in_cannot_say_anything(ready, monkeypatch):
    client, ws, h = ready
    answer_with(monkeypatch)
    tok = await token(client, h, ws, scopes=("read",))
    res = await client.post(f"{P}/tools/ask.say/call", headers=_auth(tok), json={"arguments": {"message": "hi"}})
    assert res.status_code == 403
