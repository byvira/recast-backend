"""The parity gate for Ask Ekavo: the same tool gives the same answer whether it is called through the public API with a key or by Ask for a signed-in
member, both go through the same gatekeeper and are recorded in the same ledger (tagged by who made them), a person without the right role is refused
the same way, and nothing Ask does can publish. The model is a stub; no provider is called."""
import asyncio

import pytest

from app.ask import router, runs, shortlist
from app.tools import ledger
from app.tools.contract import Risk
from app.tools.registry import get_registry
from tests.test_tools_public_api import P, _auth, _key, world  # noqa: F401  (world is a fixture)

#: free reads whose answer does not depend on which door was used (plan.limits is left out on purpose: Ask's own planning step is part of the usage it reports)
SAMPLE = ["credits.prices", "prompts.tones", "image.layouts", "brand.identity_fields", "stock.status"]


@pytest.fixture(autouse=True)
def _no_embeddings(monkeypatch):
    async def none():
        return ""

    monkeypatch.setattr(shortlist, "pick_space", none)


def _decide(tool):
    async def think(role, prompt, system):
        return {"action": "tool", "tool": tool}

    return think


async def _ask(client, h, tool, monkeypatch):
    monkeypatch.setattr(router, "think", _decide(tool))
    spec = get_registry().get(tool)
    res = await client.post("/api/v1/ask", headers=h, json={"message": spec.summary})
    assert res.status_code == 201, res.text
    body = res.json()
    for _ in range(30):
        if body["status"] != "running":
            break
        await asyncio.sleep(0.3)
        body = (await client.get(f"/api/v1/ask/{body['id']}", headers=h)).json()
    return body


@pytest.mark.parametrize("tool", SAMPLE)
async def test_a_read_gives_the_same_answer_through_a_key_and_through_ask(world, monkeypatch, tool):
    client, ws, h = world
    secret = await _key(client, h)
    via_key = await client.post(f"{P}/tools/{tool}/call", headers=_auth(secret), json={"arguments": {}})
    assert via_key.status_code == 200, via_key.text
    ask = await _ask(client, h, tool, monkeypatch)
    assert ask["status"] == "done", ask
    step = ask["result"]["steps"][0]
    assert step["state"] == "done" and step["result"]["data"] == via_key.json()["data"], tool


async def test_both_doors_are_recorded_in_the_same_ledger_and_tagged_by_who_made_the_call(world, monkeypatch):
    client, ws, h = world
    secret = await _key(client, h)
    await client.post(f"{P}/tools/plan.limits/call", headers=_auth(secret), json={"arguments": {}})
    await _ask(client, h, "plan.limits", monkeypatch)
    rows = await ledger.calls().find({"workspace_id": ws, "tool": "plan.limits", "outcome": {"$in": ["done", "admitted"]}}).to_list(length=10)
    surfaces = {r.get("surface") for r in rows}
    assert {"api", "ask"} <= surfaces, surfaces


async def test_nothing_ask_can_choose_is_a_tool_that_publishes_decides_deletes_or_administers(world):
    client, ws, h = world
    from app.tools.gatekeeper import visible_tools
    from app.tools.services import LiveServices

    services = LiveServices()
    cred = await services.credential(runs.credential_id(ws, "any-user"))
    assert cred.surface == "ask" and {s.value for s in cred.scopes} == {"read", "create"}
    perms = frozenset({"create_content", "edit_content", "publish_content", "approve_content", "manage_workspace", "manage_members"})
    offered = visible_tools(get_registry(), cred, perms)
    # publish-class tools can be shown to the person as a hand-off, but the runner (the compound runner Ask uses) refuses to run them
    from app.tools import compose

    for spec in offered:
        if spec.risk in {Risk.PUBLISH, Risk.DECISION, Risk.DESTRUCTIVE, Risk.ADMIN}:
            with pytest.raises(Exception):
                compose.validate([compose.Step(label="x", tool=spec.name, arguments={})])


async def test_a_viewer_role_is_refused_the_same_way_through_ask_as_through_a_key(world, monkeypatch):
    """Ask acts as the person with their role: a tool the role cannot use is not on its list, so it can never be chosen."""
    client, ws, h = world
    create_tool = next(s for s in get_registry().all() if s.scope.value == "create" and s.permission and not s.human_only and s.risk is Risk.WRITE)
    services = runs.LiveServices()

    async def no_rights(workspace_id, user_id):
        return frozenset()           # an active member whose role holds no rights at all

    monkeypatch.setattr(services, "member_permissions", no_rights)
    monkeypatch.setattr(router, "think", _decide(create_tool.name))
    made = await runs.create(ws, "someone", create_tool.summary, services=services)
    assert made["status"] == "none" and "reply" in made                         # the tool was never offered, so nothing was planned
    assert await ledger.calls().count_documents({"workspace_id": ws, "tool": create_tool.name, "outcome": {"$in": ["done", "admitted"]}}) == 0
