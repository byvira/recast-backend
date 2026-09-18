"""Tests for My Voices' Playground tab — POST /brand/{id}/preview-rewrite
and app.pipelines.brand.voice_playground.preview_rewrite_in_voice.

The Playground tab used to show a hardcoded 98.2% "tone match" for any
input. This is the real thing: rewrite sample text in the brand's actual
voice, with a real per-input tone-match estimate.

Runs with the LLM mocked — never a real Groq call.
"""

from app.pipelines.brand.voice_playground import preview_rewrite_in_voice
from tests.conftest import create_workspace, invite_and_accept, signup_new_user

_BRAND_DOC = {
    "brand_type": "Person",
    "identity": {"name": "Alex"},
    "audience": {},
    "voice_tone": {"tones": ["direct"], "style": "Short sentences."},
}


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests — preview_rewrite_in_voice orchestration
# ─────────────────────────────────────────────────────────────────────────────

async def test_preview_rewrite_returns_none_when_llm_returns_nothing_usable(monkeypatch):
    import app.pipelines.brand.voice_playground as vp_module

    async def fake_call_llm_structured(*args, **kwargs):
        return {}

    monkeypatch.setattr(vp_module, "call_llm_structured", fake_call_llm_structured)

    result = await preview_rewrite_in_voice(_BRAND_DOC, "Some sample text.")
    assert result is None


async def test_preview_rewrite_returns_none_when_llm_raises(monkeypatch):
    import app.pipelines.brand.voice_playground as vp_module

    async def fake_call_llm_structured(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(vp_module, "call_llm_structured", fake_call_llm_structured)

    result = await preview_rewrite_in_voice(_BRAND_DOC, "Some sample text.")
    assert result is None


async def test_preview_rewrite_clamps_out_of_range_score(monkeypatch):
    import app.pipelines.brand.voice_playground as vp_module

    async def fake_call_llm_structured(*args, **kwargs):
        return {"rewritten": "Rewritten text.", "tone_match_score": 250}

    monkeypatch.setattr(vp_module, "call_llm_structured", fake_call_llm_structured)

    result = await preview_rewrite_in_voice(_BRAND_DOC, "Some sample text.")
    assert result == {"rewritten": "Rewritten text.", "tone_match_score": 100}


async def test_preview_rewrite_strips_em_dashes(monkeypatch):
    import app.pipelines.brand.voice_playground as vp_module

    async def fake_call_llm_structured(*args, **kwargs):
        return {"rewritten": "This is bold — and true.", "tone_match_score": 80}

    monkeypatch.setattr(vp_module, "call_llm_structured", fake_call_llm_structured)

    result = await preview_rewrite_in_voice(_BRAND_DOC, "Some sample text.")
    assert "—" not in result["rewritten"]


# ─────────────────────────────────────────────────────────────────────────────
# Integration tests — POST /brand/{id}/preview-rewrite
# ─────────────────────────────────────────────────────────────────────────────

async def _create_brand(client, ws_id: str) -> str:
    res = await client.post(
        "/api/v1/brand/", json={"brand_type": "Person"}, headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code in (200, 201), res.text
    return res.json()["brand_profile_id"]


async def test_preview_rewrite_endpoint_returns_rewrite_and_score(signup_user, mock_llm):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Playground WS")
    brand_id = await _create_brand(client, ws_id)

    mock_llm.set_structured({"rewritten": "Rewritten in the brand voice.", "tone_match_score": 87})

    res = await client.post(
        f"/api/v1/brand/{brand_id}/preview-rewrite",
        json={"sample_text": "Some generic sample text to test."},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["rewritten"] == "Rewritten in the brand voice."
    assert body["tone_match_score"] == 87


async def test_preview_rewrite_400_on_empty_sample(signup_user, mock_llm):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Playground WS")
    brand_id = await _create_brand(client, ws_id)

    res = await client.post(
        f"/api/v1/brand/{brand_id}/preview-rewrite",
        json={"sample_text": "   "},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 400


async def test_preview_rewrite_404_for_nonexistent_brand(signup_user, mock_llm):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Playground WS")

    res = await client.post(
        "/api/v1/brand/does-not-exist/preview-rewrite",
        json={"sample_text": "Some text."},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 404


async def test_preview_rewrite_502_when_llm_returns_nothing_usable(signup_user, mock_llm):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Playground WS")
    brand_id = await _create_brand(client, ws_id)

    mock_llm.set_structured({})

    res = await client.post(
        f"/api/v1/brand/{brand_id}/preview-rewrite",
        json={"sample_text": "Some text."},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 502


async def test_preview_rewrite_blocked_for_viewer(signup_user, make_client):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Playground RBAC WS")
    brand_id = await _create_brand(client, ws_id)
    viewer_client, _ = await invite_and_accept(client, make_client, ws_id, "viewer")

    res = await viewer_client.post(
        f"/api/v1/brand/{brand_id}/preview-rewrite",
        json={"sample_text": "Some text."},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 403
