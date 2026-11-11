"""Failures and retries: one error shape for every surface, bounded and safe automatic retries, a cool-down when the writing service keeps
failing, and compound requests that keep what worked. Models and providers are replaced by stand-ins that fail on purpose; no quota is spent."""

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx
import pytest

from app.core import failures
from app.core.failures import AIUnavailable
from app.db.mongo import get_db
from app.tools import compose, executor, ledger
from app.tools.compose import Step
from app.tools.executor import ToolFailure
from app.tools.gatekeeper import Call, Credential, Gatekeeper, Refusal
from app.tools.contract import Scope
from tests.conftest import create_workspace

D = "/api/v1/developer"
P = "/api/public/v1"


# ── the one shape ────────────────────────────────────────────────────────

def test_a_busy_writing_service_is_said_in_plain_words_and_never_in_the_providers():
    for text in ("LLM service unavailable.", "LLM rate limit. Please try again in 60 seconds.", "", "Groq 429 token quota exceeded for model llama"):
        f = failures.from_upstream(503, text)
        assert f.code == "ai_busy" and f.retryable and f.retry_after_seconds == 60
        assert not any(w in f.message.lower() for w in ("groq", "llm", "token", "model", "429"))
    assert failures.from_upstream(503, "The queue is full").code == "service_unavailable"                      # a busy dependency that is not a model


def test_every_kind_of_failure_says_whether_trying_again_helps_and_what_to_do():
    cases = {402: ("credits_or_plan_limit", False), 404: ("not_found", False), 409: ("conflict", False), 422: ("input_invalid", False), 429: ("too_many_requests", True),
             500: ("internal_error", True), 502: ("service_unavailable", True), 503: ("service_unavailable", True), 504: ("timed_out", True), 403: ("not_allowed", False)}
    for status, (code, retryable) in cases.items():
        f = failures.from_upstream(status, "some words")
        assert (f.code, f.retryable) == (code, retryable), status
    for f in (failures.ai_busy(), failures.timed_out(), failures.internal_error()):
        assert f.next and f.retryable and f.retry_after_seconds
    assert failures.from_upstream(500, "Traceback (most recent call last): secret").message.find("Traceback") == -1
    assert failures.for_refusal("rate_limited")["retryable"] is True and failures.for_refusal("scope_missing")["retryable"] is False
    assert "confirmed_credits" in failures.for_refusal("confirm_required")["next"]


def test_only_a_call_that_changes_nothing_may_be_repeated_by_ekavo_itself():
    assert failures.safe_to_repeat("read") is True
    for risk in ("write", "publish", "destructive", "decision", "admin"):
        assert failures.safe_to_repeat(risk) is False


async def test_the_apps_shared_handler_gives_every_screen_and_tool_the_same_fields_and_keeps_detail(signup_user):
    from app.main import app

    async def boom():
        raise AIUnavailable()

    app.add_api_route("/api/v1/__test/busy", boom, methods=["GET"])
    try:
        client, _ = await signup_user()
        res = await client.get("/api/v1/__test/busy")
    finally:
        app.router.routes[:] = [r for r in app.router.routes if getattr(r, "path", "") != "/api/v1/__test/busy"]        # leave the app as it was
    body = res.json()
    assert res.status_code == 503 and body["detail"] == "LLM service unavailable."                    # what screens already read, unchanged
    assert body["code"] == "ai_busy" and body["retryable"] is True and body["retry_after_seconds"] == 60 and res.headers["retry-after"] == "60"
    missing = await client.get("/api/v1/content/pieces/does-not-exist", headers={"X-Workspace-Id": await create_workspace(client, f"E {uuid4().hex[:5]}")})
    assert missing.status_code == 404 and missing.json()["retryable"] is False and missing.json()["detail"]


# ── reads are retried a little, writes never ─────────────────────────────

def _resp(status: int, **body) -> httpx.Response:
    return httpx.Response(status, json=body)


