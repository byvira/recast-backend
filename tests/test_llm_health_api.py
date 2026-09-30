"""LLM health routes and the recorder writing to the real test database. Model calls are never made:
failures are fed to the recorder directly."""
from datetime import datetime, timezone

from app.db.mongo import users
from app.shared.llm_health import issues
from app.shared.llm_health.recorder import Attempt, Recorder
from tests.conftest import signup_new_user

B = "/api/v1/ops/llm"


async def _staff(make_client, master: bool = False):
    client = make_client()
    user = await signup_new_user(client, name="Staff")
    fields = {"is_platform_staff": True}
    if master:
        fields["is_master_admin"] = True
    await users.update_one({"id": user["id"]}, {"$set": fields})
    return client, user


async def _fail_and_flush(provider: str, model: str, message: str, status: int = 429, count: int = 3, prompt_path=None):
    rec = Recorder()
    for _ in range(count):
        rec.record(Attempt(provider=provider, model=model, ok=False, latency_ms=120, http_status=status, error_class="RateLimitError",
                           error_message=message, feature="hooks", prompt_path=prompt_path, workspace_id="w-test"))
    rec.record(Attempt(provider=provider, model=model, ok=True, latency_ms=300, tokens_in=50, tokens_out=10, feature="hooks"))
    await rec.flush()
    return rec


async def _issue_number(client, model: str) -> int:
    res = await client.get(f"{B}/issues", params={"q": "", "limit": 200})
    assert res.status_code == 200, res.text
    hit = [i for i in res.json()["items"] if i["model"] == model]
    assert hit, "the issue was not created"
    return hit[0]["number"]


async def test_failures_become_one_issue_with_counts_and_a_prompt_path(make_client):
    client, _ = await _staff(make_client)
    model = "m-" + datetime.now().strftime("%H%M%S%f")
    rec = await _fail_and_flush("groq", model, "Rate limit on requests per day (RPD): Limit 1000", prompt_path="text/hooks/generate")
    assert rec.recorder_errors == 0, rec.last_error
    number = await _issue_number(client, model)
    issue = (await client.get(f"{B}/issues/{number}")).json()
    assert issue["error_type"] == "quota_daily" and issue["status"] == "open"
    assert issue["title"] == "Groq ran out of free requests for today"
    assert issue["event_count"] == 3 and issue["prompt_paths"] == ["text/hooks/generate"] and issue["features"] == ["hooks"]
    assert issue["affected_workspace_count"] == 1

    await _fail_and_flush("groq", model, "Rate limit on requests per day (RPD): Limit 1000", count=2)
    again = (await client.get(f"{B}/issues/{number}")).json()
    assert again["event_count"] == 5 and again["number"] == number  # grouped, not a new issue

    events = (await client.get(f"{B}/issues/{number}/events")).json()
    assert events["total"] == 5 and "prompt" not in events["items"][0]


async def test_status_notes_report_and_ignore_rules(make_client):
    client, _ = await _staff(make_client)
    model = "m-" + datetime.now().strftime("%H%M%S%f")
    await _fail_and_flush("groq", model, "Invalid API Key", status=401)
    number = await _issue_number(client, model)
    assert (await client.get(f"{B}/issues/{number}")).json()["priority"] == "critical"

    assert (await client.patch(f"{B}/issues/{number}", json={"status": "ignored"})).status_code == 422
    assert (await client.patch(f"{B}/issues/{number}", json={"priority": "low"})).status_code == 422
    ack = (await client.patch(f"{B}/issues/{number}", json={"status": "acknowledged", "owner_id": "someone"})).json()
    assert ack["status"] == "acknowledged" and ack["owner_id"] == "someone"
    assert (await client.post(f"{B}/issues/{number}/notes", json={"text": "Looking at the key now"})).json()["notes"][0]["text"].startswith("Looking")

    report = (await client.get(f"{B}/issues/{number}/report")).json()["text"]
    assert f"ISSUE-{number:04d}" in report and "rejected our key" in report and "Link:" in report
    assert "401" not in report

    fixed = (await client.patch(f"{B}/issues/{number}", json={"status": "fixed", "reason": "Key replaced"})).json()
    assert fixed["status"] == "fixed" and fixed["resolution"]["by"]
    await _fail_and_flush("groq", model, "Invalid API Key", status=401, count=1)
    back = (await client.get(f"{B}/issues/{number}")).json()
    assert back["status"] == "open" and back["reopen_count"] == 1 and back["regressed"] is True

    ignored = (await client.patch(f"{B}/issues/{number}", json={"status": "ignored", "reason": "Known while we rotate keys"})).json()
    assert ignored["status"] == "ignored"
    await _fail_and_flush("groq", model, "Invalid API Key", status=401, count=2)
    quiet = (await client.get(f"{B}/issues/{number}")).json()
    assert quiet["status"] == "ignored" and quiet["ignored_count"] == 2


