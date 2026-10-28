"""Odette's briefing is kept to the shape its instructions ask for, whatever the model sends back."""

from app.agents.supervisor.nodes import clean_findings


def test_a_well_formed_briefing_passes_through():
    parsed = {
        "insights": [{"kind": "recommendation", "title": "Post more on LinkedIn", "body": "It gets twice the replies.",
                      "rationale": "Engagement gap.", "priority": "high", "pipeline_scope": ["text"]}],
        "flags": [{"severity": "warning", "summary": "Publishing stopped on Tuesday.", "detail": {"failed": 3}}],
        "notify": True,
    }
    out = clean_findings(parsed)
    assert out["insights"][0]["title"] == "Post more on LinkedIn" and out["insights"][0]["pipeline_scope"] == ["text"]
    assert out["flags"][0]["detail"] == {"failed": 3} and out["notify"] is True


def test_a_malformed_briefing_is_trimmed_to_safe_shapes():
    parsed = {
        "insights": [{"title": 42, "body": ["not", "text"], "pipeline_scope": "everything"}, "just a string", {"title": ""},
                     {"title": "A"}, {"title": "B"}, {"title": "C"}],
        "flags": {"severity": "critical"},
        "notify": "false",
    }
    out = clean_findings(parsed)
    assert [i["title"] for i in out["insights"]] == ["42", "A", "B"]
    assert out["insights"][0]["body"] == "" and out["insights"][0]["pipeline_scope"] == "all"
    assert out["flags"] == [] and out["notify"] is False


def test_flags_need_a_summary_and_keep_at_most_two():
    flags = [{"summary": f"Flag {n}", "detail": "plain words"} for n in range(4)] + [{"severity": "critical"}]
    out = clean_findings({"flags": flags})
    assert [f["summary"] for f in out["flags"]] == ["Flag 0", "Flag 1"]
    assert out["flags"][0]["detail"] == {"note": "plain words"} and out["insights"] == []
