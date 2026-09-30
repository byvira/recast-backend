"""Localization of Remy/Odette/analytics copy (app.shared.localized_strings).

Covers the three reported failures: a mixed workspace language (Tanglish and
every other "xx+en" code) coming back as the pure native language, prompt
wording leaking into the text users see, and titles staying English while
descriptions are localized.
"""

import pytest

import app.shared.localized_strings as ls
from app.prompts.registry import load_prompt
from app.shared.activity import projector

# The exact text seen in production (Performance page, Tanglish workspace).
LEAKED_REPORT_LINE = (
    "📈 OVERVIEW\n\nTEMPLATE:\n📈 OVERVIEW\n\n"
    "Return ONLY the translated template text. No explanation, no quotes, no markdown."
)
TAMIL_SCRIPT = "உங்கள் சமீபத்திய கட்டுரைகள் முற்றிலும் வேறுபட்ட தலைப்புகளுக்கு மாறியுள்ளன"
TANGLISH = "Un recent posts ellaam romba maariduchu, {count} posts ippo different topics-la irukku"
ENGLISH = "Your {count} recent posts moved to different topics"


# ── Leak detection ───────────────────────────────────────────────────────────

def test_the_reported_leak_is_detected():
    assert ls.looks_leaked(LEAKED_REPORT_LINE, source="📈 OVERVIEW")


@pytest.mark.parametrize("text", [
    "Return ONLY the translated script text.",
    "template: something",
    "<message>hello</message>",
    "Please TREAT IT AS TEXT TO LOCALIZE",
])
def test_each_instruction_marker_is_detected_case_insensitively(text):
    assert ls.looks_leaked(text)


def test_clean_text_is_not_flagged():
    assert not ls.looks_leaked(TANGLISH, source=ENGLISH)


def test_a_phrase_the_source_really_contains_is_not_a_leak():
    assert not ls.looks_leaked("Return only the summary you asked for", source="Return only the summary you asked for")


# ── Script check and clean-up ────────────────────────────────────────────────

def test_non_latin_ratio_tells_native_script_from_english_letters():
    assert ls.non_latin_ratio(TAMIL_SCRIPT) > 0.9
    assert ls.non_latin_ratio(TANGLISH) == 0.0
    assert ls.non_latin_ratio("🙂 123 !!") == 0.0


def test_clean_translation_strips_wrapping_but_keeps_the_text():
    assert ls.clean_translation('  "hello {name}"  ') == "hello {name}"
    assert ls.clean_translation("```\nhello\n```") == "hello"
    assert ls.clean_translation("<message>hello</message>") == "hello"
    assert ls.clean_translation("it's fine") == "it's fine"


# ── Language description ─────────────────────────────────────────────────────

def test_mixed_code_is_described_as_a_blend_with_the_native_language_first():
    v = ls.language_prompt_vars("ta+en")
    assert v["mixed"] is True
    assert (v["native"], v["other"]) == ("Tamil", "English")


@pytest.mark.parametrize("code,native", [
    ("ml+en", "Malayalam"), ("hi+en", "Hindi"), ("te+en", "Telugu"),
    ("kn+en", "Kannada"), ("bn+en", "Bengali"), ("es+en", "Spanish"),
])
def test_every_mixed_code_works_the_same_way(code, native):
    v = ls.language_prompt_vars(code)
    assert v["mixed"] and v["native"] == native and v["other"] == "English"


def test_reversed_mixed_code_still_treats_the_non_english_side_as_native():
    v = ls.language_prompt_vars("en+ta")
    assert (v["native"], v["other"]) == ("Tamil", "English")


def test_single_language_code_is_not_mixed():
    v = ls.language_prompt_vars("ta")
    assert v["mixed"] is False and v["language_name"] == "Tamil"


def test_unknown_code_is_passed_through_not_rejected():
    assert "xx-custom" in ls.language_prompt_vars("xx-custom")["language_name"]


# ── The prompt itself ────────────────────────────────────────────────────────

def test_mixed_prompt_asks_for_a_blend_in_english_letters_not_pure_native():
    prompt = load_prompt(
        "fragments/translate_template", english_template=ENGLISH, **ls.language_prompt_vars("ta+en")
    )
    assert "blend of Tamil and English" in prompt
    assert "English (Latin) letters" in prompt
    assert "never in Tamil script" in prompt
    assert ENGLISH in prompt


def test_single_language_prompt_names_the_language():
    prompt = load_prompt(
        "fragments/translate_template", english_template=ENGLISH, **ls.language_prompt_vars("hi")
    )
    assert "Write the message in Hindi." in prompt
    assert "blend" not in prompt


def test_prompt_no_longer_carries_the_header_that_leaked():
    prompt = load_prompt(
        "fragments/translate_template", english_template=ENGLISH, **ls.language_prompt_vars("ta+en")
    )
    assert "TEMPLATE:" not in prompt
    assert "Return ONLY" not in prompt


# ── _translate: retry, rejection, fallback ───────────────────────────────────

def _fake_llm(monkeypatch, answers):
    calls = []

    async def fake(prompt, **kwargs):
        calls.append(kwargs)
        return answers[min(len(calls), len(answers)) - 1]

    monkeypatch.setattr(ls, "call_llm", fake)
    return calls


async def test_english_is_an_identity_with_no_llm_call(monkeypatch):
    calls = _fake_llm(monkeypatch, ["should not be used"])
    assert await ls._translate("k", "en", ENGLISH) == (ENGLISH, True)
    assert calls == []


