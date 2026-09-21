"""Tests for the analytics agent actually answering a real question.

Previously analyze_node always ran a fixed 4-point checklist (best
platform / trends / content type / underperformers) and format_report_node
always wrapped it in the same full OVERVIEW/BEST POST/ANALYSIS/
RECOMMENDATIONS skeleton, regardless of what was asked — {{ question }}
was interpolated into the prompt but the instructions never referenced it,
so every question in the "Ask" chat came back looking identical. Fixed by
branching both nodes on whether state["question"] is the shared
DEFAULT_OVERVIEW_QUESTION constant.

Unit-level — calls the node functions directly with a constructed state,
not the full HTTP/graph/DB stack, since analyze_node/format_report_node
are pure functions of their state dict plus a mocked LLM call.
"""

from app.agents.analytics.nodes import analyze_node, format_report_node, recommend_node
from app.agents.analytics.state import DEFAULT_OVERVIEW_QUESTION, build_initial_state


def _state_with_metrics(question: str) -> dict:
    state = build_initial_state(workspace_id="ws1", question=question, language="en")
    state["connected_platforms"] = ["linkedin", "facebook"]
    state["account_metrics"] = [
        {"platform": "linkedin", "followers": 0, "total_impressions": 0, "total_reach": 0},
        {"platform": "facebook", "followers": 0, "total_impressions": 5, "total_reach": 1},
    ]
    state["post_metrics"] = [
        {"platform": "facebook", "likes": 0, "comments": 0, "reposts": 0, "engagement_rate": 0.0},
    ]
    return state


async def test_specific_question_uses_the_focused_prompt_not_the_checklist(mock_llm):
    mock_llm.set_plain("Facebook is your only channel with any real signal right now.")
    state = _state_with_metrics("What should I post next?")

    result = await analyze_node(state)
    assert result["analysis"] == "Facebook is your only channel with any real signal right now."


async def test_default_overview_question_uses_the_checklist_prompt(mock_llm):
    mock_llm.set_plain("1. Best platform... 2. Trends... 3. Content type... 4. Underperformers...")
    state = _state_with_metrics(DEFAULT_OVERVIEW_QUESTION)

    result = await analyze_node(state)
    assert "Best platform" in result["analysis"]


async def test_specific_question_report_is_focused_not_the_full_skeleton(mock_llm):
    state = _state_with_metrics("What should I post next?")
    state["analysis"] = "Facebook is your only channel with any real signal right now."
    state["recommendations"] = ["Post on Facebook this week."]

    result = await format_report_node(state)
    report = result["report"]

    assert "Facebook is your only channel" in report
    assert "Post on Facebook this week." in report
    # The full-overview-only sections must NOT appear for a real question —
    # this is exactly what made every question look like the same report.
    assert "OVERVIEW" not in report
    assert "BEST PERFORMING POST" not in report


async def test_default_overview_report_keeps_the_full_skeleton(mock_llm):
    state = _state_with_metrics(DEFAULT_OVERVIEW_QUESTION)
    state["analysis"] = "1. Best platform... 2. Trends..."
    state["recommendations"] = ["Do X.", "Do Y."]

    result = await format_report_node(state)
    report = result["report"]

    assert "OVERVIEW" in report
    assert "BEST PERFORMING POST" in report
    assert "Total Impressions:" in report


async def test_recommend_node_receives_the_real_question(monkeypatch):
    captured = {}

    async def _fake_call_llm_structured(prompt=None, *args, **kwargs):
        captured["prompt"] = prompt
        return ["Do X."]

    import app.agents.analytics.nodes as nodes_module
    monkeypatch.setattr(nodes_module, "call_llm_structured", _fake_call_llm_structured)

    state = _state_with_metrics("What should I post next?")
    state["analysis"] = "Facebook is your only channel with real signal."

    await recommend_node(state)
    # The real rendered prompt (analytics/recommend.jinja) must actually
    # contain the question now — previously recommend.jinja never received
    # it at all.
    assert "What should I post next?" in captured["prompt"]
