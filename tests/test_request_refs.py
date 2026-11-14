"""Pictures sent with one request (L26): only the look is kept (no model, no cost, 24 hours, never the picture), it is the person's own, it is added to each picture
prompt made while that request runs and to nothing else, and it works from the app and from a chat. No provider is called."""
from io import BytesIO
from uuid import uuid4

import pytest
from PIL import Image

from app.ask import router, runs, shortlist, threads
from app.billing import costs as credit_costs
from app.channels import engine, link as links
from app.channels.base import Inbound
from app.channels.simulator import SimulatorAdapter
from app.db.mongo import get_db
from app.pipelines.media import reference_style, request_refs
from app.tools.contract import Risk
from app.tools.registry import get_registry
from tests.conftest import create_workspace


def png(colour=(200, 40, 40), size=(64, 64)) -> bytes:
    out = BytesIO()
    Image.new("RGB", size, colour).save(out, "PNG")
    return out.getvalue()


@pytest.fixture
async def world(signup_user, monkeypatch):
    client, profile = await signup_user()
    ws = await create_workspace(client, f"Refs {uuid4().hex[:6]}")
    await request_refs.create_indexes()
    await runs.create_indexes()
    await threads.create_indexes()
    await engine.create_indexes()
    for name in ("request_references", "ask_threads", "ask_messages", "channel_seen", "channel_hints", "channel_links", "channel_codes", "channel_attempts"):
        await get_db()[name].delete_many({})

    async def no_space():
        return ""

    monkeypatch.setattr(shortlist, "pick_space", no_space)
    return client, profile["id"], ws, {"X-Workspace-Id": ws}


async def test_only_the_look_is_kept_and_it_says_what_the_look_is(world):
    client, user, ws, h = world
    made = await request_refs.add(ws, user, png())
    assert made["id"] and made["traits"] and made["colours"]
    row = await request_refs.col().find_one({"id": made["id"]})
    assert set(row) >= {"read", "created_at"} and not any(isinstance(v, (bytes, bytearray)) for v in row.values())          # the picture itself is not kept
    assert row["workspace_id"] == ws and row["user_id"] == user


async def test_an_empty_file_a_non_picture_a_huge_file_and_too_many_a_day_are_each_refused_plainly(world, monkeypatch):
    client, user, ws, h = world
    for data, code in ((b"", "input_invalid"), (b"not a picture at all", "unreadable")):
        with pytest.raises(request_refs.RefError) as err:
            await request_refs.add(ws, user, data)
        assert err.value.code == code
    monkeypatch.setattr(request_refs, "MAX_BYTES", 10)
    with pytest.raises(request_refs.RefError) as big:
        await request_refs.add(ws, user, png())
    assert big.value.code == "too_large" and big.value.status == 413
    monkeypatch.setattr(request_refs, "MAX_BYTES", 8 * 1024 * 1024)
    for _ in range(request_refs.PER_DAY):
        await request_refs.add(ws, user, png())
    with pytest.raises(request_refs.RefError) as many:
        await request_refs.add(ws, user, png())
    assert many.value.code == "limit" and many.value.status == 429


async def test_the_look_is_the_persons_own_and_names_the_pictures_they_sent(world):
    client, user, ws, h = world
    a = await request_refs.add(ws, user, png((250, 240, 200)))
    line = await request_refs.line_for(ws, user, [a["id"]])
    assert "pictures the person sent for this request" in line and "brand's own" not in line
    assert await request_refs.line_for(ws, "someone-else", [a["id"]]) == "" and await request_refs.line_for("other-workspace", user, [a["id"]]) == ""
    assert await request_refs.line_for(ws, user, []) == "" and await request_refs.line_for(ws, user, ["gone"]) == ""
    assert request_refs.check_ids(["a", "a", "b", "c", "d"]) == ["a", "b", "c"]


async def test_the_look_is_added_to_a_picture_prompt_only_while_it_is_set(world):
    client, user, ws, h = world
    assert await reference_style.with_style_line("A cafe at dawn", {}, ws) == "A cafe at dawn"
    token = request_refs.CURRENT.set("Follow the look of the pictures the person sent for this request: warm light.")
    try:
        said = await reference_style.with_style_line("A cafe at dawn", {}, ws)
        assert said.startswith("A cafe at dawn") and said.endswith("warm light.")
        assert await reference_style.with_style_line(said, {}, ws) == said                  # never added twice
    finally:
        request_refs.CURRENT.reset(token)
    assert await reference_style.with_style_line("A cafe at dawn", {}, ws) == "A cafe at dawn"


async def test_the_route_takes_a_picture_and_refuses_what_is_not_one(world):
    client, user, ws, h = world
    ok = await client.post("/api/v1/ask/references", headers=h, files={"file": ("p.png", png(), "image/png")})
    assert ok.status_code == 200 and ok.json()["id"] and ok.json()["traits"]
    bad = await client.post("/api/v1/ask/references", headers=h, files={"file": ("p.txt", b"hello", "text/plain")})
    assert bad.status_code == 422 and "picture" in bad.json()["detail"]


