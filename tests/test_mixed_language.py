"""Tests for mixed-language (Tanglish-style) generation as a first-class,
explicitly-selectable mode — FINDINGS.md's Q5 gap. Distinct from earlier
this session's tone/language code-switching work (Casual+Tamil permitting
occasional English loanwords): this is a deliberately-selected blended
register a user picks, not an implicit side effect of tone.

A mixed code like "ta+en" needs zero schema/validation changes anywhere —
language fields are already fully opaque, unvalidated strings throughout
this codebase — so these are all direct-render tests against the two
functions that give the code meaning: resolve_language_name() and
build_language_instruction(). Also proves build_tone_override() doesn't
double up its own code-switching guidance on top of the dedicated mixed
instruction, and that every existing single-language code is unaffected.
"""

from app.pipelines.text.brand_context import build_tone_override
from app.pipelines.text.generator import build_language_instruction, resolve_language_name


def test_resolve_language_name_gives_a_real_display_name_for_mixed_codes():
    assert resolve_language_name("ta+en") == "Tamil and English (Tanglish)"
    assert resolve_language_name("hi+en") == "Hindi and English (Hinglish)"


def test_build_language_instruction_renders_mixed_mode_not_single_language():
    out = build_language_instruction("ta+en")
    assert "Tamil" in out
    assert "English" in out
    assert "natural, fluid blend" in out
    assert "Respond entirely in" not in out


def test_build_language_instruction_mixed_mode_still_warns_against_literal_copying():
    """This fragment fully replaces language_instruction.jinja for mixed
    codes, so it needs its own copy of the "don't paste prompt text
    literally" protection — not inherited from the single-language path."""
    out = build_language_instruction("ta+en")
    assert "never paste literal English prompt text" in out


def test_build_language_instruction_mixed_mode_works_for_every_configured_pair():
    for code in ("ta+en", "hi+en", "te+en", "kn+en", "ml+en", "bn+en", "es+en", "tl+en"):
        out = build_language_instruction(code)
        assert "natural, fluid blend" in out, f"failed for {code}"
        assert "the language identified by the code" not in out, f"failed for {code}"


def test_tone_override_does_not_double_up_code_switching_guidance_for_mixed_mode():
    """build_tone_override()'s own "Natural code-switching is expected"
    addition (added earlier this session) would be redundant on top of
    the dedicated mixed-language instruction — must not fire here."""
    out = build_tone_override("casual", "ta+en")
    assert "Natural code-switching is expected" not in out
    assert "TONE OVERRIDE" in out  # the base tone instruction still applies


def test_tone_override_formal_tones_also_skip_the_addition_for_mixed_mode():
    out = build_tone_override("professional", "ta+en")
    assert "Lean toward composed, native" not in out
    assert "TONE OVERRIDE" in out


# ─────────────────────────────────────────────────────────────────────────────
# Regression — every existing single-language code must be completely
# unaffected by the new "+" branch.
# ─────────────────────────────────────────────────────────────────────────────

def test_single_language_codes_are_unaffected():
    out = build_language_instruction("ta")
    assert "Respond entirely in Tamil" in out
    assert "natural, fluid blend" not in out


def test_english_is_unaffected():
    out = build_language_instruction("en")
    assert "Respond entirely in English" in out


def test_single_language_tone_override_code_switching_still_fires():
    out = build_tone_override("casual", "ta")
    assert "Natural code-switching is expected" in out
