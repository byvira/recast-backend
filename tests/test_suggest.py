"""Typing suggestions: the member's own words and brand words come back for matching in the browser, one short AI continuation is
asked for only when needed, and every limit and failure ends in "no suggestion", never an error. The model is always a stand-in."""
from datetime import datetime, timezone

import pytest

from app.api.v1 import suggest as suggest_module
from app.core.config import settings
from app.db.mongo import content_sessions, users
from app.pipelines import suggest as engine
from tests.test_image_assets import _h, _setup


@pytest.fixture(autouse=True)
def fresh_state():
    engine._cache.clear()
    suggest_module._recent_calls.clear()
    yield
    engine._cache.clear()
    suggest_module._recent_calls.clear()


@pytest.fixture
def model(monkeypatch):
    """Replaces the language model; records how often it was asked."""
    state = {"calls": 0, "reply": " taught me about money", "error": None}

    async def fake(prompt, model=None, system="", temperature=0.7, max_tokens=2500, reasoning_effort="low"):
        state["calls"] += 1
        if state["error"]:
            raise state["error"]
        return state["reply"]

    monkeypatch.setattr(suggest_module, "call_llm", fake)
    return state


# ── the pure parts ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_reply_becomes_a_suggestion_that_fits_the_typed_words():
    assert engine.clean_completion(" taught me about money", "my parents") == " taught me about money"
    assert engine.clean_completion("taught me about money", "my parents") == " taught me about money"  # a new word, even with no space
    assert engine.clean_completion(", who taught me", "my parents") == ", who taught me"  # punctuation stays attached
    assert engine.clean_completion("my parents taught me", "my parents") == " taught me"  # the repeated words are taken off
    assert engine.clean_completion('"lessons from them"', "Tips for ") == "lessons from them"  # no double space after a space
    assert engine.clean_completion("Next point", "That was one.") == " Next point"


def test_unusable_replies_are_dropped():
    assert engine.clean_completion("", "hello there") is None
    assert engine.clean_completion("I cannot help with that", "hello there") is None
    assert engine.clean_completion("Sorry, as an AI", "hello there") is None
    assert engine.clean_completion("Disorderly conduct is rare", "hello there") == " Disorderly conduct is rare"


def test_a_long_reply_is_cut_to_a_short_suggestion():
    out = engine.clean_completion(" " + " ".join(["word"] * 60), "hello")
    assert len(out.split()) == engine.MAX_SUGGESTION_WORDS


# ── the member's switch ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

async def test_suggestions_are_on_unless_the_member_turns_them_off(signup_user):
    client, profile, ws_id, _ = await _setup(signup_user)
    me = await client.get("/api/v1/users/me", headers=_h(ws_id))
    assert me.json()["prompt_suggestions"] is True
    res = await client.put("/api/v1/users/me", json={"prompt_suggestions": False}, headers=_h(ws_id))
    assert res.status_code == 200 and res.json()["prompt_suggestions"] is False
    assert (await client.get("/api/v1/users/me", headers=_h(ws_id))).json()["prompt_suggestions"] is False


async def test_with_the_switch_off_nothing_is_offered_and_the_model_is_never_asked(signup_user, model):
    client, _, ws_id, _ = await _setup(signup_user)
    await client.put("/api/v1/users/me", json={"prompt_suggestions": False}, headers=_h(ws_id))
    corpus = await client.get("/api/v1/suggest/corpus?kind=post_idea", headers=_h(ws_id))
    assert corpus.json() == {"enabled": False, "items": []}
    done = await client.post("/api/v1/suggest/complete", json={"kind": "post_idea", "text": "my parents taught me"}, headers=_h(ws_id))
    assert done.status_code == 200 and done.json()["completion"] is None and model["calls"] == 0


# ── the member's own words ───────────────────────────────────────────────────────────────────────────────────────────────────────────

async def test_only_the_callers_own_earlier_prompts_come_back(signup_user):
    client, profile, ws_id, _ = await _setup(signup_user)
    me = (await client.get("/api/v1/users/me", headers=_h(ws_id))).json()
    now = datetime.now(timezone.utc)
    base = {"workspace_id": ws_id, "source_type": "topic", "created_at": now}
    await content_sessions.insert_many([
        {**base, "session_id": "s-mine", "user_id": me["id"], "input_text": "Lessons from my parents about money"},
        {**base, "session_id": "s-theirs", "user_id": "someone-else", "input_text": "A teammate's private idea"},
        {**base, "session_id": "s-url", "user_id": me["id"], "source_type": "url", "input_text": "Long article text pasted from a link"},
    ])
    res = await client.get("/api/v1/suggest/corpus?kind=post_idea", headers=_h(ws_id))
    texts = [item["text"] for item in res.json()["items"]]
    assert "Lessons from my parents about money" in texts
    assert "A teammate's private idea" not in texts and "Long article text pasted from a link" not in texts
    assert all(item["source"] in ("history", "brand") for item in res.json()["items"])


