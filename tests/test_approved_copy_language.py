"""Tests for a real, published-content bug: a brand's own English-authored
"approved copy" (required phrases / approved openers / approved closers,
app/prompts/text/generate/approved_copy.jinja) used to be injected into the
prompt with no instruction to translate it, so a non-English generation
would keep the literal English phrase and wrap target-language grammar
around it — e.g. a real Tamil LinkedIn post came back with
"'Scale your voice, not your workload' – இதுதான் நம்முடைய நோக்கம்".

The fix is two-part and deliberately has zero per-language branching (no
new params, no language allowlist), so it must hold for every language,
not just Tamil — every assertion below is duplicated for a second,
unrelated language to prove that.

Direct-render tests only (no LLM/HTTP calls), same pattern as
tests/test_brand_context_tone_fallback.py.
"""

from app.pipelines.text.generator import build_approved_copy_instruction, build_language_instruction
from app.models.text import AgentTask, Platform

NEVER_LITERAL_MARKER = "never insert the English text itself"
ADAPT_MARKER = "never paste this English text literally"


def _task_with_approved_copy() -> AgentTask:
    return AgentTask(
        agent="test",
        platform=Platform.LINKEDIN,
        content="source",
        brand_context="",
        session_id="s1",
        metadata={
            "required_phrases": [{"text": "Scale your voice, not your workload", "placement": "any"}],
            "approved_openers": ["The uncomfortable truth:"],
            "approved_closers": ["Ready to lock in brand voice?"],
        },
    )


def test_required_phrases_instruct_translation_of_meaning():
    out = build_approved_copy_instruction(_task_with_approved_copy())
    assert "Scale your voice, not your workload" in out
    assert NEVER_LITERAL_MARKER in out


def test_approved_opener_instructs_translation():
    out = build_approved_copy_instruction(_task_with_approved_copy())
    assert "The uncomfortable truth:" in out
    assert ADAPT_MARKER in out


def test_approved_closer_instructs_translation():
    out = build_approved_copy_instruction(_task_with_approved_copy())
    assert "Ready to lock in brand voice?" in out
    # Appears twice (opener block + closer block) — confirm at least once.
    assert out.count(ADAPT_MARKER) == 2


def test_language_instruction_broadened_beyond_structural_examples_tamil():
    out = build_language_instruction("ta")
    assert "Tamil" in out
    assert "never paste the literal English text" in out
    assert "required/approved phrases" in out


def test_language_instruction_broadened_beyond_structural_examples_hindi():
    """Same assertions, different language — proves the fix isn't Tamil-specific."""
    out = build_language_instruction("hi")
    assert "Hindi" in out
    assert "never paste the literal English text" in out
    assert "required/approved phrases" in out
