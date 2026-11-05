"""The levels of authority for the model's reading: where every kind starts, what each level does to a finding, the floor, the switch, how a
kind earns a step up, and how one loses a step by itself. No real model is called."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.content_guard import agent as guard_agent
from app.agents.content_guard import config as guard_config
from app.agents.content_guard import judges, ladder
from app.agents.content_guard.agent import check_piece_before_send, guard_piece_doc, review_text
from app.agents.content_guard.rules import ScreenResult
from app.core import staff as ST
from app.core.config import settings
from app.db.mongo import content_pieces, content_safety, get_db, safety_events, users

CALM = "A calm note about the roadmap and what we ship next"


@pytest.fixture(autouse=True)
async def clean(monkeypatch):
    monkeypatch.setattr(settings, "CONTENT_GUARD_LIVE_CHECKS", True)
    cols = (content_safety, safety_events, ladder._col(), ladder._history(), ladder._decisions(), judges.log_col(), get_db()["safety_appeals"])
    for c in cols:
        await c.delete_many({})
    guard_config.reset_cache()
    ladder.reset_cache()
    guard_agent._model_verdicts.clear()
    await guard_config.save({"model_check": "publish", "rewrite_flagged": False}, expected_version=0, user_id="u")
    yield
    for c in cols:
        await c.delete_many({})
    ladder.reset_cache()


def _unsafe(*cats):
    return AsyncMock(return_value=ScreenResult(ok=False, categories=list(cats), matches=["reason"]))


async def _set(kind, level, version=None):
    snap = await ladder.snapshot()
    await ladder.save(levels={kind: level}, enabled=None, reason="Testing a level", expected_version=snap["version"] if version is None else version, user_id="u1", user_name="Tester")


# ---- the levels --------------------------------------------------------------------------------------------------------------------
async def test_every_kind_starts_at_fix_which_is_what_always_happened():
    snap = await ladder.snapshot()
    assert snap["enabled"] and {k["level"] for k in snap["kinds"]} == {3}
    floors = {k["kind"]: k["floor"] for k in snap["kinds"]}
    assert floors["hate"] == floors["violence"] == floors["self_harm"] == floors["sexual"] == 3 and floors["claims"] == 1


async def test_a_change_needs_a_reason_and_respects_the_floor():
    snap = await ladder.snapshot()
    with pytest.raises(ladder.LadderError, match="few words"):
        await ladder.save(levels={"claims": 2}, enabled=None, reason="", expected_version=snap["version"], user_id="u", user_name=None)
    with pytest.raises(ladder.LadderError, match="cannot go below Fix"):
        await ladder.save(levels={"hate": 1}, enabled=None, reason="Too many holds", expected_version=snap["version"], user_id="u", user_name=None)
    with pytest.raises(ladder.LadderError, match="not a kind"):
        await ladder.save(levels={"nonsense": 2}, enabled=None, reason="A reason", expected_version=snap["version"], user_id="u", user_name=None)
    with pytest.raises(ladder.LadderError, match="Watch, Advise"):
        await ladder.save(levels={"claims": 9}, enabled=None, reason="A reason", expected_version=snap["version"], user_id="u", user_name=None)
    with pytest.raises(ladder.LadderError, match="Nothing was changed"):
        await ladder.save(levels={"claims": 3}, enabled=None, reason="A reason", expected_version=snap["version"], user_id="u", user_name=None)
    await _set("claims", 2)
    assert ladder.current_level("claims") == 2
    rows = await ladder.history()
    assert rows[0]["changes"] == [{"path": "claims", "label": "Claims that cannot be backed up", "before": "Fix", "after": "Advise"}]
    with pytest.raises(ladder.LadderError, match="changed while"):
        await ladder.save(levels={"claims": 1}, enabled=None, reason="Stale", expected_version=0, user_id="u", user_name=None)


async def test_the_switch_puts_everything_back_to_the_starting_levels():
    await _set("claims", 1)
    snap = await ladder.snapshot()
    await ladder.save(levels={}, enabled=False, reason="Something looks wrong", expected_version=snap["version"], user_id="u", user_name=None)
    assert ladder.current_level("claims") == 3, "off means the starting level"
    snap = await ladder.snapshot()
    await ladder.save(levels={}, enabled=True, reason="Looks fine again", expected_version=snap["version"], user_id="u", user_name=None)
    assert ladder.current_level("claims") == 1, "the levels set before were kept"


# ---- what each level does ------------------------------------------------------------------------------------------------------------
async def test_fix_is_what_has_always_happened_a_finding_holds_the_post():
    with patch.object(guard_agent, "_model_screen", _unsafe("claims")):
        result = await review_text(CALM, where="generation", stage="publish")
    assert result.blocked and result.categories == ["claims"]


async def test_watch_records_the_finding_and_lets_the_post_go_on():
    await _set("claims", 1)
    with patch.object(guard_agent, "_model_screen", _unsafe("claims")):
        result = await review_text(CALM, where="generation", stage="publish", platform="LinkedIn")
    assert not result.blocked and result.notes == []
    row = await ladder._decisions().find_one({})
    assert row["action"] == "watched" and row["categories"] == ["claims"] and row["level"] == 1 and row["platform"] == "LinkedIn"
    assert await safety_events.count_documents({}) == 0, "nothing was held, so nothing is listed as caught"


async def test_advise_lets_it_go_on_with_a_note_the_member_sees():
    await _set("claims", 2)
    with patch.object(guard_agent, "_model_screen", _unsafe("claims")):
        result = await review_text(CALM, where="generation", stage="publish")
        doc = await guard_piece_doc({"content": "We guarantee you will love our roadmap", "workspace_id": "w1", "piece_id": "p-ladder", "platform": "LinkedIn", "language": "en"}, where="generation")
    assert not result.blocked and result.notes == ["claims that cannot be backed up"]
    assert doc.get("flagged_for_review") is None and "claims that cannot be backed up" in doc["safety_note"]["message"]


async def test_hold_does_not_try_a_rewrite_first():
    await guard_config.save({"rewrite_flagged": True}, expected_version=guard_config.current()["version"], user_id="u")
    await _set("claims", 4)
    boom = AsyncMock(side_effect=AssertionError("no rewrite at the Hold level"))
    with patch.object(guard_agent, "_model_screen", _unsafe("claims")), patch("app.shared.llm.call_llm", new=boom):
        result = await review_text(CALM, where="generation", stage="publish")
    assert result.blocked and not boom.called


async def test_the_strictest_level_among_the_kinds_found_applies():
    await _set("claims", 1)
    assert await ladder.level_for(["claims", "hate"]) == 3, "hate cannot go below Fix, so the finding is held"
    assert await ladder.level_for(["claims"]) == 1 and await ladder.level_for([]) == 3


async def test_a_serious_kind_is_held_whatever_is_saved_on_the_ladder():
    ladder.set_cache({"hate": 1}, True, 1)  # even a document edited by hand
    assert ladder.current_level("hate") == 3
    with patch.object(guard_agent, "_model_screen", _unsafe("hate")):
        assert (await review_text(CALM, where="generation", stage="publish")).blocked


async def test_the_rules_are_never_affected():
    await _set("claims", 1)
    result = await review_text("Guaranteed returns of 40% every month", where="generation")
    assert result.blocked and "claims" in result.categories, "the free rules still hold what they always held"


async def test_before_sending_a_watched_finding_does_not_stop_the_post():
    await _set("claims", 1)
    await content_pieces.insert_one({"piece_id": "p-send", "workspace_id": "w1", "content": CALM, "platform": "Blog"})
    piece = {"piece_id": "p-send", "workspace_id": "w1", "content": CALM, "platform": "Blog"}
    try:
        with patch.object(guard_agent, "_model_screen", _unsafe("claims")):
            await check_piece_before_send(piece, "w1")
    finally:
        await content_pieces.delete_many({"piece_id": "p-send"})
    assert piece["safety_check"]["ok"] is True and piece["safety_check"]["noted"] == ["claims"]
    assert await safety_events.count_documents({}) == 0


# ---- earning a step up and losing one --------------------------------------------------------------------------------------------------
async def _compared(n, agree=True):
    now = datetime.now(timezone.utc)
    await judges.log_col().insert_many([{"at": now, "primary": "fast", "primary_ok": True, "primary_categories": [], "shadow": "safeguard", "shadow_ok": agree,
                                         "shadow_categories": [], "language": "hi", "agree": agree, "error": None, "excerpt": "x"} for _ in range(n)])


async def _appeals(kind, released, upheld):
    now = datetime.now(timezone.utc)
    rows = [{"id": f"a{i}", "status": "released", "source": "model", "categories": [kind], "created_at": now} for i in range(released)]
    rows += [{"id": f"b{i}", "status": "upheld", "source": "model", "categories": [kind], "created_at": now} for i in range(upheld)]
    if rows:
        await get_db()["safety_appeals"].insert_many(rows)


async def test_not_ready_until_enough_posts_were_compared_and_the_models_agree():
    ready = (await ladder.readiness())["claims"]
    assert ready["ready"] is False and "Needs 20 compared posts, has 0" in ready["reasons"][0]
    await _compared(20, agree=False)
    assert "agree 0.0%" in (await ladder.readiness())["claims"]["reasons"][0]


async def test_ready_when_they_agree_and_staff_rarely_release():
    await _compared(25)
    await _appeals("claims", released=1, upheld=9)
    r = (await ladder.readiness())["claims"]
    assert r["ready"] is True and r["released_share"] == 10.0 and r["reasons"] == []
    await _appeals("hate", released=4, upheld=2)
    h = (await ladder.readiness())["hate"]
    assert h["ready"] is False and "released 66.7% of 6" in h["reasons"][0]


async def test_a_kind_staff_keep_releasing_loses_a_step_by_itself():
    await _appeals("claims", released=8, upheld=2)
    await _appeals("hate", released=9, upheld=1)
    moved = await ladder.demote_where_released()
    assert moved == ["claims"] and ladder.current_level("claims") == 2
    assert ladder.current_level("hate") == 3, "the floor holds even when staff release most of what it held"
    last = (await ladder.history())[0]
    assert last["automatic"] is True and last["by_name"] == "Automatic" and "released 80.0% of 10" in last["reason"]
    assert await ladder.demote_where_released() == ["claims"], "and again the next time while it keeps being wrong"
    assert ladder.current_level("claims") == 1
    assert await ladder.demote_where_released() == [], "never below Watch"


async def test_nothing_is_demoted_while_the_switch_is_off():
    await _appeals("claims", released=8, upheld=2)
    snap = await ladder.snapshot()
    await ladder.save(levels={}, enabled=False, reason="Switched off", expected_version=snap["version"], user_id="u", user_name=None)
    assert await ladder.demote_where_released() == []


# ---- who may use it -------------------------------------------------------------------------------------------------------------------
async def test_staff_look_and_only_those_who_approve_change(signup_user):
    await ST.ensure_templates()
    analyst, ap = await signup_user("Analyst")
    await users.update_one({"id": ap["id"]}, {"$set": {"is_platform_staff": True, "staff": {"level": "staff", "role_id": "quality_analyst", "status": "active"}}})
    master, mp = await signup_user("Founder")
    await users.update_one({"id": mp["id"]}, {"$set": {"is_master_admin": True, "is_platform_staff": True}})
    ST.forget()
    snap = (await analyst.get("/api/v1/ops/content-safety/ladder")).json()
    assert snap["version"] == 0 and len(snap["kinds"]) >= 8
    body = {"levels": {"claims": 2}, "reason": "Trying advise", "version": 0}
    assert (await analyst.put("/api/v1/ops/content-safety/ladder", json=body)).status_code == 403
    done = await master.put("/api/v1/ops/content-safety/ladder", json=body)
    assert done.status_code == 200 and next(k for k in done.json()["kinds"] if k["kind"] == "claims")["level"] == 2
    assert (await master.put("/api/v1/ops/content-safety/ladder", json={**body, "levels": {"hate": 1}, "version": 1})).status_code == 422
    assert (await master.put("/api/v1/ops/content-safety/ladder", json={**body, "version": 0})).status_code == 409
    assert (await master.get("/api/v1/ops/content-safety/ladder/history")).json()["items"][0]["reason"] == "Trying advise"
    customer, _ = await signup_user("Customer")
    assert (await customer.get("/api/v1/ops/content-safety/ladder")).status_code == 403


def test_the_check_runs_every_night():
    from app.workers.jobs import JOBS

    assert any(j.name == "ladder_check" for j in JOBS)
