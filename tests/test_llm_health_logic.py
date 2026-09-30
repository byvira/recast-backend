"""Status, quota, banner, reports and alert decisions. Pure tests, no network and no database."""
from datetime import datetime, timedelta, timezone

from app.shared.llm_health import alerts, health, reports

NOW = datetime(2026, 10, 2, 10, 30, tzinfo=timezone.utc)


# ---- quota -----------------------------------------------------------------------------------------------
def test_the_daily_reset_follows_the_providers_time_zone():
    utc = health.last_reset(NOW, {"tz": "UTC", "hour": 0})
    assert utc == datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc)
    assert health.next_reset(NOW, {"tz": "UTC", "hour": 0}) == datetime(2026, 10, 3, 0, 0, tzinfo=timezone.utc)
    pacific = health.last_reset(NOW, {"tz": "America/Los_Angeles", "hour": 0})
    assert pacific == datetime(2026, 10, 2, 7, 0, tzinfo=timezone.utc)  # midnight Pacific daylight time is 07:00 UTC
    assert health.next_reset(NOW, {"tz": "America/Los_Angeles", "hour": 0}) > NOW


def test_percent_and_levels_use_text_thresholds():
    assert health.percent(50, 100) == 50.0 and health.percent(1, None) is None
    assert [health.level(p) for p in (None, 10, 70, 89.9, 90, 150)] == ["unknown", "fine", "warning", "warning", "critical", "critical"]
    assert health.level(60, warn=50, critical=80) == "warning"


def test_quota_rows_only_for_windows_with_a_limit():
    rows = health.quota_rows({"rpd": 1000, "tpd": 200000, "rpm": 30}, {"calls": 950, "tokens": 20000, "calls_min": 27}, warn=70, critical=90)
    assert [r["window"] for r in rows] == ["rpd", "tpd", "rpm"]
    assert rows[0]["level"] == "critical" and rows[1]["level"] == "fine" and rows[0]["percent"] == 95.0
    assert rows[2]["label"] == "Requests this minute" and rows[2]["percent"] == 90.0 and rows[2]["level"] == "critical"
    assert health.quota_rows({}, {"calls": 5}, warn=70, critical=90) == []


def test_projection_says_when_the_allowance_runs_out_only_if_before_reset():
    start = NOW - timedelta(hours=10)
    reset = NOW + timedelta(hours=13, minutes=30)
    fast = health.projection(600, 1000, start, NOW, reset)
    assert fast and fast["before_reset"] and fast["runs_out_at"] < reset
    slow = health.projection(100, 1000, start, NOW, reset)
    assert slow and not slow["before_reset"]
    assert health.projection(0, 1000, start, NOW, reset) is None
    assert health.projection(50, 1000, NOW - timedelta(minutes=2), NOW, reset) is None  # too little data to guess
    assert health.projection(1200, 1000, start, NOW, reset)["before_reset"]


def test_starting_limits_are_the_owners_and_gemini_has_none_invented():
    assert health.DEFAULT_LIMITS["groq"]["openai/gpt-oss-120b"] == {"rpm": 30, "rpd": 1000, "tpm": 8000, "tpd": 200000}
    assert health.DEFAULT_LIMITS["gemini"] == {}
    assert health.DEFAULT_RESET["gemini"]["tz"] == "America/Los_Angeles"


# ---- provider status and banner ------------------------------------------------------------------------------
def status(**kw):
    base = dict(calls=100, successes=100, quota_levels=[], open_issue_priorities=[], auth_failed=False, limit_reached=False, fallback_used=False)
    return health.provider_status(**{**base, **kw})


def test_provider_status_rules():
    assert status() == "healthy"
    assert status(calls=0, successes=0) == "idle"
    assert status(successes=95) == "degraded"
    assert status(quota_levels=["critical"]) == "degraded"
    assert status(fallback_used=True) == "degraded"
    assert status(limit_reached=True) == "limit_reached"
    assert status(successes=40) == "down"
    assert status(auth_failed=True) == "down"


def test_the_banner_reads_in_plain_words():
    assert health.overall({"groq": "healthy", "gemini": "idle"}, user_facing_failures=False, top_issue_title=None)["state"] == "all_working"
    partly = health.overall({"groq": "limit_reached", "gemini": "healthy"}, user_facing_failures=False, top_issue_title="Groq ran out of free requests for today")
    assert partly["state"] == "partly_working" and "fallback" in partly["text"]
    bad = health.overall({"groq": "down", "gemini": "down"}, user_facing_failures=True, top_issue_title=None)
    assert bad["state"] == "not_working"
    assert health.overall({}, user_facing_failures=False, top_issue_title=None)["state"] == "all_working"
    # open failures that people see win, even when the providers look idle or healthy
    assert health.overall({"groq": "idle", "gemini": "healthy"}, user_facing_failures=True, top_issue_title=None)["state"] == "not_working"
    assert health.overall({}, user_facing_failures=True, top_issue_title="Generation is failing for hooks")["text"].endswith("hooks.")


