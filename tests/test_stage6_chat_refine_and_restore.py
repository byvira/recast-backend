"""Tests for Module 2 Stage 6 — confirming chat-refine and version-restore
actually work end-to-end now that a real piece_id exists for SSE-generated
content (Stage 1's persistence fix).

Before Stage 1, RefineDrawer.tsx's own guard ("Content not yet saved — run
the pipeline first to enable chat refinement") was permanently true for
every live-generated card, since piece_id was always "". Both endpoints
were already correctly built and are exercised here for real, with the
LLM mocked — never a real Groq call.
"""

from uuid import uuid4

from app.db.mongo import workspaces
from app.pipelines.text import refiner as refiner_module
from app.pipelines.text.storage import ensure_session_exists, save_live_piece
from tests.conftest import create_workspace


async def _create_brand(client, ws_id: str) -> str:
    res = await client.post(
        "/api/v1/brand/", json={"brand_type": "Person"}, headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code in (200, 201), res.text
    return res.json()["brand_profile_id"]


async def _seed_piece(workspace_id: str, user_id: str, brand_id: str) -> str:
    session_id = str(uuid4())
    await ensure_session_exists(
        session_id=session_id, workspace_id=workspace_id, user_id=user_id,
        brand_id=brand_id, source_type="text",
    )
    return await save_live_piece(
        session_id=session_id, workspace_id=workspace_id, user_id=user_id,
        brand_id=brand_id, platform="LinkedIn",
        content="Original content, version 1.", word_count=4, char_count=28,
    )


async def test_refine_chat_saves_a_real_version_for_a_real_piece(signup_user, mock_llm):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Chat Refine WS")
    brand_id = await _create_brand(client, ws_id)
    piece_id = await _seed_piece(ws_id, profile["id"], brand_id)

    mock_llm.set_chat("Refined via chat turn 1.")

    res = await client.post(
        "/api/v1/text/refine-chat",
        json={
            "messages": [{"role": "user", "content": "Make it punchier"}],
            "platform": "LinkedIn",
            "brand_id": brand_id,
            "piece_id": piece_id,
        },
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["refined"] == "Refined via chat turn 1."
    assert body["version_saved"] is True


async def test_refine_chat_preserves_workspace_language(signup_user, monkeypatch):
    """/refine-chat had no language awareness at all — same gap as /refine.
    Sets the workspace's language explicitly (English content, so a pass
    can only mean the workspace setting was actually read) and captures
    the real system prompt sent to the LLM (not the shared mock_llm
    fixture, which doesn't expose it) to confirm the fix."""
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Chat Refine Language WS")
    await workspaces.update_one({"id": ws_id}, {"$set": {"language": "ta"}})
    brand_id = await _create_brand(client, ws_id)
    piece_id = await _seed_piece(ws_id, profile["id"], brand_id)

    captured: dict[str, str] = {}

    async def _fake_call_llm_chat(messages, *args, system: str = "", **kwargs):
        captured["system"] = system
        return "Refined via chat turn 1."

    monkeypatch.setattr(refiner_module, "call_llm_chat", _fake_call_llm_chat)

    res = await client.post(
        "/api/v1/text/refine-chat",
        json={
            "messages": [{"role": "user", "content": "Make it punchier"}],
            "platform": "LinkedIn",
            "brand_id": brand_id,
            "piece_id": piece_id,
        },
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text
    assert "Tamil" in captured["system"]

    piece = await client.get(
        f"/api/v1/content/pieces/{piece_id}", headers={"X-Workspace-Id": ws_id},
    )
    assert piece.json()["content"] == "Refined via chat turn 1."
    assert piece.json()["version_count"] == 2

    versions = await client.get(
        f"/api/v1/content/pieces/{piece_id}/versions", headers={"X-Workspace-Id": ws_id},
    )
    version_list = versions.json()["versions"]
    assert len(version_list) == 2
    assert version_list[-1]["action"] == "chat_turn_1"


async def test_refine_chat_multi_turn_versions_accumulate(signup_user, mock_llm):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Chat Refine WS")
    brand_id = await _create_brand(client, ws_id)
    piece_id = await _seed_piece(ws_id, profile["id"], brand_id)

    mock_llm.set_chat("Turn 1 result.")
    res1 = await client.post(
        "/api/v1/text/refine-chat",
        json={
            "messages": [{"role": "user", "content": "shorten it"}],
            "platform": "LinkedIn",
            "brand_id": brand_id,
            "piece_id": piece_id,
        },
        headers={"X-Workspace-Id": ws_id},
    )
    assert res1.status_code == 200, res1.text

    mock_llm.set_chat("Turn 2 result.")
    res2 = await client.post(
        "/api/v1/text/refine-chat",
        json={
            "messages": [
                {"role": "user", "content": "shorten it"},
                {"role": "assistant", "content": "Turn 1 result."},
                {"role": "user", "content": "now add a CTA"},
            ],
            "platform": "LinkedIn",
            "brand_id": brand_id,
            "piece_id": piece_id,
        },
        headers={"X-Workspace-Id": ws_id},
    )
    assert res2.status_code == 200, res2.text
    assert res2.json()["refined"] == "Turn 2 result."

    piece = await client.get(
        f"/api/v1/content/pieces/{piece_id}", headers={"X-Workspace-Id": ws_id},
    )
    assert piece.json()["content"] == "Turn 2 result."
    assert piece.json()["version_count"] == 3


async def test_restore_version_creates_a_forward_version_not_a_literal_revert(signup_user, mock_llm):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Restore WS")
    brand_id = await _create_brand(client, ws_id)
    piece_id = await _seed_piece(ws_id, profile["id"], brand_id)

    # Create version 2 via a real edit.
    edit_res = await client.patch(
        f"/api/v1/content/pieces/{piece_id}",
        json={"content": "Version 2 content."},
        headers={"X-Workspace-Id": ws_id},
    )
    assert edit_res.status_code == 200, edit_res.text

    # Restore back to version 1.
    restore_res = await client.post(
        f"/api/v1/content/pieces/{piece_id}/restore/1",
        headers={"X-Workspace-Id": ws_id},
    )
    assert restore_res.status_code == 200, restore_res.text
    assert restore_res.json()["content"] == "Original content, version 1."

    piece = await client.get(
        f"/api/v1/content/pieces/{piece_id}", headers={"X-Workspace-Id": ws_id},
    )
    body = piece.json()
    assert body["content"] == "Original content, version 1."
    # Forward-only: restoring v1 creates v3, it doesn't delete v2 or rewind
    # the counter — confirms the append-style restore design for real.
    assert body["version_count"] == 3

    versions = await client.get(
        f"/api/v1/content/pieces/{piece_id}/versions", headers={"X-Workspace-Id": ws_id},
    )
    version_list = versions.json()["versions"]
    assert len(version_list) == 3
    assert version_list[-1]["action"] == "restored_from_v1"
    assert version_list[-1]["content"] == "Original content, version 1."
    # v2 is still there, untouched, in the history.
    assert version_list[1]["content"] == "Version 2 content."
