"""Tests for POST /api/v1/text/pieces/manual.

Voices' playground "Send to Drafts" used to just show a fake success toast
with nothing saved. This endpoint persists text already produced elsewhere
(the playground's own real voice-transform call) as a real draft, with no
second LLM call — regenerate would have rewritten it, not saved it.
"""
from uuid import uuid4

from app.db.mongo import brand_profiles, content_pieces
from tests.conftest import create_workspace


async def _create_brand(client, ws_id: str) -> str:
    res = await client.post("/api/v1/brand/", json={"brand_type": "Person"}, headers={"X-Workspace-Id": ws_id})
    assert res.status_code in (200, 201), res.text
    brand_id = res.json()["brand_profile_id"]
    # /text/pieces/manual (like /text/regenerate) requires a *complete* brand
    # profile — flip it directly rather than driving full onboarding.
    await brand_profiles.update_one({"id": brand_id}, {"$set": {"is_complete": True}})
    return brand_id


async def test_manual_piece_is_saved_exactly_as_given(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Manual Piece WS")
    brand_id = await _create_brand(client, ws_id)

    res = await client.post(
        "/api/v1/text/pieces/manual",
        json={"platform": "LinkedIn", "brand_id": brand_id, "content": "Exactly this text, unchanged."},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["content"] == "Exactly this text, unchanged."
    assert body["piece_id"]

    piece = await content_pieces.find_one({"piece_id": body["piece_id"]})
    assert piece is not None
    assert piece["workspace_id"] == ws_id
    assert piece["platform"] == "LinkedIn"
    assert piece["content"] == "Exactly this text, unchanged."
    assert piece["brand_id"] == brand_id

    # A real draft, reachable through the normal piece-detail route.
    check = await client.get(f"/api/v1/content/pieces/{body['piece_id']}", headers={"X-Workspace-Id": ws_id})
    assert check.status_code == 200
    assert check.json()["stage"] == "drafting"


async def test_manual_piece_rejects_empty_content(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Manual Piece Empty WS")
    brand_id = await _create_brand(client, ws_id)

    res = await client.post(
        "/api/v1/text/pieces/manual",
        json={"platform": "LinkedIn", "brand_id": brand_id, "content": "   "},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 400


async def test_manual_piece_rejects_an_unknown_platform(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Manual Piece Platform WS")
    brand_id = await _create_brand(client, ws_id)

    res = await client.post(
        "/api/v1/text/pieces/manual",
        json={"platform": "Not A Platform", "brand_id": brand_id, "content": "Hello"},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 400


async def test_manual_piece_rejects_someone_elses_brand(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Manual Piece Brand WS")

    res = await client.post(
        "/api/v1/text/pieces/manual",
        json={"platform": "LinkedIn", "brand_id": str(uuid4()), "content": "Hello"},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 404
