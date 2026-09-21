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

from app.pipelines.text.brand_context import build_brand_context, build_tone_override

FALLBACK_MARKER = "No tone/style has been explicitly configured"
INACTIVE_MARKER = "This brand voice has been turned off"


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


# ─────────────────────────────────────────────────────────────────────────────
# is_active — a disabled brand's specific voice must never reach the prompt,
# but the pipeline must still produce a full, sensible generic-voice context
# rather than erroring or emitting nothing.
# ─────────────────────────────────────────────────────────────────────────────

def test_text_pipeline_shape_suppresses_identity_when_inactive():
    out = build_brand_context({
        "brand_type": "Person",
        "identity": {"name": "Jane Doe", "profession": "Consultant"},
        "voice_tone": {"tones": ["direct"], "style": "punchy"},
        "is_active": False,
    })
    assert "Jane Doe" not in out
    assert "Consultant" not in out
    assert INACTIVE_MARKER in out


def test_text_pipeline_shape_renders_identity_when_active_key_missing():
    """No migration needed for existing brands — a doc with no is_active
    key at all must behave exactly like is_active=True."""
    out = build_brand_context({
        "brand_type": "Person",
        "identity": {"name": "Jane Doe"},
    })
    assert "Jane Doe" in out
    assert INACTIVE_MARKER not in out


def test_agent_node_shape_suppresses_identity_when_inactive():
    from app.prompts.registry import load_prompt

    out = load_prompt(
        "fragments/brand_context",
        document_shape="agent_node",
        brand_profile={
            "brand_type": "Person",
            "identity": {"name": "Jane Doe", "profession": "Consultant"},
            "voice_tone": {"tones": ["direct"], "style": "punchy"},
            "is_active": False,
        },
    )
    assert "Jane Doe" not in out
    assert "Consultant" not in out
    assert INACTIVE_MARKER in out


# ─────────────────────────────────────────────────────────────────────────────
# New real-world tone overrides (Professional, Direct) — additive to the
# existing formal/casual/punchy/storytelling set.
# ─────────────────────────────────────────────────────────────────────────────

def test_professional_tone_override_renders():
    out = build_tone_override("professional")
    assert "TONE OVERRIDE" in out
    assert "professional" in out.lower()


def test_direct_tone_override_renders():
    out = build_tone_override("direct")
    assert "TONE OVERRIDE" in out
    assert "literal" in out.lower()


def test_professional_tone_survives_alongside_a_non_english_language():
    """app/prompts/text/generate/master.jinja renders language_instruction
    and tone_override_text as two independent, unconditional placeholders
    (see master.jinja lines 2 and 5) — neither one's presence gates the
    other. Confirms the "works for every language, not just one" requirement
    by composing both the way master.jinja actually does, rather than
    trusting that independence by inspection alone."""
    from app.pipelines.text.generator import build_language_instruction

    tone_text = build_tone_override("professional")
    language_text = build_language_instruction("ta")
    composed = f"{language_text}\n{tone_text}"

    assert "Tamil" in composed
    assert "professional" in composed.lower()
