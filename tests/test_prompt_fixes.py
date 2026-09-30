"""Stage 7 fixes: score clamping, clip quotes, angle lists, thread and placeholder checks, language lines.
Pure: models and providers are replaced."""

import asyncio

from app.models.text import AgentTask, Platform
from app.pipelines.media import clip_check
from app.prompts import safe
from app.prompts.registry import load_prompt

WORDS = [
    {"word": "Systems", "start_s": 0.0, "end_s": 0.4},
    {"word": "beat", "start_s": 0.4, "end_s": 0.7},
    {"word": "willpower", "start_s": 0.7, "end_s": 1.3},
    {"word": "every", "start_s": 1.3, "end_s": 1.6},
    {"word": "time", "start_s": 1.6, "end_s": 2.0},
    {"word": "Later", "start_s": 5.0, "end_s": 5.4},
]


# ---- scores ------------------------------------------------------------------------------------
def test_scores_are_always_a_number_between_zero_and_one():
    assert safe.clamp_score(0.8) == 0.8
    assert safe.clamp_score("0.4") == 0.4
    assert safe.clamp_score(87) == 0.87
    assert safe.clamp_score(1000) == 1.0 and safe.clamp_score(-5) == 0.0
    assert safe.clamp_score("high") == 0.5 and safe.clamp_score(None) == 0.5
    assert safe.clamp_score(float("nan")) == 0.5


# ---- clip quotes -------------------------------------------------------------------------------
def test_a_real_quote_is_kept():
    assert clip_check.verified_quote("Systems beat willpower", WORDS, 0.0, 2.0) == "Systems beat willpower"


def test_a_made_up_quote_is_replaced_by_the_real_words():
    out = clip_check.verified_quote("Discipline is a myth sold by gurus", WORDS, 0.0, 2.0)
    assert out == "Systems beat willpower every time"


def test_an_empty_quote_becomes_the_real_words_and_words_outside_the_span_are_ignored():
    assert clip_check.verified_quote("", WORDS, 0.0, 2.0) == "Systems beat willpower every time"
    assert "Later" not in clip_check.span_text(WORDS, 0.0, 2.0)


# ---- language lines in the templates -----------------------------------------------------------
def test_the_templates_now_carry_a_language_line():
    line = "Write in Tamil."
    hooks = load_prompt("text/hooks/generate", brand_context="b", platform_label="LinkedIn", banned_openings=[], banned_words=[], content="c", language_line=line)
    assert line in hooks
    seo = load_prompt("text/seo/master", platform_instruction="p", content="c", language_line=line)
    assert line in seo
    angles = load_prompt("text/orchestrate/batch_angles", days=3, topic_cluster="t", language_line=line)
    assert line in angles


def test_the_hook_scorer_no_longer_claims_banned_words_are_rejected_automatically():
    out = load_prompt(
        "text/hooks/score", language_note_active=False, brand_context="b", banned_words=["leverage"], approved_openers=[],
        platform="LinkedIn", content="c", language_line="x", current_hook="h", examples=[],
    )
    assert "rejected automatically" not in out and "leverage" in out


# ---- angles -------------------------------------------------------------------------------------
def test_angles_keep_only_real_text_without_banned_words(monkeypatch):
    from app.pipelines.text import angles

    async def fake(prompt, **k):
        return {"angles": [
            {"name": "Contrarian", "rationale": "r", "content": "A clean rewrite of the piece."},
            {"name": "Leaky", "rationale": "r", "content": "Return ONLY the rewritten piece. Text."},
            {"name": "Banned", "rationale": "r", "content": "We leverage this idea."},
            {"name": "Wrapped", "rationale": "r", "content": '{"content": "Wrapped rewrite."}'},
            "junk",
            {"name": "Empty", "rationale": "r", "content": "  "},
        ]}

    monkeypatch.setattr(angles, "call_llm_structured", fake)
    task = AgentTask(agent="angles", platform=Platform.LINKEDIN, content="The original post text.", brand_context="", metadata={"banned_words": ["leverage"]}, session_id="s")
    result = asyncio.run(angles.run_angles_agent(task))
    names = [a["name"] for a in result.output["angles"]]
    assert result.success and names == ["Contrarian", "Wrapped"]
    assert result.output["angles"][1]["content"] == "Wrapped rewrite."


def test_angles_with_nothing_usable_fail_cleanly(monkeypatch):
    from app.pipelines.text import angles

    async def fake(prompt, **k):
        return {"angles": [{"name": "x", "content": ""}]}

    monkeypatch.setattr(angles, "call_llm_structured", fake)
    task = AgentTask(agent="angles", platform=Platform.LINKEDIN, content="Original.", brand_context="", metadata={}, session_id="s")
    result = asyncio.run(angles.run_angles_agent(task))
    assert not result.success and result.output == {"angles": []}


# ---- thread shape and placeholders --------------------------------------------------------------
def _thread(tweets):
    from app.pipelines.text.generator import validate_content

    content = "\n".join(f"{i}/ {t}" for i, t in enumerate(tweets, start=1))
    return validate_content(content=content + " " + " ".join(["word"] * 200), platform=Platform.TWITTER_THREAD,
                            banned_words=[], required_phrases=[], approved_openers=[], approved_closers=[], language="ta")


def test_a_thread_tweet_over_the_limit_is_a_hard_issue():
    ok, issues = _thread(["short one", "x" * 300])
    assert not ok and any("Thread tweet" in i for i in issues)


def test_a_thread_within_the_limit_passes():
    ok, issues = _thread(["short one", "another short one"])
    assert ok, issues


def test_placeholder_text_is_flagged_as_advice_not_a_failure():
    from app.pipelines.text.generator import validate_content

    ok, issues = validate_content(
        content=" ".join(["word"] * 120) + " [LINK]", platform=Platform.YOUTUBE, banned_words=[], required_phrases=[],
        approved_openers=[], approved_closers=[], language="ta",
    )
    assert ok and any(i.startswith("Advisory:") and "[LINK]" in i for i in issues)