async def test_manual_issue_and_filters(make_client):
    client, _ = await _staff(make_client)
    made = await client.post(f"{B}/issues", json={"title": "Images look wrong", "description": "Cards are flat", "feature": "image", "priority": "high"})
    assert made.status_code == 200, made.text
    number = made.json()["number"]
    got = (await client.get(f"{B}/issues/{number}")).json()
    assert got["source"] == "manual" and got["title"] == "Images look wrong" and got["priority"] == "high"
    listed = (await client.get(f"{B}/issues", params={"status": "active", "priority": "high", "q": "images look"})).json()
    assert any(i["number"] == number for i in listed["items"])
    assert (await client.get(f"{B}/issues/999999")).status_code == 404


async def test_health_overview_reads_the_saved_counts(make_client):
    client, _ = await _staff(make_client)
    await _fail_and_flush("groq", "m-overview", "Rate limit reached on tokens per minute (TPM). Please try again in 6s", count=2)
    data = (await client.get(f"{B}/health", params={"range": "24h"})).json()
    assert data["banner"]["state"] in {"all_working", "partly_working", "not_working"}
    assert data["tiles"]["calls"] >= 3 and data["tiles"]["rate_limited"] >= 2
    assert any(f["feature"] == "hooks" for f in data["by_feature"])
    assert data["series"] and "recorder" in data and data["retention"]["events_days"] == 30
    assert "groq" in data["providers"] and data["providers"]["groq"]["quota"]
    assert (await client.get(f"{B}/health", params={"range": "9y"})).status_code == 422


async def test_only_staff_can_read_and_only_a_master_admin_can_change_settings(make_client):
    member = make_client()
    await signup_new_user(member, name="Member")
    assert (await member.get(f"{B}/health")).status_code == 403
    assert (await member.get(f"{B}/issues")).status_code == 403

    staff, _ = await _staff(make_client)
    cfg = (await staff.get(f"{B}/config")).json()
    assert any(m["model"] == "openai/gpt-oss-120b" and m["source"].startswith("entered by owner") for m in cfg["models"])
    body = {"provider": "groq", "model": "openai/gpt-oss-120b", "limits": {"rpd": 500}}
    assert (await staff.put(f"{B}/config", json=body)).status_code == 403
    assert (await staff.put(f"{B}/alerts", json={"email_to": ["a@b.co"]})).status_code == 403

    admin, _ = await _staff(make_client, master=True)
    saved = (await admin.put(f"{B}/config", json=body)).json()
    row = next(m for m in saved["models"] if m["model"] == "openai/gpt-oss-120b")
    assert row["limits"] == {"rpd": 500} and row["source"] == "saved"
    assert (await admin.put(f"{B}/config", json={**body, "warn_pct": 95, "critical_pct": 90})).status_code == 422
    assert (await admin.put(f"{B}/alerts", json={"email_to": ["not-an-email"]})).status_code == 422
    ok = (await admin.put(f"{B}/alerts", json={"email_to": ["Ops@Example.com"], "cooldown_minutes": 30})).json()
    assert ok["rules"]["email_to"] == ["ops@example.com"] and ok["rules"]["cooldown_minutes"] == 30
    restore = {"provider": "groq", "model": "openai/gpt-oss-120b", "limits": {"rpm": 30, "rpd": 1000, "tpm": 8000, "tpd": 200000}}
    await admin.put(f"{B}/config", json=restore)
    await admin.put(f"{B}/alerts", json={"email_to": []})


