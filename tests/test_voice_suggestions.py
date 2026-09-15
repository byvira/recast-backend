"""Tests for AI-drafted voice-pattern suggestions (onboarding wizard step 6).

Two layers:
  - Unit tests for the pure context-summary and response-cleaning helpers
    in app.pipelines.brand.voice_suggestions — no I/O, no mocking, fast.
  - Integration tests for POST /brand/{id}/suggest-voice-patterns, with
    generate_voice_pattern_suggestions monkeypatched — same pattern this
    suite already uses for send_templated_email, so no real LLM call ever
    runs in CI.
"""

import app.api.v1.brand as brand_module
from app.pipelines.brand.voice_suggestions import (
    _audience_summary,
    _clean_phrases,
    _clean_string_list,
    _identity_summary,
    _tone_summary,
    generate_voice_pattern_suggestions,
)
from tests.conftest import create_workspace, invite_and_accept, signup_new_user


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests — context summarisation
# ─────────────────────────────────────────────────────────────────────────────

def test_identity_summary_person():
    line = _identity_summary(
        "Person", {"name": "Jane Doe", "profession": "Tech Writer", "bio": "Writes about dev tools."}
    )
    assert "Jane Doe" in line
    assert "Tech Writer" in line
    assert "Writes about dev tools." in line


def test_identity_summary_business_reads_normalised_company_name():
    # company_name (post-normalise_brand_keys), not the camelCase companyName
    # the frontend sends — this is exactly the mismatch fixed in
    # brand_context.jinja; the summary builder must read the same key.
    line = _identity_summary(
        "Business", {"company_name": "Acme Corp", "industry": "FinTech"}
    )
    assert "Acme Corp" in line
    assert "FinTech" in line


def test_identity_summary_business_falls_back_to_camelcase():
    line = _identity_summary("Business", {"companyName": "Acme Corp"})
    assert "Acme Corp" in line


def test_identity_summary_product_reads_normalised_product_name():
    line = _identity_summary("Product", {"product_name": "Recast", "description": "Repurposing engine"})
    assert "Recast" in line
    assert "Repurposing engine" in line


def test_identity_summary_empty_identity_returns_empty_string():
    assert _identity_summary("Person", {}) == ""
    assert _identity_summary("Person", None) == ""  # type: ignore[arg-type]


def test_audience_summary_combines_pain_point_and_reading_level():
    line = _audience_summary({"primary_pain_point": "too much noise online", "reading_level": "Standard"})
    assert "too much noise online" in line
    assert "standard" in line.lower()


def test_tone_summary_joins_tones_humor_emoji():
    line = _tone_summary({"tones": ["Direct", "Warm"], "humor": "Subtle", "emoji": "Sometimes"})
    assert "Direct, Warm" in line
    assert "Subtle" in line
    assert "Sometimes" in line


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests — response cleaning (never trust a raw LLM dict)
# ─────────────────────────────────────────────────────────────────────────────

def test_clean_string_list_drops_non_strings_and_blanks():
    assert _clean_string_list(["a", "", "  ", 5, None, "b"], limit=10) == ["a", "b"]


def test_clean_string_list_caps_at_limit():
    assert _clean_string_list(["a", "b", "c", "d"], limit=2) == ["a", "b"]


def test_clean_string_list_rejects_non_list():
    assert _clean_string_list("not a list", limit=5) == []
    assert _clean_string_list(None, limit=5) == []


def test_clean_phrases_drops_blank_text_and_defaults_bad_placement():
    raw = [
        {"text": "Let's go", "placement": "hook"},
        {"text": "", "placement": "hook"},  # blank text dropped
        {"text": "Ship it", "placement": "not-a-real-placement"},  # coerced to "any"
        "not a dict",  # dropped
        {"text": "  spaced  ", "placement": "close"},
    ]
    cleaned = _clean_phrases(raw, limit=10)
    assert cleaned == [
        {"text": "Let's go", "placement": "hook"},
        {"text": "Ship it", "placement": "any"},
        {"text": "spaced", "placement": "close"},
    ]


def test_clean_phrases_caps_at_limit():
    raw = [{"text": f"phrase {i}", "placement": "any"} for i in range(10)]
    assert len(_clean_phrases(raw, limit=3)) == 3


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests — generate_voice_pattern_suggestions orchestration
# ─────────────────────────────────────────────────────────────────────────────

async def test_generate_returns_none_when_llm_returns_empty_dict(monkeypatch):
    import app.pipelines.brand.voice_suggestions as vs_module

    async def fake_call_llm_structured(*args, **kwargs):
        return {}

    monkeypatch.setattr(vs_module, "call_llm_structured", fake_call_llm_structured)

    result = await generate_voice_pattern_suggestions(
        {"brand_type": "Person", "identity": {"name": "A"}, "audience": {}, "voice_tone": {}}
    )
    assert result is None


