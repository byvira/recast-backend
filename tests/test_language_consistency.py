"""One language rule for every generator, the Tanglish and script checks, and the draft review. No network."""
import asyncio

from app.models.text import Platform
from app.pipelines.text import critique, language_check, quality
from app.pipelines.text.normalizer import brand_grounding
from app.prompts.safe import hook_fits
from app.shared import language as lang

TANGLISH = (
    "Oru customer-ku reply panna romba late aachu. Adhukulla avanga vera brand-ku poitaanga. Idhu unga team-oda speed "
    "problem illa, process problem. Muthalla oru simple checklist ready pannunga, adhu podhum."
)
ENGLISH = (
    "Replying late to customers hurts retention. Customers move to another brand before your team has even read the "
    "message, so the process matters more than the speed of any one person on the team."
)


def run(coro):
    return asyncio.run(coro)


def _patch(monkeypatch, workspace=None, user=None):
    async def ws(_):
        return workspace

    async def us(_):
        return user

    monkeypatch.setattr(lang, "workspace_language", ws)
    monkeypatch.setattr(lang, "user_language", us)


def test_the_order_is_explicit_then_piece_then_campaign_then_brand_then_workspace(monkeypatch):
    _patch(monkeypatch, workspace="hi", user="en")
    assert run(lang.resolve_content_language(explicit="ta+en", piece="fr", campaign="de", brand="es")) == "ta+en"
    assert run(lang.resolve_content_language(piece="fr", campaign="de", brand="es")) == "fr"
    assert run(lang.resolve_content_language(campaign="de", brand="es")) == "de"
    assert run(lang.resolve_content_language(brand="es")) == "es"
    assert run(lang.resolve_content_language(workspace_id="w")) == "hi"


def test_the_members_default_english_no_longer_beats_detection(monkeypatch):
    _patch(monkeypatch, workspace=None, user="en")
    spanish = "Hoy quiero hablar de cómo organizar mejor el trabajo de un equipo pequeño sin perder la calma en el proceso."
    assert run(lang.resolve_content_language(user_id="u", text=spanish)) == "es"


def test_the_members_default_is_used_when_nothing_else_decides(monkeypatch):
    _patch(monkeypatch, workspace=None, user="ta+en")
    assert run(lang.resolve_content_language(user_id="u", text="ok")) == "ta+en"
    _patch(monkeypatch, workspace=None, user=None)
    assert run(lang.resolve_content_language(user_id="u")) == "en"


def test_plain_english_fails_a_tanglish_request_and_tanglish_passes():
    assert language_check.language_problem(ENGLISH, "ta+en")
    assert language_check.language_problem(TANGLISH, "ta+en") is None


def test_a_single_language_is_judged_by_its_script():
    english = "This is a long enough sentence written entirely in english letters so that the check can judge it properly."
    tamil = "இது தமிழில் எழுதப்பட்ட நீண்ட வாக்கியம் ஆகும், அதனால் சரிபார்ப்பு இதை சரியாக மதிப்பிட முடியும்."
    assert language_check.language_problem(english, "ta")
    assert language_check.language_problem(tamil, "ta") is None


def test_english_unknown_codes_and_short_text_are_never_judged():
    assert language_check.language_problem(ENGLISH, "en") is None
    assert language_check.language_problem(ENGLISH, "xx") is None
    assert language_check.language_problem("Short post.", "ta+en") is None


def test_the_quality_gate_sends_english_back_when_a_mix_was_asked_for():
    body = (ENGLISH + " ") * 6
    result = run(quality.run_quality_gate(body, Platform.LINKEDIN, "", [], language="ta+en"))
    assert not result.passed
    assert any(i.startswith("The post is ") for i in result.issues)
    ok = run(quality.run_quality_gate((TANGLISH + " ") * 6, Platform.LINKEDIN, "", [], language="ta+en"))
    assert ok.passed


def test_a_mixed_language_gets_no_english_readability_or_call_to_action_advice():
    result = run(quality.run_quality_gate((TANGLISH + " ") * 6, Platform.LINKEDIN, "", [], language="ta+en"))
    assert result.readability_score is None
    assert not any("CTA" in i or "Readability" in i for i in result.issues)


def test_one_minimum_length_table_is_used_by_both_checks():
    from app.pipelines.text import generator

    assert generator.MIN_WORD_COUNTS is quality.MIN_WORD_COUNTS
    assert quality.MIN_WORD_COUNTS[Platform.TWITTER_THREAD] == 200


def test_an_english_opening_cannot_replace_a_tanglish_one():
    assert not hook_fits(TANGLISH, "Late replies cost you customers every single week", language="ta+en")
    assert hook_fits(TANGLISH, "Customer-ku late-a reply panna, adhu ungaluku nashtam", language="ta+en")
    assert hook_fits(TANGLISH, "Late replies cost you customers every single week")


def test_a_language_is_inferred_for_romanised_posts_only():
    assert language_check.infer_mixed_language(TANGLISH) == "ta+en"
    assert language_check.infer_mixed_language(ENGLISH) is None


