"""The morning email about AI problems still open: built from what is open, sent once a day, and only when there is something to say."""
from __future__ import annotations

from datetime import datetime, timezone

from app.db.mongo import get_db
from app.shared.llm_health import alerts, service


def _issue(number: int, priority: str = "high", status: str = "open") -> dict:
    return {"_id": f"digest-test-{number}", "number": number, "priority": priority, "status": status, "error_type": "unknown",
            "provider": "groq", "model": "m", "features": ["writing"], "last_seen": datetime.now(timezone.utc)}


def test_the_digest_lists_what_is_open_what_is_down_and_what_failed():
    body = alerts.digest_body([_issue(1, "high"), _issue(2, "critical"), _issue(3, "low")], {"groq": "down", "gemini": "ok"}, 12, "https://app.example")
    lines = body.splitlines()
    assert lines[0].startswith("3 AI problems are still open (1 critical, 1 high, 1 low).")
    assert lines[1].startswith("  ISSUE-0002") and "(critical)" in lines[1], "the worst come first"
    assert "Not working right now: Groq." in body and "12 AI calls failed in the last 24 hours." in body
    assert body.splitlines()[-1] == "https://app.example/ops/llm"
    one = alerts.digest_body([_issue(5)], {}, 1)
    assert "1 AI problem is still open (1 high)." in one and "1 AI call failed" in one


def test_a_quiet_day_has_no_digest():
    assert alerts.digest_body([], {"groq": "ok"}, 0) is None


async def test_the_digest_is_sent_once_a_day_to_the_set_addresses_and_only_when_asked(monkeypatch):
    db = get_db()
    # whatever other tests saved is put back afterwards, so this test leaves the database exactly as it found it
    saved_issues = await db["llm_issues"].find({}).to_list(None)
    saved_rules = await db["llm_alert_rules"].find_one({"_id": "default"})
    await db["llm_issues"].delete_many({})
    await db["llm_alert_log"].delete_many({"kind": "digest"})
    sent: list[tuple] = []

    async def fake_send(to, subject, body):
        sent.append((tuple(to), subject, body))
        return True

    monkeypatch.setattr(alerts, "_send_email", fake_send)

    async def states():
        return {"groq": {"status": "ok"}}

    monkeypatch.setattr(service, "provider_states", states)

    try:
        # no addresses set: nothing is sent
        await db["llm_alert_rules"].update_one({"_id": "default"}, {"$set": {"email_to": [], "daily_digest": True}}, upsert=True)
        await db["llm_issues"].insert_one(_issue(10))
        assert await service.send_daily_digest() is False and not sent

        await db["llm_alert_rules"].update_one({"_id": "default"}, {"$set": {"email_to": ["ops@example.com"]}}, upsert=True)
        assert await service.send_daily_digest() is True
        assert sent[0][0] == ("ops@example.com",) and "1 AI problem is still open" in sent[0][2]
        # the same day again sends nothing more
        assert await service.send_daily_digest() is False and len(sent) == 1

        # switched off in Alerts: nothing is sent
        await db["llm_alert_log"].delete_many({"kind": "digest"})
        await db["llm_alert_rules"].update_one({"_id": "default"}, {"$set": {"daily_digest": False}}, upsert=True)
        assert await service.send_daily_digest() is False and len(sent) == 1

        # nothing open and nothing down: a quiet day sends nothing
        await db["llm_alert_rules"].update_one({"_id": "default"}, {"$set": {"daily_digest": True}}, upsert=True)
        await db["llm_issues"].delete_many({})
        assert await service.send_daily_digest() is False and len(sent) == 1

    finally:
        # the saved rules and the test issues never outlive this test, so no other test can send to these addresses
        await db["llm_alert_rules"].delete_one({"_id": "default"})
        if saved_rules:
            await db["llm_alert_rules"].insert_one(saved_rules)
        await db["llm_issues"].delete_many({})
        if saved_issues:
            await db["llm_issues"].insert_many(saved_issues)
        await db["llm_alert_log"].delete_many({"kind": "digest"})