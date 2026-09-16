"""Tests for POST /api/v1/text/regenerate — specifically the piece_id bug
fixed alongside Module 2 Stage 5 (Retry).

RegenerateResponse.piece_id used to always be "" even though the piece
really was saved: GeneratedPiece has no piece_id field of its own (same
root cause Stage 1 fixed for the SSE path — confirmed independently
present here too), and _save_result() discarded the real, storage-
generated piece_id instead of returning it. Retry depends on this
working, since it reads response.piece_id to give the retried card a
real, usable piece_id.

Runs the real pipeline end to end with the LLM mocked — never a real
Groq call.
"""

from app.db.mongo import brand_profiles
from app.pipelines.text.storage import get_piece
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


async def test_regenerate_returns_a_real_persisted_piece_id(signup_user, mock_llm):
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