async def test_sharing_says_plainly_when_a_channel_is_not_set_up(make_client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "SLACK_WEBHOOK_URL", "", raising=False)
    client, _ = await _staff(make_client)
    made = (await client.post(f"{B}/issues", json={"title": "Share me please"})).json()
    res = await client.post(f"{B}/issues/{made['number']}/share", json={"channel": "slack"})
    assert res.status_code == 409 and "Slack is not set up" in res.json()["detail"]
    res = await client.post(f"{B}/issues/{made['number']}/share", json={"channel": "email"})
    assert res.status_code == 409
    summary = (await client.get(f"{B}/status-summary")).json()
    assert summary["text"].startswith("LLM status:")


async def test_auto_fix_sweep_closes_quiet_issues_and_leaves_manual_ones(make_client):
    from datetime import timedelta

    from app.db.mongo import llm_issues

    client, _ = await _staff(make_client)
    model = "m-" + datetime.now().strftime("%H%M%S%f")
    await _fail_and_flush("groq", model, "Rate limit on requests per minute", count=1)
    number = await _issue_number(client, model)
    await llm_issues.update_one({"number": number}, {"$set": {"last_seen": datetime.now(timezone.utc) - timedelta(hours=5)}})
    await issues.auto_fix_sweep()
    assert (await client.get(f"{B}/issues/{number}")).json()["status"] == "fixed"
    manual = (await client.post(f"{B}/issues", json={"title": "Old manual issue"})).json()
    await llm_issues.update_one({"number": manual["number"]}, {"$set": {"last_seen": datetime.now(timezone.utc) - timedelta(days=9)}})
    await issues.auto_fix_sweep()
    assert (await client.get(f"{B}/issues/{manual['number']}")).json()["status"] == "open"


async def test_old_notes_are_copied_into_issues_once_and_the_old_routes_are_gone(make_client):
    from uuid import uuid4

    from app.db.mongo import llm_issues, ops_llm_notes

    client, _ = await _staff(make_client)
    nid = "legacy-" + uuid4().hex[:8]
    await ops_llm_notes.insert_one({
        "_id": nid, "kind": "security", "severity": "high", "title": "Rotate the Groq key", "detail": "Key was pasted in chat",
        "status": "resolved", "created_by": "u1", "created_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
        "resolved_at": datetime(2026, 9, 2, tzinfo=timezone.utc),
    })
    try:
        assert await issues.merge_legacy_notes() >= 1
        assert await issues.merge_legacy_notes() == 0  # a second run copies nothing
        doc = await llm_issues.find_one({"legacy_note_id": nid})
        assert doc["source"] == "manual" and doc["status"] == "fixed" and doc["priority"] == "high" and doc["kind"] == "security"
        assert doc["title"] == "Rotate the Groq key" and doc["notes"][0]["text"] == "Key was pasted in chat"
        shown = (await client.get(f"{B}/issues", params={"q": "rotate the groq key"})).json()
        assert any(i["number"] == doc["number"] and i["title"] == "Rotate the Groq key" for i in shown["items"])
    finally:
        await ops_llm_notes.delete_one({"_id": nid})
        await llm_issues.delete_many({"legacy_note_id": nid})
    for old in ("/api/v1/ops/ai/notes", "/api/v1/ops/ai/health"):
        assert (await client.get(old)).status_code == 404
    assert (await client.post("/api/v1/ops/ai/ping")).status_code in (404, 405)


