"""The text agent writes into the same decision log as image, audio and video, so Odette reads one format."""
from types import SimpleNamespace
from uuid import uuid4

from app.agents.supervisor.tools import make_tools
from app.db.mongo import get_db
from app.models.text import GeneratedPiece, Platform
from app.pipelines.text import orchestrator


def _normalised(ws: str):
    return SimpleNamespace(workspace_id=ws, user_id="u1")


def _piece(**kw) -> GeneratedPiece:
    base = dict(platform=Platform.LINKEDIN, content="A real post about calmer weeks.", word_count=6, char_count=30,
                quality_passed=True, quality_issues=[], flagged_for_review=False)
    base.update(kw)
    return GeneratedPiece(**base)


async def test_a_passing_post_is_logged_as_ok_with_its_evidence():
    ws = f"ws-{uuid4()}"
    await orchestrator._log_text_decision(_normalised(ws), Platform.LINKEDIN, _piece(), is_repurpose=False, batch_mode=False, session_id="s1")
    row = await get_db()["agent_decisions"].find_one({"workspace_id": ws, "agent": "text"})
    assert row["outcome"] == "ok" and row["step"] == "write_post" and row["run_id"] == "s1"
    assert row["evidence"]["quality_passed"] is True and row["evidence"]["words"] == 6


async def test_a_post_held_for_review_is_a_decision_waiting_for_a_person():
    ws = f"ws-{uuid4()}"
    held = _piece(quality_passed=False, flagged_for_review=True, quality_issues=["Too generic", "No clear hook"])
    await orchestrator._log_text_decision(_normalised(ws), Platform.LINKEDIN, held, is_repurpose=True, batch_mode=False, session_id=None)
    _, dispatch = make_tools(ws)
    waiting = await dispatch("get_needs_decision", {})
    assert waiting["count"] == 1 and "held for your review" in waiting["items"][0]["summary"]
    row = await get_db()["agent_decisions"].find_one({"workspace_id": ws})
    assert row["evidence"]["issues"] == ["Too generic", "No clear hook"] and row["evidence"]["repurposed"] is True


async def test_a_post_that_was_not_made_is_logged_as_failed():
    ws = f"ws-{uuid4()}"
    await orchestrator._log_text_decision(
        _normalised(ws), Platform.LINKEDIN, _piece(content="", word_count=0, char_count=0, quality_passed=False),
        is_repurpose=False, batch_mode=True, session_id="s2",
    )
    row = await get_db()["agent_decisions"].find_one({"workspace_id": ws})
    assert row["outcome"] == "failed" and row["evidence"]["batch"] is True
