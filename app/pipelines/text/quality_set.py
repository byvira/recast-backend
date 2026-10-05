"""A fixed set of topics and a way to score what comes out, so a change to prompts or models can be compared before and after.

The scoring here uses no model: it counts what can be counted (right language, length, generic openings, unsupported figures).
`scripts/quality_regression.py` runs the set through the real pipeline, which makes real model calls, so it is run on purpose
and not as part of the tests.
"""
from __future__ import annotations

from statistics import mean
from typing import Optional

from app.pipelines.text.claims import unsupported_claims
from app.pipelines.text.generator import GENERIC_OPENINGS
from app.pipelines.text.language_check import language_problem

LANGUAGES = ("en", "ta+en", "hi+en")

TOPICS = (
    "Why small teams lose track of customer feedback",
    "How to plan a content week without burning out",
    "The cost of replying to leads too late",
    "What a good onboarding checklist looks like",
    "Why most meetings should be a message",
    "Turning one long video into a week of posts",
    "Hiring your first marketing person",
    "How to price a service you have never sold",
    "Keeping a brand voice when several people write",
    "What to measure in the first month of posting",
    "Writing a case study when the client is shy",
    "Saying no to features customers ask for",
    "Making a newsletter people finish reading",
    "How a founder can build trust before selling",
    "Simple ways to reuse old posts",
    "Handling a public complaint calmly",
    "Why consistency beats virality",
    "Planning a product launch with a tiny budget",
    "How to write a post from a voice note",
    "What to do when engagement suddenly drops",
)


def score_post(content: str, language: str, source: str = "") -> dict:
    """What can be counted about one finished post."""
    text = (content or "").strip()
    first_line = next((ln.strip().lower() for ln in text.splitlines() if ln.strip()), "")
    return {
        "words": len(text.split()),
        "language_ok": language_problem(text, language) is None,
        "generic_opening": any(first_line.startswith(g) for g in GENERIC_OPENINGS),
        "unsupported_claims": len(unsupported_claims(text, [source])),
    }


def summarize(rows: list[dict]) -> dict:
    """Averages over many scored posts: rates between 0 and 1, and the mean length."""
    if not rows:
        return {"posts": 0}
    return {
        "posts": len(rows),
        "language_ok_rate": round(mean(1.0 if r["language_ok"] else 0.0 for r in rows), 3),
        "generic_opening_rate": round(mean(1.0 if r["generic_opening"] else 0.0 for r in rows), 3),
        "unsupported_claims_per_post": round(mean(r["unsupported_claims"] for r in rows), 3),
        "mean_words": round(mean(r["words"] for r in rows), 1),
    }


def regressions(baseline: dict, current: dict, tolerance: float = 0.05) -> list[str]:
    """Plain sentences for everything that got worse than `baseline` by more than `tolerance` (a rate), or by 10 percent of length."""
    if current.get("posts", 0) == 0:
        return ["Nothing was generated."]
    problems: list[str] = []
    if current["language_ok_rate"] < baseline.get("language_ok_rate", 0) - tolerance:
        problems.append(f"Right-language posts fell from {baseline['language_ok_rate']:.0%} to {current['language_ok_rate']:.0%}.")
    if current["generic_opening_rate"] > baseline.get("generic_opening_rate", 1) + tolerance:
        problems.append(f"Generic openings rose from {baseline['generic_opening_rate']:.0%} to {current['generic_opening_rate']:.0%}.")
    if current["unsupported_claims_per_post"] > baseline.get("unsupported_claims_per_post", 99) + tolerance:
        problems.append("Posts carry more figures that the source does not back up.")
    base_words: Optional[float] = baseline.get("mean_words")
    if base_words and current["mean_words"] < base_words * 0.9:
        problems.append(f"Posts got shorter: {current['mean_words']} words on average, from {base_words}.")
    return problems
