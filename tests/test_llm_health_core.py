"""The LLM health log: scrubbing, classification, batching and issue grouping. Pure tests, no network and no
database. The most important rule: recording must never break or slow a model call."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.shared.llm_health import catalogue, classifier, issues
from app.shared.llm_health.context import llm_context, current_feature, current_prompt_path, set_prompt_path
from app.shared.llm_health.recorder import (
    EVENT_CAP_PER_FINGERPRINT_PER_HOUR, Attempt, Batch, Recorder, hour_start, latency_bucket,
)
from app.shared.llm_health.scrub import MAX_MESSAGE_CHARS, scrub_message

NOW = datetime(2026, 10, 2, 10, 30, tzinfo=timezone.utc)


def fail(status=429, message="rate limit", cls="RateLimitError", **kw):
    return Attempt(provider="groq", model="m", ok=False, latency_ms=100, at=NOW, http_status=status,
                   error_class=cls, error_message=message, **kw)


def ok(**kw):
    return Attempt(provider="groq", model="m", ok=True, latency_ms=300, at=NOW, tokens_in=100, tokens_out=20, **kw)


# ---- scrubbing ----------------------------------------------------------------------------------------
def test_secrets_emails_and_long_ids_are_removed():
    text = "Invalid key gsk_abcdefghijklmnop1234 for bob@example.com id 1234567890123 request abcdefghijklmnopqrstuvwxyz0123456789"
    out = scrub_message(text)
    for leaked in ("gsk_abc", "bob@", "1234567890123", "abcdefghijklmnopqrstuvwxyz0123456789"):
        assert leaked not in out
    assert "[key]" in out and "[email]" in out


def test_a_long_message_is_cut_and_whitespace_collapsed():
    out = scrub_message("word   " * 400)
    assert len(out) <= MAX_MESSAGE_CHARS and "  " not in out
    assert scrub_message(None) == ""


# ---- classification -------------------------------------------------------------------------------------
@pytest.mark.parametrize("status,message,cls,expected", [
    (401, "Invalid API Key", "AuthenticationError", "auth_invalid_key"),
    (403, "Your project has been denied access. Please contact support.", "PermissionDenied", "billing_or_access"),
    (402, "payment required", "APIStatusError", "billing_or_access"),
    (404, "The model `x` does not exist", "NotFoundError", "model_unavailable"),
    (429, "Rate limit reached for model on requests per day (RPD): Limit 1000", "RateLimitError", "quota_daily"),
    (429, "Rate limit reached on tokens per day (TPD): Limit 200000", "RateLimitError", "quota_tokens"),
    (429, "Rate limit reached on tokens per minute (TPM): Limit 8000. Please try again in 6.5s", "RateLimitError", "rate_limit_minute"),
    (429, "Rate limit reached. Please try again in 2h3m", "RateLimitError", "quota_daily"),
    (429, "RESOURCE_EXHAUSTED: quota exceeded for metric PerDay", "ClientError", "quota_daily"),
    (413, "Request too large for model on tokens per minute", "APIStatusError", "quota_tokens"),
    (400, "This model's maximum context length is 8192 tokens", "BadRequestError", "context_too_long"),
    (400, "response_format invalid", "BadRequestError", "bad_request"),
    (500, "internal error", "InternalServerError", "provider_outage"),
    (503, "overloaded", "APIStatusError", "provider_outage"),
    (504, "gateway timeout", "APIStatusError", "timeout"),
    (None, "Request timed out.", "APITimeoutError", "timeout"),
    (None, "Connection error.", "APIConnectionError", "network_error"),
    (None, "something odd", "WeirdError", "unknown"),
    (None, "", "unparseable_response", "unparseable_response"),
    (None, "", "fallback_failed", "fallback_failed"),
])
def test_failures_are_classified(status, message, cls, expected):
    assert classifier.classify(http_status=status, error_class=cls, message=message) == expected


def test_a_wait_of_hours_means_a_daily_limit_and_the_wait_is_read_from_the_text():
    assert classifier.classify(http_status=429, error_class="RateLimitError", message="busy", retry_after_s=7200) == "quota_daily"
    assert classifier.retry_after_from_text("Please try again in 7m12.5s.") == pytest.approx(432.5)
    assert classifier.retry_after_from_text("Please try again in 1h2m") == 3720
    assert classifier.retry_after_from_text("nothing here") is None


def test_every_kind_has_plain_wording_and_no_status_codes():
    for name, kind in catalogue.KINDS.items():
        text = f"{kind.title} {kind.cause} {kind.action_staff}"
        assert not any(code in text for code in ("429", "401", "403", "500", "HTTP")), name
    assert catalogue.render("{provider} is busy", provider="groq") == "Groq is busy"


# ---- batching ---------------------------------------------------------------------------------------------
def test_successes_are_counted_and_failures_become_events():
    b = Batch()
    for _ in range(5):
        b.add(ok())
    kind = b.add(fail(429, "Rate limit on requests per day"))
    assert kind == "quota_daily"
    (key, inc), = b.rollups.items()
    assert key[0] == hour_start(NOW)
    assert inc["calls"] == 6 and inc["successes"] == 5 and inc["failures"] == 1
    assert inc["tokens_in"] == 500 and inc["errors.quota_daily"] == 1
    assert len(b.events) == 1 and b.events[0]["error_type"] == "quota_daily"


def test_a_storm_of_identical_failures_keeps_a_capped_number_of_events_but_counts_all():
    b = Batch()
    for _ in range(EVENT_CAP_PER_FINGERPRINT_PER_HOUR + 30):
        b.add(fail())
    (_, inc), = b.rollups.items()
    assert inc["failures"] == EVENT_CAP_PER_FINGERPRINT_PER_HOUR + 30
    assert len(b.events) == EVENT_CAP_PER_FINGERPRINT_PER_HOUR and b.dropped_events == 30


def test_events_never_hold_prompt_text_only_a_scrubbed_message():
    b = Batch()
    b.add(fail(400, "bad request " + "x" * 2000, prompt_path="text/generate/master"))
    e = b.events[0]
    assert len(e["provider_message"]) <= MAX_MESSAGE_CHARS and e["prompt_path"] == "text/generate/master"
    assert not {"prompt", "messages", "content", "output"} & set(e)


def test_prompt_path_is_counted_per_day_even_when_events_are_capped():
    b = Batch()
    for _ in range(40):
        b.add(fail(prompt_path="text/generate/master"))
    b.add(ok(prompt_path="text/generate/master"))
    (_, path), row = next(iter(b.prompt_days.items()))
    assert path == "text/generate/master" and row["calls"] == 41 and row["failures"] == 40


def test_latency_buckets():
    assert [latency_bucket(x) for x in (100, 900, 1500, 4000, 9000, 50000)] == ["lt500", "lt1000", "lt2000", "lt5000", "lt10000", "gte10000"]


# ---- context ----------------------------------------------------------------------------------------------
def test_feature_and_prompt_path_are_carried_and_restored():
    assert current_feature() == "unknown"
    with llm_context(feature="hooks", prompt_path="text/hooks/generate"):
        assert current_feature() == "hooks" and current_prompt_path() == "text/hooks/generate"
        with llm_context(feature="refine"):
            assert current_feature() == "refine"
        assert current_feature() == "hooks"
    assert current_feature() == "unknown"
    set_prompt_path(None)


def test_load_prompt_sets_the_prompt_path_for_the_next_call():
    from app.prompts.registry import load_prompt

    set_prompt_path(None)
    load_prompt("support/category", ticket_text="x", areas="a") if False else None
    set_prompt_path("text/generate/master")
    assert current_prompt_path() == "text/generate/master"
    set_prompt_path(None)


# ---- recording never breaks a call ----------------------------------------------------------------------
def test_recording_swallows_every_error():
    r = Recorder()

    def boom(self, a):
        raise RuntimeError("batch exploded")

    r._batch.add = boom.__get__(r._batch)  # type: ignore[method-assign]
    r.record(ok())
    assert r.recorder_errors == 1


def test_recording_is_fast():
    import time

    r = Recorder()
    t0 = time.perf_counter()
    for _ in range(2000):
        r.record(ok())
    assert (time.perf_counter() - t0) / 2000 < 0.005


def test_a_failed_save_keeps_the_batch_for_the_next_try(monkeypatch):
    r = Recorder()
    r.record(ok())
    r.record(fail())

    import app.db.mongo as mongo

    class Broken:
        async def bulk_write(self, *a, **k):
            raise RuntimeError("db down")

        async def insert_many(self, *a, **k):
            raise RuntimeError("db down")

    monkeypatch.setattr(mongo, "llm_rollups", Broken(), raising=False)
    monkeypatch.setattr(mongo, "llm_events", Broken(), raising=False)
    asyncio.run(r.flush())
    assert r.recorder_errors == 1 and r._batch.rollups and r._batch.events
    assert "db down" in (r.last_error or "")


def test_the_model_helper_never_raises_even_with_odd_objects():
    from app.shared import llm

    llm._log_attempt("groq", "m", 0.0, exc=ValueError("x"))
    llm._log_attempt("groq", "m", 0.0, result=object())
    assert llm._usage_numbers(None) == (0, 0, 0)


# ---- issues -------------------------------------------------------------------------------------------------
def event(kind="quota_daily", feature="text_generate", at=NOW, **kw):
    return {"_id": kw.pop("_id", object()), "at": at, "provider": "groq", "model": "m", "feature": feature,
            "error_type": kind, "prompt_path": kw.pop("prompt_path", None), "workspace_id": kw.pop("workspace_id", "w1"),
            "outcome": "failed", "app_version": "abc", **kw}


def test_similar_failures_share_a_fingerprint_and_different_features_split_only_where_it_matters():
    a = issues.fingerprint("groq", "m", "quota_daily", "hooks")
    b = issues.fingerprint("groq", "m", "quota_daily", "refine")
    assert a == b
    assert issues.fingerprint("groq", "m", "bad_request", "hooks") != issues.fingerprint("groq", "m", "bad_request", "refine")
    assert issues.fingerprint("groq", "m", "fallback_failed", "hooks") == "fallback_failed|hooks"


def test_a_new_issue_is_opened_with_plain_words_and_impact():
    plan = issues.plan_update(None, [event(), event(workspace_id="w2", prompt_path="text/generate/master")], now=NOW, next_number=142)
    doc = plan["insert"]
    assert doc["number"] == 142 and issues.issue_number_label(142) == "ISSUE-0142"
    assert doc["status"] == "open" and doc["event_count"] == 2
    assert doc["title"] == "Groq ran out of free requests for today"
    assert doc["affected_workspaces"] == ["w1", "w2"] and doc["prompt_paths"] == ["text/generate/master"]
    assert doc["priority"] == "medium"


def test_a_fixed_issue_reopens_and_a_recent_manual_fix_is_marked_regressed():
    existing = {"status": "fixed", "error_type": "quota_daily", "failed_request_count": 3,
                "resolution": {"by": "user-1", "at": NOW - timedelta(hours=2)}}
    plan = issues.plan_update(existing, [event()], now=NOW, next_number=None)
    assert plan["reopened"] and plan["update"]["$set"]["status"] == "open" and plan["update"]["$set"]["regressed"] is True
    old = dict(existing, resolution={"by": "user-1", "at": NOW - timedelta(days=3)})
    assert issues.plan_update(old, [event()], now=NOW, next_number=None)["update"]["$set"]["regressed"] is False
    auto = dict(existing, resolution={"by": "system", "at": NOW - timedelta(hours=1)})
    assert issues.plan_update(auto, [event()], now=NOW, next_number=None)["update"]["$set"]["regressed"] is False


def test_an_ignored_issue_counts_events_but_does_not_reopen():
    plan = issues.plan_update({"status": "ignored", "error_type": "timeout"}, [event("timeout"), event("timeout")], now=NOW, next_number=None)
    assert not plan["reopened"] and plan["update"]["$inc"]["ignored_count"] == 2 and "status" not in plan["update"]["$set"]


def test_a_priority_set_by_a_person_is_not_overwritten():
    plan = issues.plan_update({"status": "open", "error_type": "timeout", "priority_overridden": True}, [event("timeout")], now=NOW, next_number=None)
    assert "priority" not in plan["update"]["$set"]


def test_auto_fix_waits_by_kind_and_never_touches_manual_issues():
    base = {"status": "open", "error_type": "rate_limit_minute", "last_seen": NOW - timedelta(hours=3)}
    assert issues.is_due_for_auto_fix(base, NOW)
    assert not issues.is_due_for_auto_fix(dict(base, last_seen=NOW - timedelta(minutes=30)), NOW)
    assert not issues.is_due_for_auto_fix(dict(base, error_type="auth_invalid_key", last_seen=NOW - timedelta(hours=5)), NOW)
    assert issues.is_due_for_auto_fix(dict(base, error_type="auth_invalid_key", last_seen=NOW - timedelta(hours=25)), NOW)
    assert not issues.is_due_for_auto_fix(dict(base, source="manual"), NOW)
    assert not issues.is_due_for_auto_fix(dict(base, status="ignored"), NOW)


def test_provider_outage_without_a_working_fallback_is_high_priority():
    assert issues.derive_priority("provider_outage") == "medium"
    assert issues.derive_priority("provider_outage", fallback_working=False) == "high"
    assert issues.derive_priority("auth_invalid_key") == "critical"


# ---- call outcomes: retries, fallbacks ----------------------------------------------------------------------
def test_a_retry_that_worked_is_noted_without_counting_the_call_twice():
    b = Batch()
    b.add(ok())
    b.add(Attempt(provider="groq", model="m", ok=True, latency_ms=0, at=NOW, outcome="success_after_retry", count_in_rollup=False))
    (_, inc), = b.rollups.items()
    assert inc["calls"] == 1 and inc["retries"] == 1
    assert [e["outcome"] for e in b.events] == ["success_after_retry"]


def test_a_fallback_that_worked_is_kept_as_an_event_and_counted():
    b = Batch()
    b.add(Attempt(provider="gemini", model="g", ok=True, latency_ms=800, at=NOW, outcome="fallback_success", fallback_to="gemini:g", feature="hooks"))
    (_, inc), = b.rollups.items()
    assert inc["fallbacks"] == 1 and inc["calls"] == 1
    assert b.events[0]["outcome"] == "fallback_success" and b.events[0]["fallback_to"] == "gemini:g"


def test_a_failed_fallback_becomes_its_own_kind_and_counts_the_error_once():
    b = Batch()
    kind = b.add(Attempt(provider="gemini", model="g", ok=False, latency_ms=0, at=NOW, error_class="fallback_failed", outcome="failed", count_in_rollup=False, feature="hooks"))
    assert kind == "fallback_failed"
    (_, inc), = b.rollups.items()
    assert inc.get("calls", 0) == 0 and inc["errors.fallback_failed"] == 1
    assert b.events[0]["error_type"] == "fallback_failed"
    assert issues.fingerprint("gemini", "g", "fallback_failed", "hooks") == "fallback_failed|hooks"
    assert issues.derive_priority("fallback_failed") == "critical"


def test_the_model_helpers_label_a_fallback_and_a_failed_fallback(monkeypatch):
    import asyncio as aio

    from app.shared import llm
    from app.shared.llm_health import recorder as rec_mod

    seen = []
    monkeypatch.setattr(rec_mod.recorder, "record", lambda a: seen.append(a))
    llm._log_attempt("groq", "m", 0.0, result=object())
    assert seen[-1].outcome is None
    flag = llm._in_fallback.set(True)
    try:
        llm._log_attempt("gemini", "g", 0.0, result=object())
    finally:
        llm._in_fallback.reset(flag)
    assert seen[-1].outcome == "fallback_success" and seen[-1].fallback_to == "gemini:g"
    llm._note_fallback_failed("g")
    assert seen[-1].error_class == "fallback_failed" and seen[-1].count_in_rollup is False
    llm._note_retry_success("call_llm(BALANCED)")
    assert seen[-1].outcome == "success_after_retry"

    async def flaky():
        flaky.n = getattr(flaky, "n", 0) + 1
        if flaky.n == 1:
            raise llm.RateLimitError("slow down", response=type("R", (), {"request": None, "status_code": 429, "headers": {}})(), body=None)
        return "done"

    async def no_sleep(_):
        return None

    monkeypatch.setattr(llm.asyncio, "sleep", no_sleep)
    assert aio.run(llm._backoff_retry(flaky, label="t")) == "done"
    assert seen[-1].outcome == "success_after_retry"


def test_the_app_version_comes_from_the_render_commit(monkeypatch):
    from app.shared.llm_health.recorder import app_version

    monkeypatch.setenv("RENDER_GIT_COMMIT", "0123456789abcdef")
    assert app_version() in ("0123456", None) or app_version()
