"""Tests for My Voices' Calibration/Training/Default endpoints.

These back the original mock BrandVoice page's own fields for real:
sliders, cadence attributes, formatting, channel rules, signature
phrases (calibration), writing samples (training), and one default
voice per workspace.
"""

from tests.conftest import create_workspace, invite_and_accept, signup_new_user


async def _create_brand(client, ws_id: str | None = None) -> str:
    headers = {"X-Workspace-Id": ws_id} if ws_id else {}
    res = await client.post("/api/v1/brand/", json={"brand_type": "Person"}, headers=headers)
    assert res.status_code in (200, 201), res.text
    return res.json()["brand_profile_id"]


# ─────────────────────────────────────────────────────────────────────────────
# Calibration
# ─────────────────────────────────────────────────────────────────────────────

async def test_update_calibration_persists_full_replace(api_client):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)

    calibration = {
        "formality": 80,
        "directness": 90,
        "humor": 10,
        "optimism": 60,
        "energy": 75,
        "sentence_length": "short",
        "paragraph_spacing": "dense",
        "vocabulary_level": "technical",
        "hook_aggressiveness": 95,
        "emoji_usage": "expressive",
        "allow_em_dashes": False,
        "allow_ellipses": True,
        "use_lowercase_bullets": False,
        "channel_rules": {"twitter": "Short punchy one-liners."},
        "signature_phrases": ["Let's be clear:"],
    }
    res = await api_client.patch(f"/api/v1/brand/{brand_id}/calibration", json={"calibration": calibration})
    assert res.status_code == 200, res.text
    assert res.json()["calibration"] == calibration

    res = await api_client.get(f"/api/v1/brand/{brand_id}")
    assert res.json()["calibration"] == calibration


async def test_update_calibration_404_for_nonexistent_brand(api_client):
    await signup_new_user(api_client)
    res = await api_client.patch(
        "/api/v1/brand/does-not-exist/calibration", json={"calibration": {}},
    )
    assert res.status_code == 404


async def test_new_brand_has_default_calibration(api_client):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    res = await api_client.get(f"/api/v1/brand/{brand_id}")
    assert res.json()["calibration"]["formality"] == 50
    assert res.json()["calibration"]["sentence_length"] == "balanced"


async def test_calibration_requires_edit_brand_voice_permission(api_client, make_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Calibration Perms", tier="large")
    brand_id = await _create_brand(api_client, ws_id)
    viewer_client, _ = await invite_and_accept(api_client, make_client, ws_id, "viewer")

    res = await viewer_client.patch(
        f"/api/v1/brand/{brand_id}/calibration",
        json={"calibration": {}},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 403


# ─────────────────────────────────────────────────────────────────────────────
# Training samples
# ─────────────────────────────────────────────────────────────────────────────

async def test_add_training_sample_computes_word_count_and_snippet(api_client):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)

    long_content = "word " * 50
    res = await api_client.post(
        f"/api/v1/brand/{brand_id}/training-samples",
        json={"title": "LinkedIn post draft", "source_type": "post", "content": long_content},
    )
    assert res.status_code == 201, res.text
    samples = res.json()["training_samples"]
    assert len(samples) == 1
    assert samples[0]["title"] == "LinkedIn post draft"
    assert samples[0]["word_count"] == 50
    assert samples[0]["extracted_traits"] == []
    assert samples[0]["snippet"].endswith("...")


async def test_add_training_sample_400_when_empty(api_client):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)

    res = await api_client.post(
        f"/api/v1/brand/{brand_id}/training-samples",
        json={"title": "Empty", "source_type": "notes", "content": "   "},
    )
    assert res.status_code == 400


async def test_delete_training_sample_removes_it(api_client):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)

    res = await api_client.post(
        f"/api/v1/brand/{brand_id}/training-samples",
        json={"title": "A sample", "source_type": "post", "content": "Some real content here."},
    )
    sample_id = res.json()["training_samples"][0]["id"]

    res = await api_client.delete(f"/api/v1/brand/{brand_id}/training-samples/{sample_id}")
    assert res.status_code == 200, res.text
    assert res.json()["training_samples"] == []


# ─────────────────────────────────────────────────────────────────────────────
# Default voice
# ─────────────────────────────────────────────────────────────────────────────

async def test_set_default_brand_clears_sibling(api_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Default Voice WS", tier="large")
    brand_a = await _create_brand(api_client, ws_id)
    brand_b = await _create_brand(api_client, ws_id)

    res = await api_client.patch(f"/api/v1/brand/{brand_a}/set-default", headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 200, res.text
    assert res.json()["is_default"] is True

    res = await api_client.patch(f"/api/v1/brand/{brand_b}/set-default", headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 200
    assert res.json()["is_default"] is True

    res = await api_client.get(f"/api/v1/brand/{brand_a}", headers={"X-Workspace-Id": ws_id})
    assert res.json()["is_default"] is False


async def test_set_default_brand_404_for_nonexistent(api_client):
    await signup_new_user(api_client)
    res = await api_client.patch("/api/v1/brand/does-not-exist/set-default")
    assert res.status_code == 404
