"""Tests for Module 2 Stage 4 — manual edit-save and rescore-on-change.

Covers two previously-uncovered real backend endpoints that the frontend's
edit/rescore fix now depends on:
  - PATCH /api/v1/content/pieces/{id}     (manual edit, edit_content perm)
  - POST  /api/v1/text/score-hook          (LLM-backed, membership only)
  - POST  /api/v1/text/score-readability   (pure computation, membership only)

score-hook is exercised via the mock_llm fixture — never a real Groq call.
score-readability needs no mock at all, which is itself part of what's
being confirmed (its own docstring claims "pure computation — no LLM
call"; a test that passes without mock_llm is evidence that's actually true).
"""

from uuid import uuid4

from app.pipelines.text.storage import ensure_session_exists, save_live_piece
from tests.conftest import create_workspace, invite_and_accept


async def _seed_piece(workspace_id: str, user_id: str, brand_id: str | None = None) -> str:
    session_id = str(uuid4())
    brand_id = brand_id or str(uuid4())
    await ensure_session_exists(
        session_id=session_id, workspace_id=workspace_id, user_id=user_id,
        brand_id=brand_id, source_type="text",
    )
    return await save_live_piece(
        session_id=session_id, workspace_id=workspace_id, user_id=user_id,
        brand_id=brand_id, platform="LinkedIn",
        content="Original content before any edit.", word_count=5, char_count=34,
    )


async def _create_brand(client, ws_id: str) -> str:
    res = await client.post(
        "/api/v1/brand/", json={"brand_type": "Person"}, headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code in (200, 201), res.text
    return res.json()["brand_profile_id"]


# ─────────────────────────────────────────────────────────────────────────────
# Edit
# ─────────────────────────────────────────────────────────────────────────────

async def test_edit_piece_persists_and_creates_a_new_version(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Edit WS")
    piece_id = await _seed_piece(ws_id, profile["id"])

    res = await client.patch(
        f"/api/v1/content/pieces/{piece_id}",
        json={"content": "Manually edited content, for real this time."},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["content"] == "Manually edited content, for real this time."
    assert body["version_count"] == 2

    versions = await client.get(
        f"/api/v1/content/pieces/{piece_id}/versions", headers={"X-Workspace-Id": ws_id},
    )
    assert versions.status_code == 200
    version_list = versions.json()["versions"]
    assert len(version_list) == 2
    assert version_list[-1]["action"] == "manual_edit"


async def test_edit_piece_recomputes_stale_readability_score(signup_user):
    """update_piece_content() used to only touch content/word_count/
    char_count/version_count — readability_score was never recomputed on
    manual edit, chip/chat refinement, or version restore, so it silently
    described whatever content the piece had *before* the edit (or stayed
    permanently null if the piece started non-Latin-script). This is the
    fix: it must reflect the content actually stored after the edit."""
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Edit WS")
    piece_id = await _seed_piece(ws_id, profile["id"])  # seeded with no readability_score at all

    before = await client.get(
        f"/api/v1/content/pieces/{piece_id}", headers={"X-Workspace-Id": ws_id},
    )
    assert before.json()["readability_score"] is None

    res = await client.patch(
        f"/api/v1/content/pieces/{piece_id}",
        json={"content": "Short sentences work well. They are easy to read. Most people prefer them."},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text
    assert res.json()["readability_score"] is not None

    after = await client.get(
        f"/api/v1/content/pieces/{piece_id}", headers={"X-Workspace-Id": ws_id},
    )
    assert after.json()["readability_score"] is not None


async def test_edit_piece_rejects_empty_content(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Edit WS")
    piece_id = await _seed_piece(ws_id, profile["id"])

    res = await client.patch(
        f"/api/v1/content/pieces/{piece_id}",
        json={"content": "   "},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 400


async def test_edit_piece_404_for_nonexistent_piece(signup_user):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Edit WS")

    res = await client.patch(
        f"/api/v1/content/pieces/{uuid4()}",
        json={"content": "doesn't matter"},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 404


async def test_edit_piece_blocked_for_viewer(signup_user, make_client):
    owner_client, owner_profile = await signup_user()
    ws_id = await create_workspace(owner_client, "Edit RBAC WS")
    piece_id = await _seed_piece(ws_id, owner_profile["id"])

    viewer_client, _ = await invite_and_accept(owner_client, make_client, ws_id, "viewer")

    res = await viewer_client.patch(
        f"/api/v1/content/pieces/{piece_id}",
        json={"content": "should not be allowed"},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 403


# ─────────────────────────────────────────────────────────────────────────────
# Rescore
# ─────────────────────────────────────────────────────────────────────────────

async def test_score_hook_returns_mocked_llm_result(signup_user, mock_llm):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Score WS")
    brand_id = await _create_brand(client, ws_id)

    mock_llm.set_structured({
        "current_score": 42,
        "current_reason": "Weak opener, buries the point.",
        "alternatives": [
            {"text": "The hook that actually stops the scroll.", "style": "punchy", "score": 91, "reason": "Direct claim up front."},
            {"text": "Alternative B", "style": "question", "score": 70, "reason": "Decent but soft."},
            {"text": "Alternative C", "style": "story", "score": 55, "reason": "Too slow to the point."},
        ],
    })

    res = await client.post(
        "/api/v1/text/score-hook",
        json={"content": "Some LinkedIn post content here.", "platform": "LinkedIn", "brand_id": brand_id},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["current_score"] == 42
    assert body["recommended"] == 0  # highest-scoring alternative (91) is index 0
    assert len(body["alternatives"]) == 3


async def test_score_hook_404_for_brand_outside_workspace(signup_user):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Score WS")

    res = await client.post(
        "/api/v1/text/score-hook",
        json={"content": "content", "platform": "LinkedIn", "brand_id": str(uuid4())},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 404


async def test_score_readability_needs_no_llm_mock_at_all(signup_user):
    """No mock_llm fixture here on purpose — if score-readability secretly
    called an LLM, this test would either hang or fail against a real
    Groq client with no credentials wired for it, proving its own
    "pure computation" docstring claim rather than just trusting it."""
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Score WS")

    res = await client.post(
        "/api/v1/text/score-readability",
        json={
            "content": "Short sentences work well. They are easy to read. Most people prefer them.",
            "platform": "LinkedIn",
        },
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert isinstance(body["score"], (int, float))
    assert "grade_label" in body