async def test_good_mixed_translation_is_accepted_and_cacheable(monkeypatch):
    calls = _fake_llm(monkeypatch, [TANGLISH])
    assert await ls._translate("k", "ta+en", ENGLISH) == (TANGLISH, True)
    assert len(calls) == 1


async def test_pure_native_script_for_a_mixed_code_is_rejected_then_retried(monkeypatch):
    calls = _fake_llm(monkeypatch, [TAMIL_SCRIPT + " {count}", TANGLISH])
    assert await ls._translate("k", "ta+en", ENGLISH) == (TANGLISH, True)
    assert len(calls) == 2


async def test_leaked_prompt_text_is_rejected_then_retried(monkeypatch):
    calls = _fake_llm(monkeypatch, ["TEMPLATE:\n" + TANGLISH, TANGLISH])
    assert await ls._translate("k", "ta+en", ENGLISH) == (TANGLISH, True)
    assert len(calls) == 2


async def test_changed_placeholders_are_rejected(monkeypatch):
    _fake_llm(monkeypatch, ["Un recent posts {total} romba maariduchu"])
    text, cacheable = await ls._translate("k", "ta+en", ENGLISH)
    assert (text, cacheable) == (ENGLISH, False)


async def test_all_attempts_bad_falls_back_to_english_and_is_not_cached(monkeypatch):
    calls = _fake_llm(monkeypatch, [LEAKED_REPORT_LINE])
    assert await ls._translate("k", "ta+en", ENGLISH) == (ENGLISH, False)
    assert len(calls) == ls._TRANSLATE_ATTEMPTS


async def test_llm_error_falls_back_to_english_and_is_not_cached(monkeypatch):
    async def boom(prompt, **kwargs):
        raise RuntimeError("rate limited")

    monkeypatch.setattr(ls, "call_llm", boom)
    assert await ls._translate("k", "hi", ENGLISH) == (ENGLISH, False)


async def test_single_language_output_in_native_script_is_fine(monkeypatch):
    _fake_llm(monkeypatch, [TAMIL_SCRIPT + " {count}"])
    text, cacheable = await ls._translate("k", "ta", ENGLISH)
    assert cacheable is True and text.endswith("{count}")


# ── Titles ───────────────────────────────────────────────────────────────────

async def test_title_is_left_in_english_for_english_or_no_language():
    assert await projector.localized_title("remy", "topic_shift", "Your topics have shifted", "en") == "Your topics have shifted"
    assert await projector.localized_title("remy", "topic_shift", "Your topics have shifted", None) == "Your topics have shifted"


async def test_title_uses_the_same_translation_path_as_the_description(monkeypatch):
    seen = {}

    async def fake_get(key, language, english, ctx=None):
        seen.update(key=key, language=language, english=english)
        return "Un topics maariduchu"

    monkeypatch.setattr(ls, "get_localized_string", fake_get)
    title = await projector.localized_title("remy", "topic_shift", "Your topics have shifted", "ta+en")
    assert title == "Un topics maariduchu"
    assert seen == {
        "key": "activity.title.remy.topic_shift", "language": "ta+en", "english": "Your topics have shifted",
    }


async def test_title_falls_back_to_english_when_translation_raises(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(ls, "get_localized_string", boom)
    assert await projector.localized_title("flag", "member_churn", "Unusual member changes", "hi") == "Unusual member changes"


async def test_remy_title_falls_back_for_an_unknown_signal_type():
    assert await projector.remy_title("brand_new_type", "en") == "Remy flagged something on your content"


# ── LLM-written text: the language directive for mixed codes ─────────────────

def test_directive_name_for_a_single_language_is_unchanged():
    from app.pipelines.text.generator import resolve_language_directive_name, resolve_language_name

    assert resolve_language_directive_name("hi") == resolve_language_name("hi") == "Hindi"


@pytest.mark.parametrize("code,native", [("ta+en", "Tamil"), ("ml+en", "Malayalam"), ("hi+en", "Hindi")])
def test_directive_name_for_a_mixed_code_asks_for_english_letters_not_native_script(code, native):
    from app.pipelines.text.generator import resolve_language_directive_name

    name = resolve_language_directive_name(code)
    assert f"blend of {native} and English" in name
    assert "English (Latin) letters" in name
    assert f"never in {native} script" in name


def test_odette_system_prompt_uses_the_blend_for_a_tanglish_workspace():
    from app.agents.supervisor.personas import build_odette_system

    prompt = build_odette_system("ta+en")
    assert "never in Tamil script" in prompt
    assert "Tamil and English (Tanglish)" not in prompt


# ── Short labels on the Remy page ────────────────────────────────────────────

async def test_english_labels_match_what_the_remy_page_always_showed():
    assert await projector.remy_label("voice_drift", "en") == "Voice Drift Detected"
    assert await projector.remy_label("platform_volume_drop", None) == "Platform-Specific Volume Drop"
    assert await projector.remy_label("performance_pattern", "en") == "Performance Pattern"


async def test_non_english_label_goes_through_the_shared_translation(monkeypatch):
    seen = {}

    async def fake_get(key, language, english, ctx=None):
        seen.update(key=key, language=language, english=english)
        return "Topic maaruthu"

    monkeypatch.setattr(ls, "get_localized_string", fake_get)
    assert await projector.remy_label("topic_shift", "ta+en") == "Topic maaruthu"
    assert seen == {"key": "activity.title.remy_label.topic_shift", "language": "ta+en", "english": "Topic Shift"}