async def test_a_read_that_gets_a_busy_answer_is_retried_and_a_write_is_not(monkeypatch):
    monkeypatch.setattr(executor, "RETRY_WAITS", (0.0, 0.0))
    calls: list[str] = []
    answers = [_resp(503, detail="LLM service unavailable.", code="ai_busy"), _resp(503, detail="x"), _resp(200, ok=True)]

    async def fake_send(credential, method, path, query, body):
        calls.append(method)
        return answers[min(len(calls) - 1, len(answers) - 1)]

    monkeypatch.setattr(executor, "_send", fake_send)
    cred = Credential("k", "api", "ws", "u", frozenset({Scope.READ}))
    assert await executor.request_route(cred, "GET", "/x", None, None) == {"ok": True} and len(calls) == 3        # tried again twice, then worked

    calls.clear()
    answers[:] = [_resp(503, detail="LLM service unavailable.", code="ai_busy")]
    with pytest.raises(ToolFailure) as caught:
        await executor.request_route(cred, "POST", "/x", None, {})
    assert len(calls) == 1                                                                                          # a write is never repeated by Ekavo
    assert caught.value.code == "ai_busy" and caught.value.retryable and caught.value.retry_after_seconds == 60 and "groq" not in caught.value.message.lower()


async def test_retries_are_bounded_and_a_timeout_is_said_honestly(monkeypatch):
    monkeypatch.setattr(executor, "RETRY_WAITS", (0.0, 0.0))
    calls = []

    async def always_busy(credential, method, path, query, body):
        calls.append(1)
        return _resp(503, detail="busy")

    monkeypatch.setattr(executor, "_send", always_busy)
    cred = Credential("k", "api", "ws", "u", frozenset({Scope.READ}))
    with pytest.raises(ToolFailure):
        await executor.request_route(cred, "GET", "/x", None, None)
    assert len(calls) == 1 + failures.AUTO_RETRIES                                                                  # never an endless loop

    async def slow(credential, method, path, query, body):
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(executor, "_send", slow)
    with pytest.raises(ToolFailure) as caught:
        await executor.request_route(cred, "POST", "/x", None, {})
    assert caught.value.code == "timed_out" and "may still be running" in caught.value.message and caught.value.retryable


# ── the cool-down ────────────────────────────────────────────────────────

async def test_a_writing_service_that_keeps_failing_starts_a_short_cooldown_and_a_success_ends_it():
    await failures.create_indexes()
    await failures.record_ai_success()
    assert await failures.ai_cooldown_left() == 0
    for _ in range(failures.AI_COOLDOWN_AFTER):
        await failures.record_ai_failure()
    left = await failures.ai_cooldown_left()
    assert 0 < left <= failures.AI_COOLDOWN_SECONDS
    await failures.record_ai_success()
    assert await failures.ai_cooldown_left() == 0


async def test_during_the_cooldown_only_writing_tools_are_refused_up_front_with_a_wait():
    from app.tools.registry import get_registry

    class Quiet:
        left = 45

        async def ai_cooldown_left(self, kind="writing"):
            return self.left if kind == "writing" else 0

    gate = Gatekeeper(get_registry(), Quiet())
    tool = get_registry().get("text.refine")
    assert tool.cost_key in executor.AI_COST_KEYS
    with pytest.raises(Refusal) as caught:
        await gate._ai_cooldown(tool)
    assert caught.value.code == "ai_busy" and caught.value.extra["retry_after_seconds"] == 45 and caught.value.body()["retryable"] is True
    await gate._ai_cooldown(get_registry().get("plan.limits"))                     # a tool that makes nothing is not held back
    await gate._ai_cooldown(get_registry().get("video.render"))                    # and neither is one that uses no writing model


# ── the public API end to end ────────────────────────────────────────────

@pytest.fixture
async def world(signup_user):
    client, _ = await signup_user()
    ws = await create_workspace(client, f"Fail {uuid4().hex[:6]}")
    await ledger.create_indexes()
    await ledger.settings().delete_many({})
    await failures.record_ai_success()
    made = await client.post(f"{D}/tool-keys", headers={"X-Workspace-Id": ws}, json={"name": "K", "scopes": ["read", "create"]})
    return client, ws, {"Authorization": f"Bearer {made.json()['secret']}"}


