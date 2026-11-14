"""The live stream of an Ask request: the stages arrive in order, then the finished request; a problem is a plain event; following a run ends when it stops running.
Every model answer is a stand-in."""
import json
from uuid import uuid4

import pytest

from app.ask import router, runs, shortlist, threads
from app.db.mongo import get_db
from tests.conftest import create_workspace


@pytest.fixture
async def asker(signup_user, monkeypatch):
    client, profile = await signup_user()
    ws = await create_workspace(client, f"Live {uuid4().hex[:6]}")
    await runs.create_indexes()
    await threads.create_indexes()
    for name in ("ask_threads", "ask_messages"):
        await get_db()[name].delete_many({})

    async def no_space():
        return ""

    monkeypatch.setattr(shortlist, "pick_space", no_space)
    return client, profile["id"], ws, {"X-Workspace-Id": ws}


def events(text: str) -> list[tuple[str, dict]]:
    """The (event, data) pairs in a stream's text; keep-alive lines are skipped."""
    out = []
    for block in text.strip().split("\n\n"):
        lines = [line for line in block.split("\n") if line and not line.startswith(":")]
        if not lines:
            continue
        name = next(line[len("event: "):] for line in lines if line.startswith("event: "))
        data = next(line[len("data: "):] for line in lines if line.startswith("data: "))
        out.append((name, json.loads(data)))
    return out


async def test_the_stages_arrive_in_order_and_the_finished_request_comes_last(asker, monkeypatch):
    client, user, ws, h = asker

    async def think(role, prompt, system):
        return {"action": "reply", "answer": "Short posts read better on a phone."}

    monkeypatch.setattr(router, "think", think)
    res = await client.post("/api/v1/ask/stream", headers=h, json={"message": "Should my posts be short?"})
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/event-stream")
    got = events(res.text)
    names = [n for n, _ in got]
    assert names[0] == "open" and names[-1] == "run" and "phase" in names
    phases = [d["key"] for n, d in got if n == "phase"]
    assert phases[:2] == ["find", "decide"] and all(isinstance(d["label"], str) and d["label"] for n, d in got if n == "phase")
    run = got[-1][1]
    assert run["status"] == "answered" and run["reply"] == "Short posts read better on a phone." and run["thread_id"] == got[0][1]["thread_id"]


async def test_a_problem_is_a_plain_event_and_the_stream_still_ends(asker, monkeypatch):
    client, user, ws, h = asker
    res = await client.post("/api/v1/ask/stream", headers=h, json={"message": "hi", "thread_id": "no-such-thread"})
    got = events(res.text)
    assert res.status_code == 200 and got[-1][0] == "problem" and got[-1][1]["status"] == 404 and "not found" in got[-1][1]["message"].lower()


async def test_following_a_finished_run_sends_it_once_and_ends_and_an_unknown_run_is_a_problem(asker, monkeypatch):
    client, user, ws, h = asker

    async def think(role, prompt, system):
        return {"action": "none"}

    monkeypatch.setattr(router, "think", think)
    made = (await client.post("/api/v1/ask", headers=h, json={"message": "something no tool does"})).json()
    got = events((await client.get(f"/api/v1/ask/{made['id']}/stream", headers=h)).text)
    assert [n for n, _ in got] == ["run"] and got[0][1]["id"] == made["id"]
    missing = events((await client.get("/api/v1/ask/nope/stream", headers=h)).text)
    assert missing[-1][0] == "problem" and missing[-1][1]["status"] == 404


async def test_someone_elses_run_cannot_be_followed(asker, monkeypatch, signup_user):
    client, user, ws, h = asker

    async def think(role, prompt, system):
        return {"action": "none"}

    monkeypatch.setattr(router, "think", think)
    made = (await client.post("/api/v1/ask", headers=h, json={"message": "private request"})).json()
    stranger, _ = await signup_user()
    res = await stranger.get(f"/api/v1/ask/{made['id']}/stream", headers={"X-Workspace-Id": ws})
    assert res.status_code in (403, 404) or events(res.text)[-1][0] == "problem"
