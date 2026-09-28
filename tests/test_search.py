"""Tests for GET /api/v1/search and POST /api/v1/search/agentic.

Both replace the frontend's old fully-fabricated search modal (5 hardcoded
titles, no backend call at all). The fast path is a plain, real, cheap
match; the agentic path spends one real LLM call (mocked here — never a
real Groq call) picking from the same real pool and saying why.
"""
from uuid import uuid4

from app.db.mongo import content_pieces
from app.pipelines.text.storage import ensure_session_exists, save_live_piece
from tests.conftest import create_workspace


async def _seed_piece(workspace_id: str, user_id: str, brand_id: str, content: str, platform: str = "LinkedIn") -> str:
    session_id = str(uuid4())
    await ensure_session_exists(
        session_id=session_id, workspace_id=workspace_id, user_id=user_id,
        brand_id=brand_id, source_type="text",
    )
    return await save_live_piece(
        session_id=session_id, workspace_id=workspace_id, user_id=user_id,
        brand_id=brand_id, platform=platform, content=content,
        word_count=len(content.split()), char_count=len(content),
    )


async def test_fast_search_matches_a_real_draft_by_its_own_content(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Search WS")
    piece_id = await _seed_piece(ws_id, profile["id"], str(uuid4()), "The quarterly pricing announcement is live today.")
    await _seed_piece(ws_id, profile["id"], str(uuid4()), "Totally unrelated content about hiking trails.")

    res = await client.get("/api/v1/search/", params={"q": "pricing"}, headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 200, res.text
    items = res.json()["items"]
    drafts = [i for i in items if i["type"] == "draft"]
    assert len(drafts) == 1
    assert f"piece={piece_id}" in drafts[0]["href"]
    assert "reason" not in drafts[0] or drafts[0]["reason"] is None


async def test_fast_search_never_shows_another_workspaces_content(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Search Isolation WS")
    await _seed_piece(ws_id, profile["id"], str(uuid4()), "Secret internal launch notes.")

    other = await create_workspace(client, "Other Search WS")
    res = await client.get("/api/v1/search/", params={"q": "secret"}, headers={"X-Workspace-Id": other})
    assert res.status_code == 200
    assert res.json()["items"] == []


async def test_fast_search_requires_a_query(signup_user):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Search Empty WS")
    res = await client.get("/api/v1/search/", params={"q": ""}, headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 422


async def test_agentic_search_returns_only_the_picks_the_model_made(signup_user, mock_llm):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Agentic Search WS")
    real_id = await _seed_piece(ws_id, profile["id"], str(uuid4()), "the launch day checklist for our new pricing page")
    await _seed_piece(ws_id, profile["id"], str(uuid4()), "completely unrelated hiking content")

    mock_llm.set_structured({"matches": [{"index": 0, "reason": "Mentions the pricing launch"}]})

    res = await client.post(
        "/api/v1/search/agentic", json={"query": "the pricing launch post"}, headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text
    items = res.json()["items"]
    assert len(items) == 1
    assert items[0]["reason"] == "Mentions the pricing launch"


async def test_agentic_search_falls_back_to_plain_results_if_the_model_errors(signup_user, mock_llm, monkeypatch):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Agentic Fallback WS")
    await _seed_piece(ws_id, profile["id"], str(uuid4()), "the pricing launch checklist")

    async def _boom(*a, **k):
        raise RuntimeError("provider hiccup")
    import app.api.v1.search as search_module
    monkeypatch.setattr(search_module, "call_llm_structured", _boom)

    res = await client.post(
        "/api/v1/search/agentic", json={"query": "pricing"}, headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text
    # Degrades to the literal match rather than a hard failure — the
    # member's search box keeps working even if the model call fails.
    assert len(res.json()["items"]) == 1


async def test_agentic_search_is_blocked_by_the_kill_switch(signup_user, mock_llm):
    from app.db.mongo import workspaces
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Agentic Halted WS")
    await workspaces.update_one({"id": ws_id}, {"$set": {"generation_halted": True}})

    res = await client.post(
        "/api/v1/search/agentic", json={"query": "anything"}, headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 403
