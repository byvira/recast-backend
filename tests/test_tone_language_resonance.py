"""Tests for tone selection actually changing code-switching style per
output language — a real published Tamil LinkedIn post read like a
translated script rather than natural Tanglish conversation. Root cause:
build_tone_override() (app/pipelines/text/brand_context.py) had no idea
what language was being written in, and build_language_instruction()
(app/pipelines/text/generator.py) had no idea what tone was picked — two
fully independent instruction blocks with zero interaction, so every
non-English tone read the same regardless of which one was selected.

Also covers the Tamil-only CONVERSATIONAL_REGISTER_NOTES always pushing an
"everyday spoken" register even when an explicit formal/professional/direct
tone override was picked, which contradicted the override.

Direct-render tests only (no LLM/HTTP calls), same pattern as
tests/test_brand_context_tone_fallback.py and
tests/test_approved_copy_language.py.
"""

from app.pipelines.text.brand_context import build_tone_override
from app.pipelines.text.generator import build_language_instruction

CODE_SWITCH_MARKER = "Natural code-switching is expected"
COMPOSED_VOCAB_MARKER = "Lean toward composed, native"
REGISTER_PRECEDENCE_MARKER = "TONE OVERRIDE appears later in this prompt"


# ─────────────────────────────────────────────────────────────────────────────
# Informal tones (casual/punchy/storytelling) + non-English -> code-switching
# is explicitly permitted/encouraged, not left unspecified.
# ─────────────────────────────────────────────────────────────────────────────

def test_casual_tamil_permits_natural_code_switching():
    out = build_tone_override("casual", "ta")
    assert CODE_SWITCH_MARKER in out
    assert "Tamil" in out
    assert COMPOSED_VOCAB_MARKER not in out


def test_punchy_hindi_permits_natural_code_switching():
    """Different tone, different language — proves this isn't a
    casual-only or Tamil-only special case."""
    out = build_tone_override("punchy", "hi")
    assert CODE_SWITCH_MARKER in out
    assert "Hindi" in out


def test_storytelling_spanish_permits_natural_code_switching():
    out = build_tone_override("storytelling", "es")
    assert CODE_SWITCH_MARKER in out
    assert "Spanish" in out


# ─────────────────────────────────────────────────────────────────────────────
# Formal tones (formal/professional/direct) + non-English -> composed native
# vocabulary, explicitly different guidance from the informal tones above.
# ─────────────────────────────────────────────────────────────────────────────

def test_professional_tamil_leans_native_vocabulary_not_code_switching():
    out = build_tone_override("professional", "ta")
    assert COMPOSED_VOCAB_MARKER in out
    assert "Tamil" in out
    assert CODE_SWITCH_MARKER not in out


def test_direct_hindi_leans_native_vocabulary():
    out = build_tone_override("direct", "hi")
    assert COMPOSED_VOCAB_MARKER in out
    assert "Hindi" in out


def test_formal_tamil_leans_native_vocabulary():
    out = build_tone_override("formal", "ta")
    assert COMPOSED_VOCAB_MARKER in out


# ─────────────────────────────────────────────────────────────────────────────
# Inert for English and for brand/no-override — this is additive guidance,
# never a behavior change for the cases that worked fine before.
# ─────────────────────────────────────────────────────────────────────────────

def test_casual_english_has_no_code_switching_note():
    out = build_tone_override("casual", "en")
    assert CODE_SWITCH_MARKER not in out
    assert COMPOSED_VOCAB_MARKER not in out


def test_default_language_code_behaves_like_english():
    assert build_tone_override("casual") == build_tone_override("casual", "en")


def test_brand_tone_stays_empty_regardless_of_language():
    assert build_tone_override("brand", "ta") == ""
    assert build_tone_override(None, "ta") == ""


# ─────────────────────────────────────────────────────────────────────────────
# Register-note precedence — an explicit tone override must win over the
# Tamil-only "everyday spoken register" default, not silently coexist with
# a contradictory instruction.
# ─────────────────────────────────────────────────────────────────────────────

def test_tamil_language_instruction_defers_to_a_later_tone_override():
    out = build_language_instruction("ta")
    assert "everyday spoken Tamil" in out
    assert REGISTER_PRECEDENCE_MARKER in out


def test_hindi_language_instruction_has_no_register_note_to_defer():
    """Hindi has no CONVERSATIONAL_REGISTER_NOTES entry at all — confirms
    the precedence line is part of the Tamil-specific note, not a new
    unconditional block that would need adding for every language."""
    out = build_language_instruction("hi")
    assert REGISTER_PRECEDENCE_MARKER not in out
