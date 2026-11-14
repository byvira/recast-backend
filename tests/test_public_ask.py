"""Ask Ekavo from a signed-in command line: only a person's own sign-in may use it (never a service key, never one that cannot create), a conversation carries on
with its id, one person never reaches another's request, and the command line's `ask` and `chat` agree to a price only after a yes. No model is called."""
import io
import sys
from pathlib import Path

import pytest

from app.ask import router, runs, shortlist, threads
from app.db.mongo import get_db
from tests.test_tools_public_api import P, _auth, _key, world  # noqa: F401  (world is a fixture)
from tests.test_tools_sessions_mcp import _signed_in

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cli"))

from ekavo_cli import config  # noqa: E402
from ekavo_cli.client import Response  # noqa: E402
from ekavo_cli.main import FAILED, NEEDS_YES, NOT_SIGNED_IN, OK, main  # noqa: E402


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


def answer_with(monkeypatch, reply="Short posts read better on a phone."):
    async def think(role, prompt, system):
        return {"action": "reply", "answer": reply}

    monkeypatch.setattr(router, "think", think)


async def token(client, h, ws, scopes=("read", "create"), kind="cli"):
    _, got = await _signed_in(client, h, scopes=scopes, client_kind=kind, ws=ws)
    return got.json()["access_token"]


async def test_a_signed_in_command_line_can_ask_and_carry_on_in_the_same_conversation(ready, monkeypatch):
    client, ws, h = ready
    answer_with(monkeypatch)
    tok = await token(client, h, ws)
    first = await client.post(f"{P}/ask", headers=_auth(tok), json={"message": "Should my posts be short?"})
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["run"]["status"] == "answered" and body["run"]["reply"].startswith("Short posts") and body["thread_id"]
    second = await client.post(f"{P}/ask", headers=_auth(tok), json={"message": "and on weekends?", "thread_id": body["thread_id"]})
    assert second.status_code == 201 and second.json()["thread_id"] == body["thread_id"]
    saved = await threads.page(body["thread_id"])
    assert [m["author"]["type"] for m in saved] == ["member", "ekavo", "member", "ekavo"] and {m["surface"] for m in saved} == {"cli"}
    read = await client.get(f"{P}/ask/{body['run']['id']}", headers=_auth(tok))
    assert read.status_code == 200 and read.json()["run"]["id"] == body["run"]["id"]


async def test_a_service_key_a_read_only_sign_in_and_no_sign_in_are_each_refused(ready, monkeypatch):
    client, ws, h = ready
    answer_with(monkeypatch)
    key = await _key(client, h, scopes=("read", "create"))
    refused = await client.post(f"{P}/ask", headers=_auth(key), json={"message": "hi"})
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "not_allowed"          # a key would be a way round its own credit cap
    read_only = await token(client, h, ws, scopes=("read",))
    cannot = await client.post(f"{P}/ask", headers=_auth(read_only), json={"message": "hi"})
    assert cannot.status_code == 403 and cannot.json()["error"]["code"] == "scope_missing"
    assert (await client.post(f"{P}/ask", json={"message": "hi"})).status_code == 401
    assert (await client.post(f"{P}/ask", headers=_auth(read_only), json={"message": "hi", "trust": "act"})).status_code in (403, 422)     # act is not offered here


async def test_another_persons_request_cannot_be_read_or_stopped(ready, monkeypatch, signup_user):
    client, ws, h = ready
    answer_with(monkeypatch)
    mine = await token(client, h, ws)
    made = (await client.post(f"{P}/ask", headers=_auth(mine), json={"message": "private"})).json()
    other, _ = await signup_user()
    from tests.conftest import create_workspace

    ws2 = await create_workspace(other, "Elsewhere")
    theirs = await token(other, {"X-Workspace-Id": ws2}, ws2)
    for call in (other.get(f"{P}/ask/{made['run']['id']}", headers=_auth(theirs)), other.post(f"{P}/ask/{made['run']['id']}/stop", headers=_auth(theirs))):
        assert (await call).status_code == 404


# ── the command line ─────────────────────────────────────────────────────────

