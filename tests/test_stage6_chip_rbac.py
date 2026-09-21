"""Tests for Module 2 Stage 6:
  - The Decision-2 RBAC fix: score-hook, score-readability, and the chips
    list used to require only workspace membership — a viewer could
    trigger real LLM calls (score-hook) with no gate at all. All three now
    require create_content, same as every other content-generation-
    adjacent action.
  - Chip-refine's version-save behavior, exercised for real (frontend now
    calls this for real via applyChip in useSSE-text.ts, instead of only
    ever being reachable from the orphaned PlatformOutputCard.tsx).

Runs with the LLM mocked — never a real Groq call.
"""

from uuid import uuid4

from app.db.mongo import workspaces
from app.pipelines.text import chips as chips_module
from app.pipelines.text.storage import ensure_session_exists, save_live_piece
from tests.conftest import create_workspace, invite_and_accept


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
        content="Original content before any chip is applied.", word_count=6, char_count=45,
    )


# ─────────────────────────────────────────────────────────────────────────────
# RBAC — viewer blocked, editor allowed
# ─────────────────────────────────────────────────────────────────────────────

async def test_score_hook_blocked_for_viewer(signup_user, make_client, mock_llm):
    owner_client, owner_profile = await signup_user()
    ws_id = await create_workspace(owner_client, "RBAC WS")
    brand_id = await _create_brand(owner_client, ws_id)
    viewer_client, _ = await invite_and_accept(owner_client, make_client, ws_id, "viewer")

    res = await viewer_client.post(
        "/api/v1/text/score-hook",
        json={"content": "content", "platform": "LinkedIn", "brand_id": brand_id},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 403


async def test_score_readability_blocked_for_viewer(signup_user, make_client):
    owner_client, _ = await signup_user()
    ws_id = await create_workspace(owner_client, "RBAC WS")
    viewer_client, _ = await invite_and_accept(owner_client, make_client, ws_id, "viewer")

    res = await viewer_client.post(
        "/api/v1/text/score-readability",
        json={"content": "Some content to score.", "platform": "LinkedIn"},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 403


async def test_chips_list_blocked_for_viewer(signup_user, make_client):
    owner_client, _ = await signup_user()
    ws_id = await create_workspace(owner_client, "RBAC WS")
    viewer_client, _ = await invite_and_accept(owner_client, make_client, ws_id, "viewer")

    res = await viewer_client.get(
        "/api/v1/text/chips?platform=LinkedIn",
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 403


async def test_score_hook_allowed_for_editor(signup_user, make_client, mock_llm):
    owner_client, _ = await signup_user()
    ws_id = await create_workspace(owner_client, "RBAC WS")
    brand_id = await _create_brand(owner_client, ws_id)
    editor_client, _ = await invite_and_accept(owner_client, make_client, ws_id, "editor")

    mock_llm.set_structured({"current_score": 60, "current_reason": "ok", "alternatives": []})
    res = await editor_client.post(
        "/api/v1/text/score-hook",
        json={"content": "content", "platform": "LinkedIn", "brand_id": brand_id},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text


async def test_chips_list_allowed_for_editor(signup_user, make_client):
    owner_client, _ = await signup_user()
    ws_id = await create_workspace(owner_client, "RBAC WS")
    editor_client, _ = await invite_and_accept(owner_client, make_client, ws_id, "editor")

    res = await editor_client.get(
        "/api/v1/text/chips?platform=LinkedIn",
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text
    assert len(res.json()["chips"]) > 0


# ─────────────────────────────────────────────────────────────────────────────
# Chip apply — real version save
# ─────────────────────────────────────────────────────────────────────────────

async def test_apply_chip_saves_a_real_new_version(signup_user, mock_llm):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Chip WS")
    brand_id = await _create_brand(client, ws_id)
    piece_id = await _seed_piece(ws_id, profile["id"], brand_id)

    mock_llm.set_plain("Punchier, shorter version of the content.")

    res = await client.post(
        "/api/v1/text/refine",
        json={
            "content": "Original content before any chip is applied.",
            "chip": "shorten",
            "platform": "LinkedIn",
            "brand_id": brand_id,
            "piece_id": piece_id,
        },
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["changed"] is True
    assert body["refined"] == "Punchier, shorter version of the content."

    piece = await client.get(
        f"/api/v1/content/pieces/{piece_id}", headers={"X-Workspace-Id": ws_id},
    )
    assert piece.status_code == 200
    assert piece.json()["content"] == "Punchier, shorter version of the content."
    assert piece.json()["version_count"] == 2

    versions = await client.get(
        f"/api/v1/content/pieces/{piece_id}/versions", headers={"X-Workspace-Id": ws_id},
    )
    version_list = versions.json()["versions"]
    assert len(version_list) == 2
    assert version_list[-1]["action"] == "shorten"


# ─────────────────────────────────────────────────────────────────────────────
# Custom chips (Feature 5) — a user-authored instruction, not one of the
# fixed CHIP_PROMPTS names
# ─────────────────────────────────────────────────────────────────────────────

async def test_apply_custom_chip_bypasses_the_fixed_chip_set(signup_user, mock_llm):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Custom Chip WS")
    brand_id = await _create_brand(client, ws_id)
    piece_id = await _seed_piece(ws_id, profile["id"], brand_id)

    mock_llm.set_plain("Rewritten like a scrappy founder at 2am.")

    res = await client.post(
        "/api/v1/text/refine",
        json={
            "content": "Original content before any chip is applied.",
            "chip": "Sound like a scrappy founder",
            "custom_instruction": "Sound like a scrappy founder writing at 2am.",
            "platform": "LinkedIn",
            "brand_id": brand_id,
            "piece_id": piece_id,
        },
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["refined"] == "Rewritten like a scrappy founder at 2am."

    versions = await client.get(
        f"/api/v1/content/pieces/{piece_id}/versions", headers={"X-Workspace-Id": ws_id},
    )
    version_list = versions.json()["versions"]
    assert version_list[-1]["action"] == "Sound like a scrappy founder"
    assert version_list[-1]["instruction"] == "Sound like a scrappy founder writing at 2am."


async def test_refine_preserves_workspace_language(signup_user, monkeypatch):
    """/refine had no language awareness at all — apply_chip() couldn't
    even accept one. Sets the workspace's language explicitly (English
    content, so a pass can only mean the workspace setting was actually
    read) and captures the real prompt sent to the LLM (not the shared
    mock_llm fixture, which doesn't expose it) to confirm the fix."""
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Chip Language WS")
    await workspaces.update_one({"id": ws_id}, {"$set": {"language": "ta"}})
    brand_id = await _create_brand(client, ws_id)
    piece_id = await _seed_piece(ws_id, profile["id"], brand_id)

    captured: dict[str, str] = {}

    async def _fake_call_llm(prompt: str, *args, **kwargs):
        captured["prompt"] = prompt
        return "Refined content."

    monkeypatch.setattr(chips_module, "call_llm", _fake_call_llm)

    res = await client.post(
        "/api/v1/text/refine",
        json={
            "content": "Original content before any chip is applied.",
            "chip": "shorten",
            "platform": "LinkedIn",
            "brand_id": brand_id,
            "piece_id": piece_id,
        },
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text
    assert "Tamil" in captured["prompt"]


async def test_refine_without_custom_instruction_still_rejects_unknown_chip(signup_user, mock_llm):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Custom Chip WS")
    brand_id = await _create_brand(client, ws_id)
    piece_id = await _seed_piece(ws_id, profile["id"], brand_id)

    res = await client.post(
        "/api/v1/text/refine",
        json={
            "content": "Original content before any chip is applied.",
            "chip": "not_a_real_chip",
            "platform": "LinkedIn",
            "brand_id": brand_id,
            "piece_id": piece_id,
        },
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 400