async def test_the_brands_own_words_are_offered(signup_user):
    from app.db.mongo import brand_profiles

    client, _, ws_id, brand_id = await _setup(signup_user)
    await brand_profiles.update_one({"id": brand_id}, {"$set": {"identity.niche": "Parenting and family finance", "identity.content_pillars": ["Raising money-smart kids"]}})
    res = await client.get(f"/api/v1/suggest/corpus?kind=image_prompt&brand_id={brand_id}", headers=_h(ws_id))
    brand_items = [item["text"] for item in res.json()["items"] if item["source"] == "brand"]
    assert "Parenting and family finance" in brand_items and "Raising money-smart kids" in brand_items


async def test_an_unknown_kind_is_refused(signup_user):
    client, _, ws_id, _ = await _setup(signup_user)
    assert (await client.get("/api/v1/suggest/corpus?kind=bogus", headers=_h(ws_id))).status_code == 422


# ── the AI continuation ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

async def test_a_continuation_is_made_once_and_then_remembered(signup_user, model):
    client, _, ws_id, _ = await _setup(signup_user)
    body = {"kind": "post_idea", "text": "my parents"}
    first = await client.post("/api/v1/suggest/complete", json=body, headers=_h(ws_id))
    assert first.status_code == 200 and first.json() == {"completion": " taught me about money", "source": "ai"}
    second = await client.post("/api/v1/suggest/complete", json={**body, "text": "My  parents "}, headers=_h(ws_id))
    assert second.json()["completion"] is not None
    assert model["calls"] == 1, "the same words must not be asked about twice"


async def test_very_short_text_is_not_sent_to_the_model(signup_user, model):
    client, _, ws_id, _ = await _setup(signup_user)
    res = await client.post("/api/v1/suggest/complete", json={"kind": "post_idea", "text": "my"}, headers=_h(ws_id))
    assert res.json()["completion"] is None and model["calls"] == 0


async def test_a_failing_or_slow_model_gives_no_suggestion_and_no_error(signup_user, model):
    client, _, ws_id, _ = await _setup(signup_user)
    model["error"] = RuntimeError("provider down")
    res = await client.post("/api/v1/suggest/complete", json={"kind": "script", "text": "Welcome to the show, today we"}, headers=_h(ws_id))
    assert res.status_code == 200 and res.json()["completion"] is None


async def test_the_daily_allowance_ends_ai_suggestions_but_not_the_page(signup_user, model, monkeypatch):
    client, _, ws_id, _ = await _setup(signup_user)
    monkeypatch.setattr(settings, "SUGGEST_DAILY_CAP", 2)
    results = []
    for i in range(4):
        res = await client.post("/api/v1/suggest/complete", json={"kind": "post_idea", "text": f"a different idea number {i}"}, headers=_h(ws_id))
        results.append(res.json()["completion"])
        assert res.status_code == 200
    assert results[:2] != [None, None] and results[2:] == [None, None]
    assert model["calls"] == 2


async def test_the_whole_app_stays_inside_its_per_minute_allowance(signup_user, model, monkeypatch):
    client, _, ws_id, _ = await _setup(signup_user)
    monkeypatch.setattr(settings, "SUGGEST_APP_PER_MINUTE", 1)
    one = await client.post("/api/v1/suggest/complete", json={"kind": "post_idea", "text": "first different idea"}, headers=_h(ws_id))
    two = await client.post("/api/v1/suggest/complete", json={"kind": "post_idea", "text": "second different idea"}, headers=_h(ws_id))
    assert one.json()["completion"] is not None and two.json()["completion"] is None and model["calls"] == 1


async def test_a_spent_monthly_budget_gives_no_suggestion(signup_user, model, monkeypatch):
    from fastapi import HTTPException

    async def spent(workspace_id):
        raise HTTPException(status_code=403, detail="budget used")

    import app.agents.supervisor.service as service

    monkeypatch.setattr(service, "assert_ai_budget_available", spent)
    client, _, ws_id, _ = await _setup(signup_user)
    res = await client.post("/api/v1/suggest/complete", json={"kind": "post_idea", "text": "my parents taught me"}, headers=_h(ws_id))
    assert res.status_code == 200 and res.json()["completion"] is None and model["calls"] == 0
