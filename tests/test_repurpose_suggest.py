"""Tests for POST /api/v1/text/repurpose/suggest — new repurpose flow's
"AI Suggestions" step.

Quick Recast used to jump straight from a raw paste to blind platform
checkboxes with no read of the content at all. This is a cheap, read-only
suggestion call the user can accept or override before the real
/repurpose call runs.

Runs with the LLM mocked — never a real Groq call.
"""

from tests.conftest import create_workspace


async def _create_brand(client, ws_id: str) -> str:
    res = await client.post(
        "/api/v1/brand/", json={"brand_type": "Person"}, headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code in (200, 201), res.text
    return res.json()["brand_profile_id"]


async def test_suggest_returns_platforms_tone_and_angle(signup_user, mock_llm):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Suggest WS")
    brand_id = await _create_brand(client, ws_id)

    mock_llm.set_structured({
        "suggested_platforms": ["Twitter/X", "Instagram"],
        "rationale": "Short, punchy insight fits both feeds well.",
        "suggested_tone": "punchy",
        "suggested_angle": "Lead with the surprising result.",
    })

    res = await client.post(
        "/api/v1/text/repurpose/suggest",
        json={
            "source_content": "A long LinkedIn post about distribution strategy.",
            "source_platform": "LinkedIn",
            "brand_id": brand_id,
        },
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["suggested_platforms"] == ["Twitter/X", "Instagram"]
    assert body["rationale"]
    assert body["suggested_tone"] == "punchy"


async def test_suggest_never_recommends_the_source_platform(signup_user, mock_llm):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Suggest WS")
    brand_id = await _create_brand(client, ws_id)

    # LLM misbehaves and includes the source platform anyway — the endpoint
    # must filter it out itself, not trust the model's output blindly.
    mock_llm.set_structured({
        "suggested_platforms": ["LinkedIn", "Facebook"],
        "rationale": "r",
        "suggested_tone": "t",
        "suggested_angle": "a",
    })

    res = await client.post(
        "/api/v1/text/repurpose/suggest",
        json={
            "source_content": "Some content.",
            "source_platform": "LinkedIn",
            "brand_id": brand_id,
        },
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text
    assert res.json()["suggested_platforms"] == ["Facebook"]


async def test_suggest_falls_back_gracefully_when_llm_returns_nothing_usable(signup_user, mock_llm):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Suggest WS")
    brand_id = await _create_brand(client, ws_id)

    mock_llm.set_structured({})

    res = await client.post(
        "/api/v1/text/repurpose/suggest",
        json={
            "source_content": "Some content.",
            "source_platform": "LinkedIn",
            "brand_id": brand_id,
        },
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text
    assert res.json()["suggested_platforms"] == []


async def test_suggest_blocked_for_viewer(signup_user, make_client):
    from tests.conftest import invite_and_accept

    owner_client, _ = await signup_user()
    ws_id = await create_workspace(owner_client, "Suggest RBAC WS")
    brand_id = await _create_brand(owner_client, ws_id)
    viewer_client, _ = await invite_and_accept(owner_client, make_client, ws_id, "viewer")

    res = await viewer_client.post(
        "/api/v1/text/repurpose/suggest",
        json={"source_content": "Some content.", "source_platform": "LinkedIn", "brand_id": brand_id},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 403
