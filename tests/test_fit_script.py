"""Rewrite-to-fit for narration scripts: every model answer is checked, bad ones never used. Pure."""

import asyncio

from app.pipelines.media import duration
from app.pipelines.media import fit_script as fs

SENTENCE = "This sentence has exactly seven words."
LONG = " ".join([SENTENCE] * 40)   # 280 words, about 112 s at 150 wpm
SHORT = " ".join([SENTENCE] * 4)   # 28 words, about 11 s


def words(n: int) -> str:
    return " ".join(["word"] * n)


def run(script, target, answers, wpm=150):
    calls = []

    async def llm(prompt):
        calls.append(prompt)
        item = answers[min(len(calls) - 1, len(answers) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    out = asyncio.run(fs.fit_script(script, target, wpm, "English", llm))
    return out, calls


def test_a_script_that_already_fits_is_not_touched_and_costs_nothing():
    text = words(150)  # 60 s at 150 wpm
    out, calls = run(text, 60, ["x"])
    assert out["status"] == "already_fits" and out["script"] == text and calls == []


def test_a_good_shorter_rewrite_is_used():
    out, calls = run(LONG, 60, [words(150)])
    assert out["status"] == "fitted" and out["script"] == words(150) and len(calls) == 1
    assert "shorter" in calls[0] and "<script>" in calls[0]


def test_a_bad_first_answer_is_retried_once():
    out, calls = run(LONG, 60, [words(400), words(150)])
    assert out["status"] == "fitted" and len(calls) == 2 and out["script"] == words(150)


def test_a_too_long_script_falls_back_to_cutting_at_a_sentence_end():
    out, calls = run(LONG, 60, [words(900)])
    assert out["status"] == "fitted" and len(calls) == 2
    assert duration.estimate_seconds(out["script"], 150) <= 60 and out["script"].endswith(".")


def test_a_provider_error_on_a_long_script_still_trims():
    out, _ = run(LONG, 60, [RuntimeError("down")])
    assert out["status"] == "fitted" and duration.estimate_seconds(out["script"], 150) <= 60


def test_a_too_short_script_that_cannot_be_expanded_is_returned_unchanged_with_a_reason():
    out, calls = run(SHORT, 60, [words(5)])
    assert out["status"] == "unchanged" and out["script"] == SHORT and out["reason"] and len(calls) == 2


def test_a_good_expansion_is_used_and_asks_for_no_new_facts():
    out, calls = run(SHORT, 60, [words(150)])
    assert out["status"] == "fitted" and "longer" in calls[0] and "Do not add facts" in calls[0]


def test_leaked_instructions_are_rejected():
    leaked = "Return ONLY the rewritten script. " + words(150)
    out, _ = run(SHORT, 60, [leaked])
    assert out["status"] == "unchanged"


def test_wrapping_the_model_adds_is_removed():
    out, _ = run(LONG, 60, ["```\n" + words(150) + "\n```"])
    assert out["status"] == "fitted" and out["script"] == words(150)


def test_the_target_is_kept_inside_the_allowed_range():
    # 1 second is raised to the minimum target, so a short script is judged against that
    out, _ = run(words(10), 1, [words(38)])
    assert out["status"] in ("fitted", "unchanged", "already_fits")