async def test_a_busy_answer_reaches_the_caller_with_a_wait_and_never_the_providers_words(world, monkeypatch):
    client, ws, key = world
    monkeypatch.setattr(executor, "RETRY_WAITS", (0.0, 0.0))

    async def busy(credential, method, path, query, body):
        return _resp(503, detail="Groq: rate limit reached for model gpt-oss, tokens per day", code="ai_busy")

    monkeypatch.setattr(executor, "_send", busy)
    res = await client.post(f"{P}/tools/presets.list/call", headers=key, json={"arguments": {}})
    error = res.json()["error"]
    assert res.status_code == 503 and error["code"] == "ai_busy" and error["retryable"] is True and error["retry_after_seconds"] == 60
    assert res.headers["retry-after"] == "60" and "groq" not in str(res.json()).lower() and "token" not in error["message"].lower() and res.json()["request_id"]
    row = await ledger.calls().find_one({"tool": "presets.list", "workspace_id": ws})
    assert row["outcome"] == "failed" and row["code"] == "ai_busy"


async def test_after_repeated_failures_new_writing_calls_wait_up_front_without_touching_the_service(world, monkeypatch):
    client, ws, key = world
    monkeypatch.setattr(executor, "RETRY_WAITS", (0.0, 0.0))
    for _ in range(failures.AI_COOLDOWN_AFTER):
        await failures.record_ai_failure()
    touched = []

    async def spy(credential, method, path, query, body):
        touched.append(path)
        return _resp(200, ok=True)

    monkeypatch.setattr(executor, "_send", spy)
    res = await client.post(f"{P}/tools/text.refine/call", headers=key, json={"arguments": {"content": "hello", "chip": "shorter", "platform": "LinkedIn", "brand_id": "b"}})
    error = res.json()["error"]
    assert res.status_code == 503 and error["code"] == "ai_busy" and error["retry_after_seconds"] > 0 and res.headers["retry-after"] and touched == []
    await failures.record_ai_success()


async def test_an_unexpected_bug_is_a_plain_message_with_an_id_and_never_a_stack_trace(world, monkeypatch):
    client, ws, key = world

    async def explode(admission, call):
        raise RuntimeError("secret internal detail at /srv/app/core.py line 42")

    monkeypatch.setattr(executor, "execute", explode)
    res = await client.post(f"{P}/tools/presets.list/call", headers=key, json={"arguments": {}})
    body = res.json()
    assert res.status_code == 500 and body["error"]["code"] == "internal_error" and body["error"]["retryable"] is True and body["request_id"]
    assert "secret" not in str(body) and "core.py" not in str(body) and "Traceback" not in str(body)
    row = await ledger.calls().find_one({"tool": "presets.list", "workspace_id": ws})
    assert row["outcome"] == "failed" and row["code"] == "internal_error"                    # logged for us, and no key is left stuck "in progress"


async def test_a_repeat_after_a_failure_is_not_stuck_in_progress(world, monkeypatch):
    client, ws, key = world
    headers = {**key, "Idempotency-Key": "retry-me"}
    monkeypatch.setattr(executor, "RETRY_WAITS", (0.0, 0.0))
    state = {"fail": True}

    async def flaky(credential, method, path, query, body):
        return _resp(503, detail="busy") if state["fail"] else _resp(200, items=[])

    monkeypatch.setattr(executor, "_send", flaky)
    first = await client.post(f"{P}/tools/presets.list/call", headers=headers, json={"arguments": {}})
    assert first.status_code == 503
    state["fail"] = False
    second = await client.post(f"{P}/tools/presets.list/call", headers=headers, json={"arguments": {}})
    assert second.status_code == 200 and second.json()["ok"] is True                           # the same key worked on the second try


# ── compound requests ────────────────────────────────────────────────────