def test_deepgram_is_refused_for_a_stated_language_it_cannot_read():
    from app.pipelines.media import tts_generation as tts

    assert not tts._deepgram_can_read(TANGLISH, "ta+en")
    assert tts._deepgram_can_read(ENGLISH, "en")
    assert tts._deepgram_can_read(ENGLISH, None)


def test_a_weak_draft_review_returns_fixes_and_a_good_one_returns_none():
    weak = {"scores": {"specificity": 2, "grounding": 4, "audience_fit": 3, "voice_match": 4, "platform_fit": 4}, "verdict": "revise", "fixes": ["Name one concrete step."]}
    good = {"scores": {"specificity": 4, "grounding": 4, "audience_fit": 4, "voice_match": 4, "platform_fit": 4}, "verdict": "pass", "fixes": []}
    assert critique._verdict(weak) == ["Name one concrete step."]
    assert critique._verdict(good) is None
    assert critique._verdict({}) is None
    assert critique._verdict({"scores": {"specificity": 1}}) is None


def test_the_quality_set_scores_posts_and_names_what_got_worse():
    from app.pipelines.text import quality_set

    assert len(quality_set.TOPICS) == 20 and len(set(quality_set.TOPICS)) == 20
    good = quality_set.score_post(TANGLISH * 4, "ta+en", "customer replies")
    bad = quality_set.score_post(ENGLISH * 4, "ta+en", "customer replies")
    assert good["language_ok"] and not bad["language_ok"]
    assert quality_set.summarize([]) == {"posts": 0}

    before = quality_set.summarize([good, good, good, good])
    after = quality_set.summarize([good, bad, bad, bad])
    problems = quality_set.regressions(before, after)
    assert any("Right-language posts fell" in p for p in problems)
    assert quality_set.regressions(before, before) == []
    assert quality_set.regressions(before, {"posts": 0}) == ["Nothing was generated."]


def test_the_brand_facts_for_a_topic_brief_come_only_from_the_profile():
    profile = {
        "identity": {"bio": "A small studio that makes planners.", "offerings": [{"name": "Weekly planner"}]},
        "audience": {"primary_pain_point": "Too many tools, no calm", "interests": ["focus", "paper"]},
    }
    text = brand_grounding(profile)
    assert "Too many tools, no calm" in text and "Weekly planner" in text and "focus, paper" in text
    assert brand_grounding(None) == ""


def test_look_alike_characters_become_ordinary_ones_in_every_model_reply():
    from app.agents.content_guard.rules import clean_text, clean_value

    messy = "board\u2011la 30\u202f% meeting\u00a0time team\u2011oda"
    assert clean_text(messy) == "board-la 30 % meeting time team-oda"
    assert clean_value({"a": [messy]})["a"][0] == "board-la 30 % meeting time team-oda"


def test_a_mix_in_english_letters_fails_when_native_script_leaks_in():
    hinglish = (
        "Aap bhi notice karte hain ki jab aapka calendar mein chhoti pause hoti hai, toh kaam ka flow suddenly smoother lagta "
        "hai. Isse mujhe pata chala ki har pause ek reset button hai aur kaam bahut accha hota hai."
    )
    assert language_check.language_problem(hinglish, "hi+en") is None
    leaked = hinglish + " kar sakte \u0939\u0948\u0902"
    assert "English letters" in (language_check.language_problem(leaked, "hi+en") or "")
    assert language_check.language_problem(leaked, "hi+en").startswith("The post is ")  # routes to a rewrite


def test_the_language_instruction_asks_for_proper_words_and_names_the_common_slips():
    from app.pipelines.text.generator import build_language_instruction, resolve_language_directive_name

    tamil = build_language_instruction("ta+en")
    assert "PROPER WORDS ONLY" in tamil and '"kanavu" means a dream' in tamil and "Real Hindi" not in tamil
    assert "Real Hindi words" in build_language_instruction("hi+en")
    # Remy, Odette and the other writers use the short form, which carries the same rule.
    short = resolve_language_directive_name("ta+en")
    assert "keep the English word instead of guessing one by sound" in short and "never in Tamil script" in short


async def test_invented_figures_in_a_topic_post_are_sent_back_once_and_only_once():
    from app.agents.text import nodes
    from app.models.text import InputSourceType, Platform

    def state(retry_count):
        return {
            "emitter": None, "extras": {"banned_words": []}, "current_platform": Platform.LINKEDIN, "language": "en",
            "generated_content": ("Replying late hurts. " * 12) + "Meeting time dropped 30 % after one week of the new board. " + ("More words here. " * 12),
            "brand_context": "A small studio.", "normalised_content": "late replies", "raw_input": "late replies",
            "content_brief": "", "source_type": InputSourceType.TOPIC, "retry_count": retry_count, "generation_issues": [],
        }

    first = await nodes.quality_check_node(state(0))
    assert not first["quality_passed"]
    assert any(i.startswith("Invented detail") and "30" in i for i in first["quality_issues"])

    second = await nodes.quality_check_node(state(1))
    assert not any(i.startswith("Invented detail") for i in second["quality_issues"])
    assert any(i.startswith("Advisory: Not in your source") for i in second["quality_issues"])
