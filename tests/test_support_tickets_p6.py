"""Support tickets, Phase 6 and the Support page: ratings, metrics, the AI
reply draft and diagnosis, the guides, the assistant with history, live
status and health alerts. Every AI call is mocked, so no provider quota is used."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.api.v1 import support_assistant
from app.db.mongo import (
    support_ai_usage,
    support_chats,
    support_email_log,
    support_incidents,
    support_settings,
    support_tickets,
    workspace_connections,
    workspaces,
)
from app.shared import support_ai, support_guides
from app.shared import support_rules as rules
from app.shared.support_context import enrich_ticket
from app.workers.support_lifecycle import run_support_lifecycle
from tests.test_support_tickets import _file, _member, _staff, support_tickets_doc

OPS = "/api/v1/ops/support"


@pytest.fixture(autouse=True)
def _no_background_work(monkeypatch):
    import app.api.v1.support as member_routes

    monkeypatch.setattr(member_routes, "spawn", lambda coro: coro.close())


@pytest.fixture
def fake_draft(monkeypatch):
    """Replaces the model call. Records the prompt so tests can inspect what was sent."""
    seen: dict = {"prompts": [], "reply": "Hi Member, thanks for telling us. We are looking into it."}

    async def fake(prompt, **kwargs):
        seen["prompts"].append(prompt)
        if isinstance(seen["reply"], Exception):
            raise seen["reply"]
        return seen["reply"]

    monkeypatch.setattr(support_ai, "call_llm", fake)
    return seen


# ── Ratings ──────────────────────────────────────────────────────────────────
async def test_member_can_rate_only_a_finished_ticket_and_change_it(make_client):
    member, _ = await _member(make_client)
    staff, _ = await _staff(make_client)
    tid = (await _file(member))["id"]

    early = await member.post(f"/api/v1/support/tickets/{tid}/rating", json={"value": "up"})
    assert early.status_code == 409
    await staff.patch(f"{OPS}/tickets/{tid}", json={"status": "resolved"})

    up = await member.post(f"/api/v1/support/tickets/{tid}/rating", json={"value": "up", "comment": "Quick, thanks"})
    assert up.status_code == 200 and up.json()["rating"]["value"] == "up"
    down = await member.post(f"/api/v1/support/tickets/{tid}/rating", json={"value": "down"})
    assert down.json()["rating"]["value"] == "down"
    assert (await member.post(f"/api/v1/support/tickets/{tid}/rating", json={"value": "meh"})).status_code == 422

    other, _ = await _member(make_client)
    assert (await other.post(f"/api/v1/support/tickets/{tid}/rating", json={"value": "up"})).status_code == 404
    assert "rated" in [e["type"] for e in (await staff.get(f"{OPS}/tickets/{tid}")).json()["events"]]


# ── Guides ───────────────────────────────────────────────────────────────────
async def test_guides_are_served_and_are_plain(make_client):
    member, _ = await _member(make_client)
    res = (await member.get("/api/v1/support/guides")).json()
    assert len(res["guides"]) >= 30
    topic_ids = {t["id"] for t in res["topics"]}
    assert all(g["topic"] in topic_ids for g in res["guides"])
    ids = [g["id"] for g in res["guides"]]
    assert len(ids) == len(set(ids))
    # Product copy rules: no em dashes anywhere, and every guide has something to say.
    for g in res["guides"]:
        text = f"{g['title']} {g['summary']} {g['body']}"
        assert "—" not in text and "–" not in text
        assert g["title"] and g["summary"] and len(g["body"]) > 40
    # Every topic has enough guides to fill a section.
    for t in res["topics"]:
        assert sum(1 for g in res["guides"] if g["topic"] == t["id"]) >= 2


def test_guide_search_finds_relevant_guides():
    top = [g["id"] for g in support_guides.search("my linkedin follower count is zero", limit=3)]
    assert "linkedin-followers" in top
    assert support_guides.search("zzzz qqqq") == []


# ── Diagnosis and AI draft ───────────────────────────────────────────────────
async def test_diagnosis_is_plain_code_from_the_snapshot(make_client):
    member, _ = await _member(make_client)
    staff, _ = await _staff(make_client)
    tid = (await _file(member, category="Publishing", source_context={"type": "platform", "id": "linkedin"}))["id"]
    t = await support_tickets_doc(tid)
    await workspace_connections.insert_one(
        {"id": "c1", "workspace_id": t["workspace_id"], "platform": "linkedin", "username": "acme",
         "is_active": True, "expires_at": datetime(2020, 1, 1, tzinfo=timezone.utc)}
    )
    await workspaces.update_one({"id": t["workspace_id"]}, {"$set": {"generation_halted": True}})
    await enrich_ticket(tid)

    detail = (await staff.get(f"{OPS}/tickets/{tid}")).json()
    texts = [d["text"] for d in detail["diagnosis"]]
    assert any("Linkedin connection needs to be reconnected" in x and "reported" in x for x in texts)
    assert any("paused" in x for x in texts)
    assert any(d["level"] == "problem" for d in detail["diagnosis"])


async def test_draft_is_text_only_grounded_and_redacted(make_client, fake_draft):
    member, _ = await _member(make_client)
    staff, _ = await _staff(make_client)
    tid = (await _file(
        member, subject="LinkedIn follower count is 0",
        description="Please email me at secret.person@example.com or call +1 415 555 0134. my password: hunter2hunter2",
    ))["id"]
    await enrich_ticket(tid)
    before = await support_tickets_doc(tid)

    res = await staff.post(f"{OPS}/tickets/{tid}/ai-draft")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["draft"].startswith("Hi Member") and body["prompt_version"] == "support/draft_reply@1"
    assert any(g["id"] == "linkedin-followers" for g in body["guides"])

    prompt = fake_draft["prompts"][0]
    # The ticket text is fenced off as untrusted, and personal data never reaches the model.
    assert "<ticket_data>" in prompt and "untrusted" in prompt
    assert "secret.person@example.com" not in prompt and "555 0134" not in prompt and "hunter2" not in prompt
    assert "[email]" in prompt and "[phone]" in prompt
    # Guides the model may quote are in the prompt.
    assert "follower counts" in prompt

    # It changed nothing: no message, no status, no notification to the member.
    after = await support_tickets_doc(tid)
    assert len(after["messages"]) == len(before["messages"]) and after["status"] == before["status"]
    assert (await member.get("/api/v1/support/notifications")).json()["unread"] == 1  # only the receipt
    assert await support_ai_usage.count_documents({"ticket_id": tid, "kind": "draft", "ok": True}) == 1


async def test_draft_output_is_cleaned(make_client, fake_draft):
    member, _ = await _member(make_client)
    staff, _ = await _staff(make_client)
    tid = (await _file(member))["id"]
    fake_draft["reply"] = "Hi Member — we fixed it, see https://evil.example/x for details."
    draft = (await staff.post(f"{OPS}/tickets/{tid}/ai-draft")).json()["draft"]
    assert "—" not in draft and "http" not in draft


async def test_draft_needs_staff_and_fails_softly(make_client, fake_draft):
    member, _ = await _member(make_client)
    staff, _ = await _staff(make_client)
    tid = (await _file(member))["id"]
    assert (await member.post(f"{OPS}/tickets/{tid}/ai-draft")).status_code == 403

    fake_draft["reply"] = RuntimeError("provider down")
    down = await staff.post(f"{OPS}/tickets/{tid}/ai-draft")
    assert down.status_code == 503 and "by hand" in down.json()["detail"]
    fake_draft["reply"] = "   "
    assert (await staff.post(f"{OPS}/tickets/{tid}/ai-draft")).status_code == 502
    # The ticket is untouched and the reply box still works.
    assert (await staff.post(f"{OPS}/tickets/{tid}/messages", json={"text": "By hand"})).status_code == 200


async def test_draft_caps_per_staff_and_per_day(make_client, fake_draft, monkeypatch):
    member, _ = await _member(make_client)
    staff, staff_user = await _staff(make_client)
    tid = (await _file(member))["id"]
    monkeypatch.setattr(support_ai, "CALLS_PER_STAFF_PER_HOUR", 2)
    assert (await staff.post(f"{OPS}/tickets/{tid}/ai-draft")).status_code == 200
    assert (await staff.post(f"{OPS}/tickets/{tid}/ai-draft")).status_code == 200
    assert (await staff.post(f"{OPS}/tickets/{tid}/ai-draft")).status_code == 429

    other, _ = await _staff(make_client)
    monkeypatch.setattr(support_ai, "CALLS_PER_DAY", 1)
    assert (await other.post(f"{OPS}/tickets/{tid}/ai-draft")).status_code == 429
    await support_ai_usage.delete_many({})


def test_redaction_masks_personal_data_and_secrets():
    text = "Mail a.b+c@example.co.uk, phone (415) 555-0134, key sk-abcdefghij1234567890, Bearer abcdefghijklmnop, token=xyz123"
    out = support_ai.redact(text)
    for leaked in ("a.b+c@example.co.uk", "555-0134", "sk-abcdefghij", "Bearer abcdefghijklmnop", "xyz123"):
        assert leaked not in out
    assert support_ai.redact("The post failed 3 times") == "The post failed 3 times"


async def test_ai_category_fallback_is_off_by_default_and_only_accepts_listed_areas(make_client, monkeypatch):
    assert rules.AI_CATEGORY_FALLBACK_ENABLED is False

    async def fake(prompt, **kwargs):
        return "Billing."

    monkeypatch.setattr(support_ai, "call_llm", fake)
    member, _ = await _member(make_client)
    tid = (await _file(member, subject="Hello", description="Just a question"))["id"]
    assert await support_ai.ai_category(await support_tickets_doc(tid)) == "Billing"

    async def rogue(prompt, **kwargs):
        return "Delete everything"

    monkeypatch.setattr(support_ai, "call_llm", rogue)
    assert await support_ai.ai_category(await support_tickets_doc(tid)) is None


# ── The assistant with memory ────────────────────────────────────────────────
@pytest.fixture
def fake_chat(monkeypatch):
    seen: dict = {"calls": []}

    async def fake(messages, system="", **kwargs):
        seen["calls"].append({"messages": messages, "system": system})
        return f"Answer number {len(seen['calls'])}."

    monkeypatch.setattr(support_assistant, "call_llm_chat", fake)
    return seen


async def test_assistant_remembers_the_conversation_and_is_grounded(make_client, fake_chat):
    member, _ = await _member(make_client)
    first = await member.post("/api/v1/support/assist", json={"message": "Why is my linkedin follower count zero?"})
    assert first.status_code == 200, first.text
    chat_id = first.json()["chat_id"]
    assert first.json()["reply"] == "Answer number 1." and first.json()["title"].startswith("Why is my linkedin")
    assert any(g["id"] == "linkedin-followers" for g in first.json()["guides"])

    second = await member.post("/api/v1/support/assist", json={"message": "And can I fix it?", "chat_id": chat_id})
    assert second.json()["chat_id"] == chat_id
    sent = fake_chat["calls"][1]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]  # the earlier turns came back
    assert sent[1]["content"] == "Answer number 1."
    # It is told to use guides and workspace facts, and not to invent.
    assert "Never invent" in fake_chat["calls"][0]["system"]
    assert "follower counts" in fake_chat["calls"][0]["system"]
    assert "Facts about this member's workspace" in fake_chat["calls"][0]["system"]


async def test_chat_history_open_and_delete_are_private(make_client, fake_chat):
    a, _ = await _member(make_client)
    b, _ = await _member(make_client)
    chat_id = (await a.post("/api/v1/support/assist", json={"message": "How do I invite a teammate?"})).json()["chat_id"]
    await a.post("/api/v1/support/assist", json={"message": "New topic please"})  # a second, separate chat

    listed = (await a.get("/api/v1/support/chats")).json()["chats"]
    assert len(listed) == 2 and listed[0]["title"] == "New topic please"  # newest first
    opened = (await a.get(f"/api/v1/support/chats/{chat_id}")).json()
    assert [m["role"] for m in opened["messages"]] == ["user", "assistant"]

    assert (await b.get("/api/v1/support/chats")).json()["chats"] == []
    assert (await b.get(f"/api/v1/support/chats/{chat_id}")).status_code == 404
    assert (await b.delete(f"/api/v1/support/chats/{chat_id}")).status_code == 404
    assert (await b.post("/api/v1/support/assist", json={"message": "hi", "chat_id": chat_id})).status_code == 404

    assert (await a.delete(f"/api/v1/support/chats/{chat_id}")).json() == {"deleted": True}
    assert len((await a.get("/api/v1/support/chats")).json()["chats"]) == 1


async def test_chat_limits_and_soft_failure(make_client, fake_chat, monkeypatch):
    member, _ = await _member(make_client)
    monkeypatch.setattr(support_assistant, "MAX_MESSAGES_PER_CHAT", 2)
    chat_id = (await member.post("/api/v1/support/assist", json={"message": "one"})).json()["chat_id"]
    full = await member.post("/api/v1/support/assist", json={"message": "two", "chat_id": chat_id})
    assert full.status_code == 409 and "Start a new chat" in full.json()["detail"]

    async def boom(messages, system="", **kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr(support_assistant, "call_llm_chat", boom)
    failed = await member.post("/api/v1/support/assist", json={"message": "hello"})
    assert failed.status_code == 503 and "file a ticket" in failed.json()["detail"]
    # Nothing half-saved from the failed turn.
    assert await support_chats.count_documents({"title": "hello"}) == 0


# ── Live status ──────────────────────────────────────────────────────────────
async def test_live_status_shows_real_checks_and_open_incidents(make_client):
    member, _ = await _member(make_client)
    status = (await member.get("/api/v1/support/status")).json()
    by_id = {c["id"]: c for c in status["components"]}
    assert by_id["app"]["state"] == "ok" and by_id["database"]["state"] == "ok"
    assert by_id["accounts"]["accounts"] == []
    assert "uptime" not in str(status).lower()  # nothing invented

    # A connection that needs reconnecting, and an open incident.
    made = await _file(member)
    ws = (await support_tickets_doc(made["id"]))["workspace_id"]
    await workspace_connections.insert_one(
        {"id": str(uuid4()), "workspace_id": ws, "platform": "linkedin", "is_active": True,
         "expires_at": datetime(2020, 1, 1, tzinfo=timezone.utc)}
    )
    await support_incidents.insert_one(
        {"id": str(uuid4()), "title": "Publishing on linkedin: several members are affected", "category": "Publishing",
         "platform": "linkedin", "status": "open", "ticket_ids": [], "created_at": datetime.now(timezone.utc)}
    )
    status = (await member.get("/api/v1/support/status")).json()
    by_id = {c["id"]: c for c in status["components"]}
    assert by_id["accounts"]["state"] == "degraded" and by_id["accounts"]["accounts"][0]["platform"] == "linkedin"
    assert any("linkedin" in i["title"] for i in status["incidents"])
    assert status["overall"] == "degraded"
    await support_incidents.delete_many({})


# ── Metrics and alerts ───────────────────────────────────────────────────────
async def test_metrics_report_real_numbers_and_say_null_when_empty(make_client):
    staff, _ = await _staff(make_client, "lead")
    member, _ = await _member(make_client)
    tid = (await _file(member, category="Billing"))["id"]
    await staff.post(f"{OPS}/tickets/{tid}/messages", json={"text": "Hello", "set_status": "resolved"})
    await member.post(f"/api/v1/support/tickets/{tid}/rating", json={"value": "up"})
    await member.post(f"/api/v1/support/tickets/{tid}/messages", json={"text": "Actually still broken"})  # reopen
    # A second ticket that stays resolved, so the window has something to divide by.
    kept = (await _file(member, subject="Kept resolved", category="Question"))["id"]
    await staff.post(f"{OPS}/tickets/{kept}/messages", json={"text": "Done", "set_status": "resolved"})

    m = (await staff.get(f"{OPS}/metrics", params={"days": 7})).json()
    assert m["volume"]["total"] >= 1 and m["volume"]["by_category"]["Billing"] >= 1
    assert m["first_response_seconds"]["count"] >= 1 and m["first_response_seconds"]["median"] is not None
    assert m["reopen_rate"] is not None and m["reopen_rate"] > 0
    assert m["rating"]["up"] >= 1 and m["rating"]["up_share"] is not None
    assert isinstance(m["per_agent"], list) and isinstance(m["alerts"], list)
    assert (await member.get(f"{OPS}/metrics")).status_code == 403

    empty = (await staff.get(f"{OPS}/metrics", params={"days": 1}))
    assert empty.status_code == 200
    # A window with no resolved tickets in it has no reopen rate rather than a fake zero.
    from app.shared import support_metrics

    far = datetime.now(timezone.utc) - timedelta(days=400)
    nothing = await support_metrics.compute(far, far + timedelta(days=1))
    assert nothing["volume"]["total"] == 0 and nothing["reopen_rate"] is None
    assert nothing["first_response_seconds"]["median"] is None and nothing["rating"]["up_share"] is None


async def test_health_alerts_fire_once_a_day_and_email_failures_are_counted(make_client):
    lead, _ = await _staff(make_client, "lead")
    await lead.post(f"{OPS}/notifications/read")
    await support_email_log.delete_many({})
    now = datetime.now(timezone.utc)
    await support_email_log.insert_many([{"at": now, "type": "staff_reply", "ok": False} for _ in range(4)])
    await support_email_log.insert_one({"at": now, "type": "staff_reply", "ok": True})
    await support_settings.delete_many({"_id": {"$regex": "^alert:"}})

    first = await run_support_lifecycle(now + timedelta(seconds=1))
    assert first["alerts"] >= 1
    types = [n["type"] for n in (await lead.get(f"{OPS}/notifications")).json()["notifications"]]
    assert "health_alert" in types
    # Same day, same alert: not repeated.
    again = await run_support_lifecycle(now + timedelta(seconds=2))
    assert again["alerts"] == 0

    m = (await lead.get(f"{OPS}/metrics")).json()
    assert m["email"]["failed"] >= 4 and m["email"]["failure_rate"] is not None
    assert any(a["key"] == "email_failures" for a in m["alerts"])
    await support_email_log.delete_many({})
    await support_settings.delete_many({"_id": {"$regex": "^alert:"}})
