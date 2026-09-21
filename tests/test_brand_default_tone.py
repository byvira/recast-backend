"""Unit-level coverage for _build_metadata()'s tone-resolution logic
(app/pipelines/text/orchestrator.py) — the single upstream source of the
"tone" value for every downstream consumer (the LangGraph path in
app/agents/text/nodes.py and both repurpose call sites in
app/pipelines/text/repurpose.py all just read whatever this function
produced).

A brand's persistent default_tone (My Voices > Calibration tab) is used
only when the caller didn't pass an explicit per-run tone override. The
subtle part: every real caller (text.py, text_stream.py) always
constructs an explicit ToneOverride object rather than passing None,
defaulting to ToneOverride.BRAND when the user picked nothing in the UI —
so "no override" must be recognised by tone.value == "brand", not by
tone being None. See tests/test_regenerate.py for the full HTTP-level
confirmation that this actually reaches the real generation prompt.
"""

from types import SimpleNamespace

from app.models.text import ToneOverride
from app.pipelines.text.orchestrator import _build_metadata


def _extras():
    return SimpleNamespace(
        hook_variations=True, hashtags=True, auto_cta=True, seo_meta=False,
        grammar_check=True, plagiarism_check=False, avoid_blacklist=True, pdf_export=False,
    )


def test_no_tone_no_default_resolves_to_brand():
    assert _build_metadata(_extras())["tone"] == "brand"


def test_explicit_brand_tone_with_default_set_uses_the_default():
    """The real-world shape every caller actually sends when the user
    picked nothing in the ToneSelector — ToneOverride.BRAND, not None."""
    out = _build_metadata(_extras(), tone=ToneOverride.BRAND, default_tone="professional")
    assert out["tone"] == "professional"


def test_explicit_non_brand_tone_wins_over_default():
    out = _build_metadata(_extras(), tone=ToneOverride.CASUAL, default_tone="professional")
    assert out["tone"] == "casual"


def test_default_tone_of_brand_is_a_no_op():
    out = _build_metadata(_extras(), tone=ToneOverride.BRAND, default_tone="brand")
    assert out["tone"] == "brand"


def test_none_tone_with_default_set_uses_the_default():
    """Defensive path — not hit by real callers today, but the None branch
    must behave the same as explicit ToneOverride.BRAND."""
    out = _build_metadata(_extras(), tone=None, default_tone="direct")
    assert out["tone"] == "direct"


# ─────────────────────────────────────────────────────────────────────────────
# /refine and /refine-chat — neither ever called build_tone_override() at
# all before this, so a brand's default_tone was silently ignored during
# chip refinement and chat refinement even though /generate, /regenerate,
# and /repurpose all respected it. Direct-render checks, no LLM/HTTP calls,
# same pattern as tests/test_tone_language_resonance.py.
# ─────────────────────────────────────────────────────────────────────────────

def test_refine_chat_system_prompt_applies_brand_default_tone():
    from app.pipelines.text.refiner import build_refinement_system

    out = build_refinement_system("BRAND CTX", "LinkedIn", [], "en", "professional")
    assert "TONE OVERRIDE" in out
    assert "professional, polished register" in out


def test_refine_chat_system_prompt_has_no_tone_override_when_default_unset():
    from app.pipelines.text.refiner import build_refinement_system

    out = build_refinement_system("BRAND CTX", "LinkedIn", [], "en", None)
    assert "TONE OVERRIDE" not in out


def test_refine_chat_default_tone_composes_with_non_english_language():
    from app.pipelines.text.refiner import build_refinement_system

    out = build_refinement_system("BRAND CTX", "LinkedIn", [], "ta", "casual")
    assert "Natural code-switching is expected" in out
    assert "Tamil" in out
