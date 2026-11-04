"""The Ops desk: the passive signals (plain code, no model), the active brief (model only on a button press, always answers), and the
shared scratchpad. No test calls a real model."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.core import staff as ST
from app.db.mongo import get_db, users
from app.ops_desk import brief, signals
from tests.conftest import create_workspace


async def _master(signup_user, name="Founder"):
    client, profile = await signup_user(name)
    await users.update_one({"id": profile["id"]}, {"$set": {"is_master_admin": True, "is_platform_staff": True}})
    ST.forget()
    return client, profile


async def test_the_passive_signals_read_the_workspace_with_no_model(signup_user, monkeypatch):
    called = []

    async def boom(*a, **k):
        called.append(1)
        raise AssertionError("the passive half must never call a model")

    monkeypatch.setattr(brief, "call_llm_chat", boom)
    master, _ = await _master(signup_user)
    owner, _ = await signup_user("Owner")
    ws = await create_workspace(owner, f"Desk {uuid4().hex[:6]}")
    await get_db()["workspaces"].update_one({"id": ws}, {"$set": {"billing_status": "limited"}})
    card = await master.get(f"/api/v1/ops/desk/signals/workspace/{ws}")
    assert card.status_code == 200, card.text
    body = card.json()
    levels = [s["level"] for s in body["signals"]]
    assert levels == sorted(levels, key={"problem": 0, "watch": 1, "info": 2, "good": 3}.get), "the worst come first"
    assert any(s["kind"] == "billing" and s["level"] == "problem" for s in body["signals"])
    assert body["facts"]["members"] >= 1 and not called
    assert (await master.get("/api/v1/ops/desk/signals/workspace/nope")).status_code == 404


async def test_a_person_card_lists_their_workspaces(signup_user):
    master, _ = await _master(signup_user)
    owner, profile = await signup_user("Owner")
    ws = await create_workspace(owner, f"Mine {uuid4().hex[:6]}")
    card = (await master.get(f"/api/v1/ops/desk/signals/person/{profile['id']}")).json()
    assert any(w["id"] == ws for w in card["facts"]["workspaces"])
    assert any(s["kind"] == "workspaces" for s in card["signals"])


async def test_only_staff_with_the_workspaces_area_can_use_the_desk(signup_user):
    member, _ = await signup_user("Customer")
    assert (await member.get("/api/v1/ops/desk/signals/workspace/x")).status_code == 403
    assert (await member.post("/api/v1/ops/desk/brief", json={"kind": "workspace", "id": "x"})).status_code == 403
    assert (await member.get("/api/v1/ops/desk/scratchpad")).status_code == 403


async def test_the_brief_uses_the_model_when_it_answers_and_the_plain_signals_when_it_does_not(signup_user, monkeypatch):
    master, _ = await _master(signup_user)
    owner, _ = await signup_user("Owner")
    ws = await create_workspace(owner, f"Brief {uuid4().hex[:6]}")
    await get_db()["workspaces"].update_one({"id": ws}, {"$set": {"billing_status": "past_due"}})

    async def model(**kwargs):
        assert "past" in kwargs["messages"][0]["content"].lower() or "failing" in kwargs["messages"][0]["content"].lower()
        return "The renewal is failing and retrying. Check the payment first, then email the owner."

    monkeypatch.setattr(brief, "call_llm_chat", model)
    ok = (await master.post("/api/v1/ops/desk/brief", json={"kind": "workspace", "id": ws})).json()
    assert ok["source"] == "model" and "renewal" in ok["brief"]

    async def down(**kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(brief, "call_llm_chat", down)
    fallback = await master.post("/api/v1/ops/desk/brief", json={"kind": "workspace", "id": ws})
    assert fallback.status_code == 200 and fallback.json()["source"] == "rules"
    assert "renewal payment is failing" in fallback.json()["brief"], "the button still answers, from the signals"
    # the call is in the staff ledger
    mine = [h for h in (await master.get("/api/v1/ops/team/history", params={"action": "desk"})).json()["history"]]
    assert len(mine) >= 2 and all(h["action"] == "desk.brief" for h in mine)


async def test_a_brief_that_repeats_the_instructions_is_not_shown(signup_user, monkeypatch):
    master, _ = await _master(signup_user)
    owner, _ = await signup_user("Owner")
    ws = await create_workspace(owner, f"Leak {uuid4().hex[:6]}")

    async def leaky(**kwargs):
        return brief.SYSTEM

    monkeypatch.setattr(brief, "call_llm_chat", leaky)
    out = (await master.post("/api/v1/ops/desk/brief", json={"kind": "workspace", "id": ws})).json()
    assert out["source"] == "rules"


async def test_the_scratchpad_saves_with_a_version_and_never_overwrites_a_newer_note(signup_user):
    master, _ = await _master(signup_user)
    other, _ = await _master(signup_user, "Second")
    await get_db()["ops_desk_notes"].delete_one({"_id": "scratchpad"})
    empty = (await master.get("/api/v1/ops/desk/scratchpad")).json()
    assert empty["text"] == "" and empty["version"] == 0
    first = await master.put("/api/v1/ops/desk/scratchpad", json={"text": "Refund pending for Acme", "version": 0})
    assert first.status_code == 200 and first.json()["version"] == 1 and first.json()["updated_by"]
    # the other person still holds version 0: their save is refused, not merged over
    stale = await other.put("/api/v1/ops/desk/scratchpad", json={"text": "Mine", "version": 0})
    assert stale.status_code == 409
    seen = (await other.get("/api/v1/ops/desk/scratchpad")).json()
    assert seen["text"] == "Refund pending for Acme"
    second = await other.put("/api/v1/ops/desk/scratchpad", json={"text": seen["text"] + "\nAcme emailed", "version": seen["version"]})
    assert second.status_code == 200 and second.json()["version"] == 2
    too_long = await master.put("/api/v1/ops/desk/scratchpad", json={"text": "x" * 5000, "version": 2})
    assert too_long.status_code == 422


def test_the_plain_summary_leaves_out_the_good_news_unless_that_is_all_there_is():
    res = {"signals": [{"level": "problem", "kind": "billing", "text": "A renewal is failing."}, {"level": "good", "kind": "activity", "text": "Active."}]}
    assert signals.plain_summary(res) == "A renewal is failing."
    assert signals.plain_summary({"signals": [{"level": "good", "kind": "overall", "text": "Nothing needs attention."}]}) == "Nothing needs attention."
