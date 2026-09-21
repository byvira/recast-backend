"""Tests for POST /api/v1/text/regenerate:
  - The piece_id bug fixed alongside Module 2 Stage 5 (Retry):
    RegenerateResponse.piece_id used to always be "" even though the piece
    really was saved (GeneratedPiece has no piece_id field of its own —
    same root cause Stage 1 fixed for the SSE path, confirmed
    independently present here too), because _save_result() discarded the
    real, storage-generated piece_id instead of returning it.
  - Stage 7's product decision (Decision 1): regenerating an existing
    piece (piece_id given) creates a new VERSION of that same piece, the
    same mechanism chips/chat-refine/manual-edit all already use — not a
    disconnected second piece with no link back to the card the user
    clicked regenerate on. Falls back to a brand-new piece only when
    there's genuinely nothing to version onto (no piece_id given).

Runs the real pipeline end to end with the LLM mocked — never a real
Groq call.
"""

from uuid import uuid4

from app.db.mongo import brand_profiles, workspaces
from app.pipelines.text import generator as generator_module
from app.pipelines.text.storage import ensure_session_exists, get_piece, save_live_piece
from tests.conftest import create_workspace


async def _create_brand(client, ws_id: str) -> str:
    res = await client.post(
        "/api/v1/brand/", json={"brand_type": "Person"}, headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code in (200, 201), res.text
    brand_id = res.json()["brand_profile_id"]
    # /regenerate (unlike score-hook) requires a *complete* brand profile —
    # flip it directly rather than driving the full onboarding flow, which
    # is irrelevant to what this test is actually checking.
    await brand_profiles.update_one({"id": brand_id}, {"$set": {"is_complete": True}})
    return brand_id


async def _seed_piece(workspace_id: str, user_id: str, brand_id: str) -> str:
    session_id = str(uuid4())
    await ensure_session_exists(
        session_id=session_id, workspace_id=workspace_id, user_id=user_id,
        brand_id=brand_id, source_type="text",
    )
    return await save_live_piece(
        session_id=session_id, workspace_id=workspace_id, user_id=user_id,
        brand_id=brand_id, platform="LinkedIn",
        content="Original content before regenerate.", word_count=4, char_count=34,
    )


async def test_regenerate_without_piece_id_creates_a_new_piece(signup_user, mock_llm):
    """No existing piece to version onto — falls back to a brand-new one,
    same as before Stage 7. Also the case that originally caught the
    empty-piece_id bug."""
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Regenerate WS")
    brand_id = await _create_brand(client, ws_id)

    mock_llm.set_plain("Regenerated content, real and persisted this time.")
    mock_llm.set_structured({})

    res = await client.post(
        "/api/v1/text/regenerate",
        json={
            "platform": "LinkedIn",
            "brand_id": brand_id,
            "content": "Some original source content to regenerate from.",
        },
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["piece_id"], "piece_id is empty — the bug is back"

    # Independently confirm it's a real, fetchable, workspace-scoped piece —
    # not just a non-empty string in the response.
    piece = await get_piece(body["piece_id"], ws_id)
    assert piece is not None
    assert piece["platform"] == "LinkedIn"
    assert piece["version_count"] == 1


async def test_regenerate_with_piece_id_creates_a_new_version_of_the_same_piece(signup_user, mock_llm):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Regenerate WS")
    brand_id = await _create_brand(client, ws_id)
    piece_id = await _seed_piece(ws_id, profile["id"], brand_id)

    mock_llm.set_plain("Freshly regenerated content for the same piece.")
    mock_llm.set_structured({})

    res = await client.post(
        "/api/v1/text/regenerate",
        json={
            "platform": "LinkedIn",
            "brand_id": brand_id,
            "piece_id": piece_id,
            "content": "Original content before regenerate.",
        },
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text
    body = res.json()

    # Same piece_id back — not a disconnected new one.
    assert body["piece_id"] == piece_id

    piece = await get_piece(piece_id, ws_id)
    assert piece is not None
    assert piece["content"] == "Freshly regenerated content for the same piece."
    assert piece["version_count"] == 2

    versions_res = await client.get(
        f"/api/v1/content/pieces/{piece_id}/versions", headers={"X-Workspace-Id": ws_id},
    )
    version_list = versions_res.json()["versions"]
    assert len(version_list) == 2
    assert version_list[-1]["action"] == "regenerated"
    assert version_list[-1]["content"] == "Freshly regenerated content for the same piece."
    # v1 is still there, untouched.
    assert version_list[0]["content"] == "Original content before regenerate."


async def test_regenerate_preserves_source_language(signup_user, monkeypatch):
    """Regenerate used to never resolve a language at all, silently falling
    through to run_text_pipeline's own "en" default regardless of any
    workspace/user language setting — e.g. a workspace configured for
    Tamil would still get English back on Retry. Sets the workspace's
    language explicitly (English source content, so this can only be
    passing because the workspace setting was actually read — not because
    detect_language happened to guess Tamil from the input). Captures the
    real prompt sent to the LLM (not the shared mock_llm fixture, which
    doesn't expose it) to confirm the fix."""
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Regenerate WS")
    await workspaces.update_one({"id": ws_id}, {"$set": {"language": "ta"}})
    brand_id = await _create_brand(client, ws_id)

    captured: dict[str, str] = {}

    async def _fake_call_llm_structured(prompt: str, *args, **kwargs):
        captured["prompt"] = prompt
        return {"content": "Regenerated content.", "platform": "LinkedIn"}

    monkeypatch.setattr(generator_module, "call_llm_structured", _fake_call_llm_structured)

    res = await client.post(
        "/api/v1/text/regenerate",
        json={"platform": "LinkedIn", "brand_id": brand_id, "content": "Plain English source content."},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text
    assert "Tamil" in captured["prompt"]


async def test_regenerate_applies_brand_default_tone_when_none_picked(signup_user, monkeypatch):
    """My Voices > Calibration tab's persistent default tone
    (BrandProfile.default_tone) — regenerate's request model defaults
    `tone` to the string "brand" whenever the caller doesn't pick one
    (app/models/text.py RegenerateRequest.tone), which the handler always
    converts to an explicit ToneOverride.BRAND, never None
    (app/api/v1/text.py:737). _build_metadata() must recognise that as
    "no explicit override" and fall through to the brand's own
    default_tone rather than silently ignoring it — confirmed here by
    capturing the real prompt sent to the LLM and asserting the
    Professional TONE OVERRIDE text actually appears."""
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Regenerate WS")
    brand_id = await _create_brand(client, ws_id)
    res = await client.patch(
        f"/api/v1/brand/{brand_id}/voice",
        json={"default_tone": "professional"},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text

    captured: dict[str, str] = {}

    async def _fake_call_llm_structured(prompt: str, *args, **kwargs):
        captured["prompt"] = prompt
        return {"content": "Regenerated content.", "platform": "LinkedIn"}

    monkeypatch.setattr(generator_module, "call_llm_structured", _fake_call_llm_structured)

    res = await client.post(
        "/api/v1/text/regenerate",
        json={"platform": "LinkedIn", "brand_id": brand_id, "content": "Some source content."},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text
    assert "TONE OVERRIDE" in captured["prompt"]
    assert "professional, polished register" in captured["prompt"]


async def test_regenerate_explicit_tone_wins_over_brand_default(signup_user, monkeypatch):
    """An explicit per-run tone override must still take priority over the
    brand's persistent default, not be silently replaced by it."""
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Regenerate WS")
    brand_id = await _create_brand(client, ws_id)
    await client.patch(
        f"/api/v1/brand/{brand_id}/voice",
        json={"default_tone": "professional"},
        headers={"X-Workspace-Id": ws_id},
    )

    captured: dict[str, str] = {}

    async def _fake_call_llm_structured(prompt: str, *args, **kwargs):
        captured["prompt"] = prompt
        return {"content": "Regenerated content.", "platform": "LinkedIn"}

    monkeypatch.setattr(generator_module, "call_llm_structured", _fake_call_llm_structured)

    res = await client.post(
        "/api/v1/text/regenerate",
        json={
            "platform": "LinkedIn", "brand_id": brand_id, "content": "Some source content.",
            "tone": "casual",
        },
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text
    assert "casual, conversational tone" in captured["prompt"]
    assert "professional, polished register" not in captured["prompt"]
