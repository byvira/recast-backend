"""One scenario through every way in (the app, the signed-in command line and the chat apps): the outcomes must match. A priced request waits for a yes on every
surface and nothing runs before it; a request that would publish is shown to the person and never run on any surface. No model is called."""
import pytest

from app.ask import router, runs, shortlist, threads
from app.billing import costs as credit_costs
from app.channels import engine, link as links
from app.channels.base import Inbound
from app.channels.simulator import SimulatorAdapter
from app.db.mongo import get_db
from app.tools.contract import Risk
from app.tools.registry import get_registry
from tests.test_public_ask import P, _auth, token, world  # noqa: F401  (world is a fixture)


@pytest.fixture
async def surfaces(world, monkeypatch):  # noqa: F811
    client, ws, h = world
    await runs.create_indexes()
    await threads.create_indexes()
    await engine.create_indexes()
    for name in ("ask_threads", "ask_messages", "channel_seen", "channel_hints", "channel_links", "channel_codes", "channel_attempts"):
        await get_db()[name].delete_many({})

    async def no_space():
        return ""

    async def no_problems(gate, cred, steps):
        return []

    monkeypatch.setattr(shortlist, "pick_space", no_space)
    monkeypatch.setattr(runs, "_check_steps", no_problems)
    return client, ws, h


def _priced():
    async def find():
        table = await credit_costs.costs()
        return next(s for s in get_registry().all() if s.cost_key and credit_costs.price_with(table, s.cost_key, 1.0) > 0 and s.risk is not Risk.PUBLISH and not s.human_only
                    and s.scope.value in ("create",) and not s.name.startswith(("compose.", "changes.")) and not (s.input_schema() or {}).get("required"))

    return find()


def _publishing():
    return next(s for s in get_registry().all() if s.risk is Risk.PUBLISH)


def decide(monkeypatch, tool_name):
    async def think(role, prompt, system):
        return {"action": "tool", "tool": tool_name}

    async def only(text, specs, **kwargs):
        return [tool_name], "keywords"

    monkeypatch.setattr(router, "think", think)
    monkeypatch.setattr(shortlist, "shortlist", only)


async def via_app(client, ws, h, text):
    res = await client.post("/api/v1/ask", headers=h, json={"message": text})
    assert res.status_code == 201, res.text
    return res.json()


async def via_cli(client, ws, h, text):
    tok = await token(client, h, ws)
    res = await client.post(f"{P}/ask", headers=_auth(tok), json={"message": text})
    assert res.status_code == 201, res.text
    return res.json()["run"]


async def via_chat(client, ws, h, text):
    seed = await via_app(client, ws, h, "link setup")                                       # a request made in the app tells us who the member is
    user = (await runs.runs().find_one({"id": seed["id"]}))["user_id"]
    adapter = SimulatorAdapter()
    made = await links.make_code(ws, user, "simulator")
    await engine.handle(adapter, Inbound(channel="simulator", sender="919990000001", message_id="m-link", text=made["message"]), wait=True)
    await engine.handle(adapter, Inbound(channel="simulator", sender="919990000001", message_id="m-ask", text=text), wait=True)
    row = await runs.runs().find_one({"workspace_id": ws, "message": text})
    return await runs.get(ws, user, row["id"]) if row else {}


SURFACES = {"the app": via_app, "the command line": via_cli, "a chat app": via_chat}


@pytest.mark.parametrize("name", list(SURFACES))
async def test_a_priced_request_waits_for_a_yes_on_every_surface_and_nothing_runs_before_it(surfaces, monkeypatch, name):
    client, ws, h = surfaces
    tool = await _priced()
    decide(monkeypatch, tool.name)
    run = await SURFACES[name](client, ws, h, tool.summary)
    assert run["status"] == "ready" and run["plan"]["credits"] > 0 and "I can do this" in run["reply"]
    stored = await runs.runs().find_one({"id": run["id"]})
    assert stored.get("compose_run_id") is None and stored["status"] == "ready"           # nothing was done or charged before the yes


@pytest.mark.parametrize("name", list(SURFACES))
async def test_a_request_that_would_publish_is_shown_to_the_person_and_never_run_on_any_surface(surfaces, monkeypatch, name):
    client, ws, h = surfaces
    spec = _publishing()
    decide(monkeypatch, spec.name)
    monkeypatch.setattr(runs, "visible_tools", lambda registry, cred, perms: [spec])             # as in the existing publish test: Ask is offered it only so it can hand it off
    run = await SURFACES[name](client, ws, h, "post my launch announcement right now")
    assert run["status"] == "hand_off" and run["plan"]["hand_off"]["tool"] == spec.name
    stored = await runs.runs().find_one({"id": run["id"]})
    assert stored.get("compose_run_id") is None and stored.get("result") is None