async def test_audit_trail_storage_and_fallback_status(make_client):
    from app.db.mongo import llm_events
    from app.shared.llm_health.recorder import Attempt, Recorder

    admin, _ = await _staff(make_client, master=True)
    await admin.put(f"{B}/config", json={"provider": "groq", "model": "openai/gpt-oss-120b", "limits": {"rpd": 1000}})
    await admin.put(f"{B}/alerts", json={"email_to": ["ops@example.com"]})
    audit = (await admin.get(f"{B}/audit")).json()["items"]
    actions = [a["action"] for a in audit]
    assert "config.limits" in actions and "config.alerts" in actions
    assert all("at" in a and a["detail"] for a in audit)
    await admin.put(f"{B}/alerts", json={"email_to": []})

    store = (await admin.get(f"{B}/storage")).json()
    assert {c["collection"] for c in store["collections"]} >= {"llm_events", "llm_issues", "llm_audit"}
    assert store["budget_mb"] == 100

    # a failure with the fallback covering for it reads as "working"; without it, "none"
    model = "m-" + datetime.now().strftime("%H%M%S%f")
    rec = Recorder()
    for _ in range(2):
        rec.record(Attempt(provider="groq", model=model, ok=False, latency_ms=50, http_status=503, error_class="InternalServerError", error_message="overloaded", feature="fallback-feature-x"))
    rec.record(Attempt(provider="gemini", model="g", ok=True, latency_ms=400, outcome="fallback_success", fallback_to="gemini:g", feature="fallback-feature-x"))
    await rec.flush()
    number = await _issue_number(admin, model)
    assert (await admin.get(f"{B}/issues/{number}")).json()["fallback_outcome"] == "working"
    await llm_events.delete_many({"outcome": "fallback_success", "feature": "fallback-feature-x"})
    assert (await admin.get(f"{B}/issues/{number}")).json()["fallback_outcome"] == "none"


async def test_a_failed_fallback_opens_a_critical_issue_and_turns_the_banner_red(make_client):
    from app.shared.llm_health.recorder import Attempt, Recorder

    client, _ = await _staff(make_client)
    rec = Recorder()
    feature = "feat-" + datetime.now().strftime("%H%M%S%f")
    rec.record(Attempt(provider="gemini", model="g-fail", ok=False, latency_ms=0, error_class="fallback_failed", error_message="Groq failed and the Gemini fallback failed too.", outcome="failed", count_in_rollup=False, feature=feature))
    await rec.flush()
    data = (await client.get(f"{B}/issues", params={"q": "", "limit": 200})).json()["items"]
    mine = [i for i in data if i["error_type"] == "fallback_failed" and feature in i["features"]]
    assert mine and mine[0]["priority"] == "critical" and mine[0]["title"] == f"Generation is failing for {feature}"
    banner = (await client.get(f"{B}/health")).json()["banner"]
    assert banner["state"] == "not_working"
    await client.patch(f"{B}/issues/{mine[0]['number']}", json={"status": "fixed", "reason": "test cleanup"})


async def test_the_bell_lists_serious_issues_and_lists_every_provider_on_the_health_page(make_client):
    client, _ = await _staff(make_client)
    model = "bell-" + datetime.now().strftime("%H%M%S%f")
    await _fail_and_flush("groq", model, "Rate limit on requests per day (RPD): Limit 1000")
    number = await _issue_number(client, model)
    await client.patch(f"{B}/issues/{number}", json={"priority": "critical", "reason": "test"})
    items = (await client.get(f"{B}/notifications")).json()["items"]
    mine = [n for n in items if n["href"] == f"/ops/llm/issues/{number}"]
    assert mine and mine[0]["id"].startswith("llm-issue-") and "priority" in mine[0]["detail"]
    providers = (await client.get(f"{B}/providers")).json()["providers"]
    assert {"groq", "gemini", "cloudflare", "elevenlabs", "deepgram"} <= set(providers)
    assert providers["cloudflare"]["label"] == "Cloudflare Workers AI" and providers["cloudflare"]["used_for"]