# ---- reports -----------------------------------------------------------------------------------------------
ISSUE = {
    "number": 142, "error_type": "quota_daily", "provider": "groq", "model": "openai/gpt-oss-120b", "status": "open", "priority": "medium",
    "first_seen": NOW - timedelta(minutes=35), "last_seen": NOW, "failed_request_count": 34, "affected_workspaces": ["a", "b", "c"],
    "features": ["text_generate", "hooks"], "prompt_paths": ["text/generate/master"], "fallback_outcome": "working", "last_app_version": "abc123", "notes": [],
}


def test_an_issue_report_is_complete_and_has_no_status_code():
    text = reports.issue_report(ISSUE, link="https://x/ops/llm/issues/142", reset_at=NOW + timedelta(hours=3))
    for needle in ("ISSUE-0142", "Groq ran out of free requests for today", "34 failed requests across 3 workspaces", "text_generate, hooks",
                   "Fallback: working", "Resets at", "prompt=text/generate/master", "version=abc123", "Link: https://x/ops/llm/issues/142"):
        assert needle in text, needle
    assert "429" not in text


def test_a_status_summary_lists_the_top_issues():
    banner = {"state": "partly_working", "text": "Partly working. Generation still succeeds through the fallback."}
    text = reports.status_summary(banner, {"groq": "limit_reached", "gemini": "healthy"}, [ISSUE], link="https://x/ops/llm")
    assert "Groq: limit reached" in text and "ISSUE-0142 (medium)" in text and text.endswith("https://x/ops/llm")
    assert "No open issues." in reports.status_summary(banner, {}, [], link="l")


# ---- alert decisions -------------------------------------------------------------------------------------------
def new(priority="high", number=1):
    return {"number": number, "priority": priority, "error_type": "quota_daily", "provider": "groq", "model": "m", "features": ["hooks"], "prompt_paths": []}


def decide(**kw):
    base = dict(rules={}, created=[], reopened=[], quota=[], providers={}, recently_sent={}, now=NOW, base_url="https://x")
    return alerts.decide(**{**base, **kw})


def test_only_important_new_issues_alert_by_default():
    out = decide(created=[new("high", 1), new("medium", 2), new("critical", 3), new("low", 4)])
    assert sorted(a.key for a in out) == ["issue:1:new", "issue:3:new"]
    assert len(decide(rules={"new_issue_min_priority": "medium"}, created=[new("medium", 2)])) == 1


def test_the_same_alert_is_not_sent_again_during_its_cooldown():
    key = "issue:1:new"
    assert decide(created=[new()], recently_sent={key: NOW - timedelta(minutes=10)}) == []
    assert len(decide(created=[new()], recently_sent={key: NOW - timedelta(minutes=90)})) == 1
    assert decide(created=[new()], rules={"cooldown_minutes": 5}, recently_sent={key: NOW - timedelta(minutes=10)})


def test_quota_alerts_fire_once_per_window_and_can_be_switched_off():
    q = {"provider": "groq", "model": "m", "window": "rpd", "window_start": "2026100200", "label": "Requests today", "used": 950, "limit": 1000, "percent": 95.0, "level": "critical", "reset_at": "tomorrow"}
    out = decide(quota=[q])
    assert len(out) == 1 and out[0].kind == "quota_critical" and "almost out" in out[0].subject
    assert decide(quota=[q], rules={"quota_critical": False}) == []
    warn = decide(quota=[{**q, "level": "warning", "percent": 75.0}])
    assert warn[0].kind == "quota_warning"
    assert decide(quota=[{**q, "level": "fine"}]) == []


def test_reopened_and_provider_down_alerts():
    r = decide(reopened=[{**new("medium", 9), "regressed": True}])
    assert r[0].kind == "reopened" and "soon after it was fixed" in r[0].subject
    d = decide(providers={"groq": "down", "gemini": "healthy"})
    assert [a.key for a in d] == ["provider:groq:down"]
    assert decide(providers={"groq": "down"}, rules={"provider_down": False}) == []


def test_minute_usage_counts_only_the_last_minute_for_that_model():
    import time

    from app.shared.llm_health import service
    from app.shared.llm_health.recorder import recorder

    recorder.recent.clear()
    now = time.time()
    recorder.recent["groq"] = __import__("collections").deque([
        (now - 120, True, False, False, "m", 500),   # too old
        (now - 10, True, False, False, "m", 300),
        (now - 5, True, False, False, "m", 200),
        (now - 5, True, False, False, "other", 900),  # another model
    ])
    assert service.minute_usage("groq", "m") == {"calls_min": 2, "tokens_min": 500}
    assert service.minute_usage("groq", "missing") == {"calls_min": 0, "tokens_min": 0}
    recorder.recent.clear()
