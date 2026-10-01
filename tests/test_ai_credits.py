"""The AI Credits panel's numbers come from saved data, never a fixed sample."""
from datetime import date, datetime, timedelta, timezone

from app.db.mongo import audio_assets, content_pieces, workspace_ai_budgets, workspace_ai_usage_daily
from app.shared import ai_credits
from tests.test_audio_assets import _h, _setup  # noqa: F401 — reuse the workspace setup


def test_shares_are_whole_numbers_of_what_was_made_and_zero_when_nothing_was():
    assert ai_credits.shares({"a": 3, "b": 1, "c": 0}) == {"a": 75, "b": 25, "c": 0}
    assert ai_credits.shares({"a": 0, "b": 0}) == {"a": 0, "b": 0}
    rows = ai_credits.category_rows({"posts": 1, "voice_audio": 3})
    by = {r["key"]: r for r in rows}
    assert by["posts"]["detail"] == "1 post generated" and by["voice_audio"]["detail"] == "3 clips generated" and by["video"]["detail"] == "0 videos made"
    assert [r["key"] for r in rows][:3] == ["voice_audio", "video", "posts"]


def test_window_facts_report_pace_and_when_the_oldest_usage_drops_off():
    today = date(2026, 10, 30)
    rows = [
        {"date": "2026-10-20", "tokens_used": 3000, "calls": 4},
        {"date": "2026-10-29", "tokens_used": 1000, "calls": 1},
        {"date": "2026-10-25", "tokens_used": 0, "calls": 0},
    ]
    facts = ai_credits.window_facts(rows, today)
    assert facts["tokens"] == 4000 and facts["calls"] == 5 and facts["active_days"] == 2
    assert facts["oldest_drops_off_in_days"] == 20 and round(facts["daily_pace"]) == 364
    none = ai_credits.window_facts([], today)
    assert none["tokens"] == 0 and none["oldest_drops_off_in_days"] is None


def test_the_insight_picks_the_most_useful_true_sentence():
    counts = {"voice_audio": 12, "video": 6, "posts": 34, "images": 0, "insights": 3}
    assert ai_credits.insight(cap=None, tokens=0, pace=0, counts=counts)["text"].startswith("Posts & Threads is where most of your AI work went")
    assert ai_credits.insight(cap=10000, tokens=7600, pace=100, counts=counts)["kind"] == "warn"
    fast = ai_credits.insight(cap=10000, tokens=4000, pace=800, counts=counts)
    assert fast["kind"] == "warn" and "about 7 days" in fast["text"]
    assert "reached your AI cap" in ai_credits.insight(cap=1000, tokens=1000, pace=10, counts=counts)["text"]
    empty = ai_credits.insight(cap=None, tokens=0, pace=0, counts={k: 0 for k in counts})
    assert empty["kind"] == "empty" and "Nothing yet" in empty["text"] and "—" not in empty["text"]


async def test_the_credits_route_counts_what_the_workspace_really_made(signup_user):
    client, _, ws_id, _brand = await _setup(signup_user)
    now = datetime.now(timezone.utc)
    empty = (await client.get("/api/v1/ops/ai/credits", headers=_h(ws_id))).json()
    assert empty["cap"] is None and empty["used_percent"] is None
    assert all(c["count"] == 0 for c in empty["categories"]) and empty["insight"]["kind"] == "empty"

    await workspace_ai_budgets.update_one({"workspace_id": ws_id}, {"$set": {"monthly_token_budget": 10000}, "$setOnInsert": {"id": ws_id, "workspace_id": ws_id}}, upsert=True)
    today = now.date().isoformat()
    await workspace_ai_usage_daily.insert_one({"_id": f"{ws_id}:{today}", "id": f"{ws_id}:{today}", "workspace_id": ws_id, "date": today, "tokens_used": 2500, "calls": 7})
    old = (now - timedelta(days=45)).date().isoformat()
    await workspace_ai_usage_daily.insert_one({"_id": f"{ws_id}:{old}", "id": f"{ws_id}:{old}", "workspace_id": ws_id, "date": old, "tokens_used": 9999, "calls": 9})
    for i in range(3):
        await content_pieces.insert_one({"piece_id": f"cp-{ws_id}-{i}", "workspace_id": ws_id, "created_at": now, "deleted": False, "platform": "LinkedIn", "content": "x"})
    await content_pieces.insert_one({"piece_id": f"cp-{ws_id}-old", "workspace_id": ws_id, "created_at": now - timedelta(days=60), "platform": "LinkedIn", "content": "x"})
    await content_pieces.insert_one({"piece_id": f"cp-{ws_id}-gone", "workspace_id": ws_id, "created_at": now, "deleted": True, "platform": "LinkedIn", "content": "x"})
    await audio_assets.insert_one({"id": f"aa-{ws_id}", "workspace_id": ws_id, "created_at": now, "title": "t",
                                   "video_clips": [{"id": "c1", "created_at": now}, {"id": "c0", "created_at": now - timedelta(days=90)}]})

    body = (await client.get("/api/v1/ops/ai/credits", headers=_h(ws_id))).json()
    by = {c["key"]: c for c in body["categories"]}
    assert body["tokens_used"] == 2500 and body["cap"] == 10000 and body["used_percent"] == 25 and body["calls"] == 7
    assert by["posts"]["count"] == 3 and by["voice_audio"]["count"] == 1 and by["video"]["count"] == 1 and by["images"]["count"] == 0
    assert by["posts"]["share"] == 60 and by["voice_audio"]["share"] == 20
    assert body["oldest_drops_off_in_days"] == 30 and body["insight"]["kind"] in ("info", "warn")
    assert "do not use AI tokens" in body["note"]

    other, _, other_ws, _ = await _setup(signup_user)
    theirs = (await other.get("/api/v1/ops/ai/credits", headers=_h(other_ws))).json()
    assert theirs["tokens_used"] == 0 and all(c["count"] == 0 for c in theirs["categories"])