async def test_a_request_carries_its_pictures_and_they_guide_only_that_request_while_it_runs(world, monkeypatch):
    client, user, ws, h = world
    table = await credit_costs.costs()
    priced = next(s for s in get_registry().all() if s.cost_key and credit_costs.price_with(table, s.cost_key, 1.0) > 0 and s.risk is not Risk.PUBLISH and not s.human_only
                  and s.scope.value in ("create",) and not s.name.startswith(("compose.", "changes.")) and not (s.input_schema() or {}).get("required"))

    async def think(role, prompt, system):
        return {"action": "tool", "tool": priced.name}

    async def no_problems(gate, cred, steps):
        return []

    seen: list[str] = []

    async def fake_steps(workspace_id, user_id, run_id, steps, services):
        seen.append(request_refs.active_line())                                          # what a picture made now would be told
        await runs.runs().update_one({"id": run_id}, {"$set": {"status": "done", "result": {"status": "done", "steps": []}}})

    monkeypatch.setattr(router, "think", think)
    monkeypatch.setattr(runs, "_check_steps", no_problems)
    monkeypatch.setattr(runs, "_execute_steps", fake_steps)
    ref = await request_refs.add(ws, user, png((20, 20, 30)))
    run = await runs.create(ws, user, priced.summary, reference_ids=[ref["id"], "x", "x"])
    assert (await runs.runs().find_one({"id": run["id"]}))["reference_ids"] == [ref["id"], "x"]
    await runs.confirm(ws, user, run["id"], wait=True)
    plain = await runs.create(ws, user, priced.summary)
    await runs.confirm(ws, user, plain["id"], wait=True)
    assert "pictures the person sent" in seen[0] and "dark tones" in seen[0] and seen[1] == ""            # the next request does not inherit the look
    assert request_refs.active_line() == ""                                                              # and nothing is left set afterwards


# ── from a chat app ──────────────────────────────────────────────────────────

async def test_a_picture_sent_in_a_chat_is_kept_as_a_look_and_guides_the_next_request_once(world, monkeypatch):
    client, user, ws, h = world
    adapter = SimulatorAdapter(media={"img1": png((10, 10, 10))})
    made = await links.make_code(ws, user, "simulator")
    sender = "919990000111"
    await engine.handle(adapter, Inbound(channel="simulator", sender=sender, message_id="a", text=made["message"]), wait=True)
    assert await engine.handle(adapter, Inbound(channel="simulator", sender=sender, message_id="b", kind="image", media={"id": "img1"}), wait=True) == "reference_saved"
    assert "follow its look" in adapter.last["text"]
    asked: list[list[str]] = []

    async def fake_create(workspace_id, user_id, text, trust="ask", **kw):
        asked.append(kw.get("reference_ids"))
        return {"id": uuid4().hex, "status": "answered", "reply": "ok", "language": "en"}

    monkeypatch.setattr(runs, "create", fake_create)
    await engine.handle(adapter, Inbound(channel="simulator", sender=sender, message_id="c", text="make three posts about our launch"), wait=True)
    await engine.handle(adapter, Inbound(channel="simulator", sender=sender, message_id="d", text="and one more"), wait=True)
    assert len(asked[0]) == 1 and asked[1] == []                                          # the picture belongs to the next request only


async def test_a_chat_picture_that_cannot_be_read_is_told_plainly_and_nothing_is_kept(world):
    client, user, ws, h = world
    adapter = SimulatorAdapter(media={"junk": b"this is not an image"})
    made = await links.make_code(ws, user, "simulator")
    await engine.handle(adapter, Inbound(channel="simulator", sender="919990000222", message_id="a", text=made["message"]), wait=True)
    assert await engine.handle(adapter, Inbound(channel="simulator", sender="919990000222", message_id="b", kind="image", media={"id": "junk"}), wait=True) == "reference_refused"
    assert "JPG or PNG" in adapter.last["text"]
    assert await engine.handle(adapter, Inbound(channel="simulator", sender="919990000222", message_id="c", kind="image", media={"id": "missing"}), wait=True) == "reference_failed"
    assert await request_refs.col().count_documents({"workspace_id": ws}) == 0


# ── a background job sees the look too ───────────────────────────────────────

async def test_a_background_job_started_during_a_request_makes_its_pictures_with_the_look_and_a_plain_one_does_not(world, test_actions=None):
    from pydantic import BaseModel

    from app.shared import jobs
    from tests.test_pipeline_runs import _wait_for

    client, user, ws, h = world
    seen: list[str] = []

    class Nothing(BaseModel):
        note: str = ""

    async def run(ctx, payload, reporter):
        seen.append(request_refs.active_line())
        return {"ok": True}

    jobs.register(jobs.JobAction(name="test.look", permission="edit_content", payload_model=Nothing, run=run, title=lambda p: "Test look", kind="text"))
    try:
        token = request_refs.CURRENT.set("Follow the look of the pictures the person sent for this request: warm light.")
        try:
            first = await client.post("/api/v1/jobs", json={"action": "test.look", "payload": {"note": "a"}}, headers=h)
        finally:
            request_refs.CURRENT.reset(token)
        assert first.status_code == 202, first.text
        done = await _wait_for(client, first.json()["id"], ("done", "failed"), headers=h)
        assert done["status"] == "done" and "style_line" not in str(done)                        # kept with the job, never shown
        second = await client.post("/api/v1/jobs", json={"action": "test.look", "payload": {"note": "b"}}, headers=h)
        await _wait_for(client, second.json()["id"], ("done", "failed"), headers=h)
    finally:
        jobs.ACTIONS.pop("test.look", None)
    assert seen == ["Follow the look of the pictures the person sent for this request: warm light.", ""]