async def test_generate_returns_none_when_llm_raises(monkeypatch):
    import app.pipelines.brand.voice_suggestions as vs_module

    async def fake_call_llm_structured(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(vs_module, "call_llm_structured", fake_call_llm_structured)

    result = await generate_voice_pattern_suggestions(
        {"brand_type": "Person", "identity": {"name": "A"}, "audience": {}, "voice_tone": {}}
    )
    assert result is None


async def test_generate_cleans_and_returns_valid_suggestions(monkeypatch):
    import app.pipelines.brand.voice_suggestions as vs_module

    async def fake_call_llm_structured(*args, **kwargs):
        return {
            "openers": ["Here's the thing nobody tells you:", "", 5],
            "closers": ["Build carefully, ship often."],
            "phrases": [{"text": "source code fidelity", "placement": "hook"}],
        }

    monkeypatch.setattr(vs_module, "call_llm_structured", fake_call_llm_structured)

    result = await generate_voice_pattern_suggestions(
        {"brand_type": "Person", "identity": {"name": "A"}, "audience": {}, "voice_tone": {}}
    )
    assert result == {
        "openers": ["Here's the thing nobody tells you:"],
        "closers": ["Build carefully, ship often."],
        "phrases": [{"text": "source code fidelity", "placement": "hook"}],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Integration tests — POST /brand/{id}/suggest-voice-patterns
# ─────────────────────────────────────────────────────────────────────────────

_FAKE_SUGGESTIONS = {
    "openers": ["Opener one.", "Opener two."],
    "closers": ["Closer one."],
    "phrases": [{"text": "signature phrase", "placement": "any"}],
}


async def test_suggest_voice_patterns_requires_edit_brand_voice_permission(api_client, make_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Suggest Perms", tier="large")
    viewer_client, _ = await invite_and_accept(api_client, make_client, ws_id, "viewer")

    res = await api_client.post("/api/v1/brand/", json={"brand_type": "Person"}, headers={"X-Workspace-Id": ws_id})
    brand_id = res.json()["brand_profile_id"]

    res = await viewer_client.post(
        f"/api/v1/brand/{brand_id}/suggest-voice-patterns", headers={"X-Workspace-Id": ws_id}
    )
    assert res.status_code == 403


async def test_suggest_voice_patterns_404_for_unknown_brand(api_client):
    await signup_new_user(api_client)
    res = await api_client.post("/api/v1/brand/does-not-exist/suggest-voice-patterns")
    assert res.status_code == 404


async def test_suggest_voice_patterns_400_without_identity(api_client):
    await signup_new_user(api_client)
    res = await api_client.post("/api/v1/brand/", json={"brand_type": "Person"})
    brand_id = res.json()["brand_profile_id"]

    res = await api_client.post(f"/api/v1/brand/{brand_id}/suggest-voice-patterns")
    assert res.status_code == 400


async def test_suggest_voice_patterns_happy_path(api_client, monkeypatch):
    async def fake_generate(brand_profile):
        assert brand_profile["identity"]["name"] == "Jane Doe"
        return _FAKE_SUGGESTIONS

    monkeypatch.setattr(brand_module, "generate_voice_pattern_suggestions", fake_generate)

    await signup_new_user(api_client)
    res = await api_client.post("/api/v1/brand/", json={"brand_type": "Person"})
    brand_id = res.json()["brand_profile_id"]

    await api_client.put(
        f"/api/v1/brand/{brand_id}/step",
        json={"step": 2, "data": {"name": "Jane Doe", "bio": "x", "profession": "y"}},
    )

    res = await api_client.post(f"/api/v1/brand/{brand_id}/suggest-voice-patterns")
    assert res.status_code == 200
    assert res.json() == _FAKE_SUGGESTIONS

    # Never written to the profile — still exactly what step 2 saved.
    res = await api_client.get(f"/api/v1/brand/{brand_id}")
    assert res.json()["manual_data"] is None


async def test_suggest_voice_patterns_502_when_generation_fails(api_client, monkeypatch):
    async def fake_generate(brand_profile):
        return None

    monkeypatch.setattr(brand_module, "generate_voice_pattern_suggestions", fake_generate)

    await signup_new_user(api_client)
    res = await api_client.post("/api/v1/brand/", json={"brand_type": "Person"})
    brand_id = res.json()["brand_profile_id"]
    await api_client.put(f"/api/v1/brand/{brand_id}/step", json={"step": 2, "data": {"name": "A"}})

    res = await api_client.post(f"/api/v1/brand/{brand_id}/suggest-voice-patterns")
    assert res.status_code == 502