class Server:
    def __init__(self, replies):
        self.replies, self.seen = list(replies), []

    def __call__(self, method, url, headers, body):
        self.seen.append((method, url.split("/api/public/v1")[-1], body))
        return self.replies.pop(0)


def cli(argv, server):
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, out=out, err=err, transport=server, sleep=lambda s: None)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture(autouse=False)
def signed(tmp_path, monkeypatch):
    monkeypatch.setenv("EKAVO_CONFIG_DIR", str(tmp_path))
    config.save(config.Settings(url="https://api.test", token="ek_live_x", workspace_id="ws1", scopes=("read", "create"), expires_at="2027-02-01T00:00:00Z"))


PLAN = {"id": "r1", "status": "ready", "trust": "ask", "reply": "I can do this in 1 step. It costs 4 credits.", "plan": {"steps": [{"label": "draft"}], "credits": 4, "exact": True}}


def test_an_answer_is_printed_and_the_conversation_id_is_offered_for_the_next_message(signed):
    server = Server([Response(201, {"ok": True, "run": {"id": "r1", "status": "answered", "reply": "Short posts read better."}, "thread_id": "t1"}, {})])
    code, out, err = cli(["ask", "should", "posts", "be", "short"], server)
    assert code == OK and "Short posts read better." in out and "--thread t1" in err
    assert server.seen[0][2] == {"message": "should posts be short"}


def test_a_price_is_not_agreed_to_without_a_yes_and_is_cancelled(signed):
    server = Server([Response(201, {"ok": True, "run": PLAN, "thread_id": "t1"}, {}), Response(200, {"ok": True, "run": {**PLAN, "status": "cancelled"}}, {})])
    code, out, err = cli(["ask", "make", "a", "post"], server)
    assert code == NEEDS_YES and "costs 4 credits" in out and "--yes" in err
    assert [s[1] for s in server.seen] == ["/ask", "/ask/r1/stop"]                       # nothing was confirmed


def test_with_yes_the_price_is_agreed_and_the_work_is_followed_to_its_end(signed):
    done = {"id": "r1", "status": "done", "result": {"steps": [{"label": "draft", "state": "done"}]}}
    server = Server([Response(201, {"ok": True, "run": PLAN, "thread_id": "t1"}, {}), Response(200, {"ok": True, "run": {**PLAN, "status": "running"}}, {}),
                     Response(200, {"ok": True, "run": done}, {})])
    code, out, err = cli(["ask", "--yes", "make", "a", "post"], server)
    assert code == OK and "[done] draft" in out
    assert [s[1] for s in server.seen] == ["/ask", "/ask/r1/confirm", "/ask/r1"]


def test_a_failed_request_exits_with_a_failure_and_not_signed_in_is_said_plainly(signed, tmp_path, monkeypatch):
    server = Server([Response(201, {"ok": True, "run": {"id": "r2", "status": "failed", "reply": "Something went wrong."}, "thread_id": "t2"}, {})])
    assert cli(["ask", "hello"], server)[0] == FAILED
    monkeypatch.setenv("EKAVO_CONFIG_DIR", str(tmp_path / "nobody"))
    assert cli(["ask", "hello"], Server([]))[0] == NOT_SIGNED_IN


def test_chat_keeps_one_conversation_asks_before_a_price_and_leaves_on_exit(signed, monkeypatch):
    lines = iter(["hello", "make a post", "n", "exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(lines))
    server = Server([
        Response(201, {"ok": True, "run": {"id": "r1", "status": "answered", "reply": "Hi there."}, "thread_id": "t1"}, {}),
        Response(201, {"ok": True, "run": PLAN, "thread_id": "t1"}, {}),
        Response(200, {"ok": True, "run": {**PLAN, "status": "cancelled"}}, {}),
    ])
    code, out, err = cli(["chat"], server)
    assert code == OK and "Hi there." in out and "Cancelled." in err
    assert server.seen[0][2] == {"message": "hello"} and server.seen[1][2] == {"message": "make a post", "thread_id": "t1"}      # the second message carries the conversation
    assert [s[1] for s in server.seen] == ["/ask", "/ask", "/ask/r1/stop"]
