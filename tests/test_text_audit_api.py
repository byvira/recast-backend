"""API-level tests for the text fixes from the flow audit (the model is mocked, never called)."""

from uuid import uuid4

from app.db.mongo import content_pieces, content_piece_versions, member_lexicon
from app.pipelines.text import refiner as refiner_module
from app.pipelines.text.storage import ensure_session_exists, save_live_piece
from tests.conftest import create_workspace


async def _brand(client, ws_id: str) -> str:
    res = await client.post("/api/v1/brand/", json={"brand_type": "Person"}, headers={"X-Workspace-Id": ws_id})
    assert res.status_code in (200, 201), res.text
    return res.json()["brand_profile_id"]


async def _piece(ws_id: str, user_id: str, brand_id: str, **extra) -> str:
    session_id = str(uuid4())
    await ensure_session_exists(session_id=session_id, workspace_id=ws_id, user_id=user_id, brand_id=brand_id, source_type="text")
    return await save_live_piece(
        session_id=session_id, workspace_id=ws_id, user_id=user_id, brand_id=brand_id, platform="LinkedIn",
        content="Original content, version 1.", word_count=4, char_count=28, extra_fields=extra or None,
    )


def _chat(ws_id: str, brand_id: str, piece_id: str):
    return {
        "json": {
            "messages": [{"role": "user", "content": "Make it punchier"}],
            "platform": "LinkedIn", "brand_id": brand_id, "piece_id": piece_id,
        },
        "headers": {"X-Workspace-Id": ws_id},
    }


async def test_a_chat_answer_with_a_word_the_member_banned_is_not_saved(signup_user, mock_llm):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Chat Guard WS")
    brand_id = await _brand(client, ws_id)
    piece_id = await _piece(ws_id, profile["id"], brand_id)
    # the member's own banned word is applied in chat now, the same as in generation
    await member_lexicon.insert_one({"workspace_id": ws_id, "user_id": profile["id"], "blacklist": ["synergy"], "whitelist": []})
    mock_llm.set_chat("We love synergy here.")

    res = await client.post("/api/v1/text/refine-chat", **_chat(ws_id, brand_id, piece_id))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["refined"] == "Original content, version 1."   # the text is as it was
    assert body["version_saved"] is False and body["error"]
    assert (await content_pieces.find_one({"piece_id": piece_id}))["version_count"] == 1


async def test_a_good_chat_answer_still_saves_a_version(signup_user, mock_llm):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Chat Guard OK WS")
    brand_id = await _brand(client, ws_id)
    piece_id = await _piece(ws_id, profile["id"], brand_id)
    mock_llm.set_chat("Sharper wording, same point.")

    res = await client.post("/api/v1/text/refine-chat", **_chat(ws_id, brand_id, piece_id))
    body = res.json()
    assert res.status_code == 200 and body["refined"] == "Sharper wording, same point."
    assert body["version_saved"] is True and body["error"] is None


async def test_a_post_is_refined_in_the_language_it_was_made_in(signup_user, monkeypatch):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Piece Language WS")   # the workspace itself is English
    brand_id = await _brand(client, ws_id)
    piece_id = await _piece(ws_id, profile["id"], brand_id, language="ta")
    captured: dict[str, str] = {}

    async def _fake_call_llm_chat(messages, *args, system: str = "", **kwargs):
        captured["system"] = system
        return "Refined in the same language."

    monkeypatch.setattr(refiner_module, "call_llm_chat", _fake_call_llm_chat)
    res = await client.post("/api/v1/text/refine-chat", **_chat(ws_id, brand_id, piece_id))
    assert res.status_code == 200, res.text
    assert "Tamil" in captured["system"]


async def test_a_failed_hook_score_is_an_error_not_a_zero(signup_user, mock_llm):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Score Fail WS")
    brand_id = await _brand(client, ws_id)
    mock_llm.set_structured({})   # the model gave nothing usable

    res = await client.post(
        "/api/v1/text/score-hook",
        json={"content": "A post worth scoring, long enough to be real.", "platform": "LinkedIn", "brand_id": brand_id},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 503, res.text
    assert "Scoring isn't available" in res.json()["detail"]


async def test_an_empty_generation_leaves_no_piece_behind():
    """A run where the model wrote nothing used to save an empty, flagged piece that then showed in Drafts."""
    from app.agents.text.event_emitter import EventEmitter
    from app.agents.text.nodes import collect_output_node
    from app.models.text import InputSourceType, Platform

    ws, user, brand = str(uuid4()), str(uuid4()), str(uuid4())
    state = {
        "generated_content": "", "current_platform": Platform.LINKEDIN, "hooks": [], "seo_package": {},
        "readability_score": None, "quality_passed": False, "quality_issues": ["Content is empty"],
        "flagged_for_review": True, "publish_target": None, "schedule_mode": "now", "scheduled_at": None,
        "pieces": [], "emitter": EventEmitter(), "workspace_id": ws, "user_id": user, "brand_id": brand,
        "session_id": str(uuid4()), "source_type": InputSourceType.TEXT, "extras": {}, "language": "en",
        "is_repurpose": False, "batch_mode": False, "raw_input": "x",
    }
    await collect_output_node(state)
    assert await content_pieces.count_documents({"workspace_id": ws}) == 0
    assert await content_piece_versions.count_documents({"workspace_id": ws}) == 0