class Script:
    """A stand-in for running one tool: each tool follows its own script of answers, and every call is recorded."""

    def __init__(self, **scripts: list[Any]):
        self.scripts, self.calls = scripts, []

    async def __call__(self, call: Call):
        self.calls.append((call.tool, call.idempotency_key))
        script = self.scripts[call.tool]
        step = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(step, Exception):
            raise step
        return step


def _busy(after=60):
    return ToolFailure.of(failures.ai_busy(after))


async def _start(script: Script, steps: list[Step], **kw):
    async def nosleep(_):
        return None

    return await compose.start("ws-c", "k-c", "u-c", steps, run_tool=script, sleep=nosleep, **kw)


def _facebook_steps():
    return [Step(label="write", tool="text.generate", arguments={"topic": "business tactics", "platforms": ["Facebook"]}),
            Step(label="tags", tool="text.refine", arguments={"content": "${write.data.text}", "chip": "hashtags"}),
            Step(label="picture", tool="image.generate", arguments={"title": "Business tactics", "text": "${write.data.text}"}, optional=True)]


@pytest.fixture
async def indexed():
    await compose.create_indexes()


async def test_when_the_picture_fails_the_post_and_hashtags_are_kept_and_the_run_says_what_is_missing(indexed):
    script = Script(**{"text.generate": [({"text": "Tactic one: focus."}, None)], "text.refine": [({"text": "Tactic one: focus. #business"}, None)], "image.generate": [_busy(60)]})
    out = await _start(script, _facebook_steps())
    assert out["status"] == "partial" and out["done"] == ["write", "tags"] and out["missing"] == ["picture"] and out["retry_after_seconds"] == 60
    assert "compose.resume" in out["next"] and "after 60 seconds" in out["next"] and "picture" in out["next"]
    states = {s["label"]: s for s in out["steps"]}
    assert states["picture"]["error"]["code"] == "ai_busy" and states["picture"]["error"]["retryable"] and "result" in states["write"]
    assert [t for t, _ in script.calls] == ["text.generate", "text.refine", "image.generate"]                  # the picture was tried once, not hammered


async def test_resume_redoes_only_what_failed_and_each_step_keeps_its_own_idempotency_key(indexed):
    script = Script(**{"text.generate": [({"text": "A post"}, None)], "text.refine": [({"text": "A post #x"}, None)], "image.generate": [_busy(60), ({"image_id": "i1"}, None)]})
    first = await _start(script, _facebook_steps())
    run = await compose.load("ws-c", first["run_id"])

    async def nosleep(_):
        return None

    second = await compose.advance(run, "k-c", run_tool=script, sleep=nosleep)
    assert second["status"] == "done" and second["missing"] == [] and second["next"] == ""
    assert [t for t, _ in script.calls] == ["text.generate", "text.refine", "image.generate", "image.generate"]       # the post was not written twice
    keys = [k for t, k in script.calls if t == "image.generate"]
    assert keys[0] == keys[1] == f"{first['run_id']}:2"                                                         # the same key: never made or charged twice


async def test_a_short_wait_is_retried_inside_the_call_and_a_long_wait_is_handed_back(indexed):
    script = Script(**{"text.generate": [ToolFailure.of(failures.Failure("ai_busy", "busy", True, 2, "", 503)), ({"text": "ok"}, None)]})
    out = await _start(script, [Step(label="write", tool="text.generate", arguments={})])
    assert out["status"] == "done" and out["steps"][0]["attempts"] == 2
    slow = Script(**{"text.generate": [_busy(60)]})
    out = await _start(slow, [Step(label="write", tool="text.generate", arguments={})])
    assert out["status"] == "failed" and len(slow.calls) == 1 and out["retry_after_seconds"] == 60                      # no holding the connection for a minute


