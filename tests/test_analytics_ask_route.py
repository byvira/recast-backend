"""Tests for GET/POST /api/v1/analytics/ask's response shape.

GET returned "platforms" and POST returned "platforms_checked" for the
exact same value (result["connected_platforms"]) — the frontend's
AnalyticsReport type only ever declared platforms_checked, so GET's key
never actually matched what the UI expected and the field went unused.
Standardized both to platforms_checked.

No connected accounts in these tests, so connected_platforms is
legitimately [] — this only locks in the key name, not real platform
data (that's check_platforms_node's own job, exercised elsewhere).
"""

from tests.conftest import create_workspace, signup_new_user


async def test_get_ask_returns_platforms_checked_key(api_client, mock_llm):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Ask Route WS")

    res = await api_client.get("/api/v1/analytics/ask", headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 200, res.text
    body = res.json()
    assert "platforms_checked" in body
    assert "platforms" not in body
    assert body["platforms_checked"] == []


async def test_post_ask_returns_platforms_checked_key(api_client, mock_llm):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Ask Route WS 2")

    res = await api_client.post(
        "/api/v1/analytics/ask",
        json={"question": "What should I post next?"},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text
    assert res.json()["platforms_checked"] == []
