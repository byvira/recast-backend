"""Tests for the real default tone instruction added to
app/prompts/fragments/brand_context.jinja.

A brand profile with no voice_tone.tones/style configured used to inject
zero tone guidance into the generation prompt at all — build_tone_override()
(app/pipelines/text/brand_context.py) also contributes nothing when
tone == "brand" (the default), so an unconfigured brand's every generation
had no tone signal whatsoever, which reads as generic/stiff. Both real
text-generation document_shape branches (text_pipeline, agent_node) now
inject a natural/conversational default in that case — this is template
rendering only, no LLM call, no DB, no HTTP.
"""

from app.pipelines.text.brand_context import build_brand_context

FALLBACK_MARKER = "No tone/style has been explicitly configured"


def test_text_pipeline_shape_injects_fallback_when_voice_tone_unset():
    out = build_brand_context({"brand_type": "Person", "identity": {"name": "Test"}})
    assert FALLBACK_MARKER in out


def test_text_pipeline_shape_omits_fallback_when_voice_tone_set():
    out = build_brand_context({
        "brand_type": "Person",
        "identity": {"name": "Test"},
        "voice_tone": {"tones": ["direct"], "style": "punchy"},
    })
    assert FALLBACK_MARKER not in out
    assert "Tone: direct" in out
    assert "Style directives: punchy" in out


def test_text_pipeline_shape_omits_fallback_when_only_style_set():
    """Style alone (no tones list) is still a real, explicit configuration
    — must not also get the fallback appended on top."""
    out = build_brand_context({
        "brand_type": "Person",
        "identity": {"name": "Test"},
        "voice_tone": {"style": "punchy, no fluff"},
    })
    assert FALLBACK_MARKER not in out
    assert "Style directives: punchy, no fluff" in out


def test_agent_node_shape_injects_fallback_when_voice_tone_unset():
    from app.prompts.registry import load_prompt

    out = load_prompt(
        "fragments/brand_context",
        document_shape="agent_node",
        brand_profile={"brand_type": "Person", "identity": {"name": "Test"}},
    )
    assert FALLBACK_MARKER in out


def test_agent_node_shape_omits_fallback_when_voice_tone_set():
    from app.prompts.registry import load_prompt

    out = load_prompt(
        "fragments/brand_context",
        document_shape="agent_node",
        brand_profile={
            "brand_type": "Person",
            "identity": {"name": "Test"},
            "voice_tone": {"tones": ["direct"], "style": "punchy"},
        },
    )
    assert FALLBACK_MARKER not in out