async def test_a_required_step_that_fails_for_good_stops_the_run_and_later_steps_never_start(indexed):
    script = Script(**{"text.generate": [({"text": "x"}, None)], "text.refine": [ToolFailure("plan_limit", "Limit reached.", 402)], "image.generate": [({"image_id": "i"}, None)]})
    out = await _start(script, [Step(label="write", tool="text.generate"), Step(label="tags", tool="text.refine"), Step(label="picture", tool="image.generate")])
    assert out["status"] == "partial" and out["done"] == ["write"] and out["missing"] == ["tags", "picture"]
    assert [t for t, _ in script.calls] == ["text.generate", "text.refine"] and "cannot be fixed by trying again" in out["next"]


async def test_background_work_is_waited_for_then_handed_back_as_waiting_and_finished_later(indexed):
    jobs = {"j1": {"status": "running"}}

    async def get_job(ws, job_id):
        return jobs[job_id]

    script = Script(**{"text.generate": [({"accepted": True}, {"id": "j1"})]})
    steps = [Step(label="write", tool="text.generate")]
    out = await _start(script, steps, get_job=get_job, wait_seconds=0)
    assert out["status"] == "waiting" and "compose.status" in out["next"] and out["missing"] == ["write"]

    jobs["j1"] = {"status": "done", "result": {"data": {"text": "Done"}}}

    async def nosleep(_):
        return None

    run = await compose.load("ws-c", out["run_id"])
    finished = await compose.advance(run, "k-c", run_tool=script, get_job=get_job, sleep=nosleep, wait_seconds=0)
    assert finished["status"] == "done" and len(script.calls) == 1                                               # not started a second time


async def test_a_background_step_that_dies_on_the_models_is_reported_without_their_words(indexed):
    async def get_job(ws, job_id):
        return {"status": "failed", "error": "groq.RateLimitError: 429 tokens per day exceeded for llama-3.3"}

    script = Script(**{"text.generate": [({}, {"id": "j9"})]})
    out = await _start(script, [Step(label="write", tool="text.generate")], get_job=get_job)
    error = out["steps"][0]["error"]
    assert error["code"] == "ai_busy" and error["retryable"] and "groq" not in str(out).lower() and "429" not in str(out)


async def test_a_step_can_use_an_earlier_result_and_a_bad_reference_is_a_plain_error_that_costs_nothing(indexed):
    script = Script(**{"text.generate": [({"text": "Hello"}, None)], "text.refine": [({"text": "Hello!"}, None)]})
    out = await _start(script, [Step(label="write", tool="text.generate"), Step(label="tags", tool="text.refine", arguments={"content": "${write.data.text}"})])
    assert out["status"] == "done"
    assert compose.resolve({"a": ["${write.data.text}"]}, [{"label": "write", "state": "done", "result": {"data": {"text": "Hello"}}}]) == {"a": ["Hello"]}
    bad = Script(**{"text.generate": [({"text": "x"}, None)], "text.refine": [({}, None)]})
    out = await _start(bad, [Step(label="write", tool="text.generate"), Step(label="tags", tool="text.refine", arguments={"content": "${write.data.nothing}"})])
    assert out["steps"][1]["error"]["code"] == "input_invalid" and "did not return" in out["steps"][1]["error"]["message"] and [t for t, _ in bad.calls] == ["text.generate"]


async def test_steps_are_checked_before_anything_starts(indexed):
    for steps, code in (
        ([Step(label="a", tool="publish.now", arguments={})], "not_a_step"),
        ([Step(label="a", tool="library.delete_post", arguments={})], "not_a_step"),
        ([Step(label="a", tool="compose.run", arguments={})], "not_a_step"),
        ([Step(label="a", tool="no.such_tool")], "tool_unknown"),
        ([Step(label="a", tool="text.generate"), Step(label="a", tool="text.generate")], "input_invalid"),
        ([Step(label="a", tool="text.refine", arguments={"x": "${b.data}"}), Step(label="b", tool="text.generate")], "input_invalid"),
    ):
        script = Script(**{"text.generate": [({}, None)], "text.refine": [({}, None)]})
        with pytest.raises(ToolFailure) as caught:
            await _start(script, steps)
        assert caught.value.code == code and script.calls == []


