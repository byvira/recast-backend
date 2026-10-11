"""Recommendations are plain strings, at most five, and never quote a percentage the analysis does not hold."""
import json
from pathlib import Path

from app.agents.analytics.nodes import clean_recommendations

ANALYSIS = "LinkedIn engagement is 4.2% and Instagram is 1.1%."


def test_non_text_empty_and_extra_items_are_dropped():
    raw = ["Lead with LinkedIn.", "", None, {"a": 1}, "Second", "Third", "Fourth", "Fifth", "Sixth"]
    assert clean_recommendations(raw, ANALYSIS) == ["Lead with LinkedIn.", "Second", "Third", "Fourth", "Fifth"]


def test_a_percentage_that_is_not_in_the_analysis_is_dropped():
    kept = clean_recommendations(["Aim for 90% engagement.", "Match LinkedIn's 4.2% on Instagram."], ANALYSIS)
    assert kept == ["Match LinkedIn's 4.2% on Instagram."]


def test_a_single_string_reply_becomes_a_one_item_list():
    assert clean_recommendations("Do X.", ANALYSIS) == ["Do X."]


def test_the_prompt_example_carries_no_numbers_to_copy():
    line = json.loads((Path(__file__).resolve().parent.parent / "app" / "prompts" / "fixtures" / "analytics_recommend_example.json").read_text(encoding="utf-8"))["example_line"]
    assert not any(ch.isdigit() for ch in line)