async def test_a_run_is_only_visible_to_its_own_workspace(indexed):
    script = Script(**{"text.generate": [({"text": "x"}, None)]})
    out = await _start(script, [Step(label="write", tool="text.generate")])
    assert (await compose.load("ws-c", out["run_id"]))["id"] == out["run_id"]
    with pytest.raises(ToolFailure) as caught:
        await compose.load("another-workspace", out["run_id"])
    assert caught.value.status == 404


async def test_the_compose_tools_run_through_the_public_api_with_each_step_through_the_gatekeeper(world, monkeypatch):
    client, ws, key = world
    res = await client.post(f"{P}/tools/compose.run/call", headers=key, json={"arguments": {"steps": [{"label": "a", "tool": "publish.now", "arguments": {}}]}})
    assert res.status_code == 422 and res.json()["error"]["code"] == "not_a_step"
    ok = await client.post(f"{P}/tools/compose.run/call", headers=key, json={"arguments": {"steps": [{"label": "plans", "tool": "plan.limits", "arguments": {}}]}})
    assert ok.status_code == 200, ok.text
    data = ok.json()["data"]
    assert data["status"] == "done" and data["steps"][0]["state"] == "done"
    status = await client.post(f"{P}/tools/compose.status/call", headers=key, json={"arguments": {"run_id": data["run_id"]}})
    assert status.json()["data"]["status"] == "done"
    calls = await ledger.calls().count_documents({"workspace_id": ws, "tool": "plan.limits"})
    assert calls == 1                                                                              # the step went through the gatekeeper and the ledger
    assert datetime.now(timezone.utc) and get_db() is not None


async def test_each_service_has_its_own_cooldown_and_words_so_a_dead_picture_service_never_blocks_writing():
    await failures.create_indexes()
    await failures.record_ai_success()
    for _ in range(failures.AI_COOLDOWN_AFTER):
        await failures.record_ai_failure("picture")
    assert await failures.ai_cooldown_left("picture") > 0 and await failures.ai_cooldown_left("writing") == 0 and await failures.ai_cooldown_left("voice") == 0
    from app.tools.registry import get_registry

    class Services:
        async def ai_cooldown_left(self, kind="writing"):
            return await failures.ai_cooldown_left(kind)

    gate = Gatekeeper(get_registry(), Services())
    await gate._ai_cooldown(get_registry().get("text.refine"))                                      # writing still works
    with pytest.raises(Refusal) as caught:
        await gate._ai_cooldown(get_registry().get("image.generate"))
    assert caught.value.code == "picture_busy" and "picture service" in caught.value.message and caught.value.body()["retry_after_seconds"] > 0
    assert failures.ai_busy(kind="voice").code == "voice_busy" and failures.ai_busy(kind="transcription").code == "transcription_busy"
    await failures.record_ai_success("picture")
    assert await failures.ai_cooldown_left("picture") == 0


def test_a_picture_that_fell_back_to_a_text_card_is_reported_as_a_warning_not_as_success():
    warned = executor.degraded({"id": "i1", "qa_flagged": True, "qa_flag_reason": "No AI picture, so this is a text card. The picture service is busy."})
    assert warned and warned[0]["code"] == "degraded" and "text card" in warned[0]["message"]
    assert executor.degraded({"id": "i1", "qa_flagged": False, "qa_flag_reason": None}) == ()
    assert executor.degraded({"media": {"qa_flagged": True, "qa_flag_reason": "The brand logo couldn't be loaded."}})[0]["message"].startswith("The brand logo")
    assert executor.degraded(["nothing"]) == ()


async def test_a_warning_travels_with_a_step_and_with_the_answer(indexed):
    script = Script(**{"image.generate": [({"id": "i1", "qa_flagged": True, "qa_flag_reason": "No AI picture, so this is a text card."}, None)]})
    out = await _start(script, [Step(label="picture", tool="image.generate")])
    assert out["status"] == "done" and out["steps"][0]["warnings"][0]["message"].startswith("No AI picture")
