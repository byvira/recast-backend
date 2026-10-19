"""Disconnecting LinkedIn asks LinkedIn to stop honouring the saved token, and the connection is removed either way.

The call to LinkedIn is replaced by a recorder; nothing here reaches a real provider.
"""

from app.db.mongo import workspace_connections
from app.pipelines.publish.token_store import save_token
from tests.conftest import create_workspace, signup_new_user


async def _connect_linkedin(ws_id: str, user_id: str) -> None:
    await save_token(
        workspace_id=ws_id, platform="linkedin", access_token="li-access-secret", refresh_token="li-refresh-secret",
        expires_at=None, platform_user_id="li-1", username="person", connected_by=user_id,
    )


async def test_linkedin_disconnect_revokes_the_token_at_linkedin(api_client, monkeypatch):
    profile = await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Revoke Space")
    await _connect_linkedin(ws_id, profile["id"])

    calls = []

    async def record(url, data):
        calls.append((url, data))

    monkeypatch.setattr("app.api.v1.oauth._provider_post", record, raising=False)
    res = await api_client.delete("/api/v1/oauth/linkedin/disconnect", headers={"X-Workspace-Id": ws_id})

    assert res.status_code == 200
    assert len(calls) == 1
    url, data = calls[0]
    assert url == "https://www.linkedin.com/oauth/v2/revoke"
    assert data["token"] == "li-access-secret"
    assert data["client_id"] is not None and "client_secret" in data
    assert await workspace_connections.count_documents({"workspace_id": ws_id, "platform": "linkedin"}) == 0


async def test_linkedin_disconnect_still_removes_the_connection_when_linkedin_refuses(api_client, monkeypatch):
    profile = await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Revoke Failure Space")
    await _connect_linkedin(ws_id, profile["id"])

    async def refuse(url, data):
        raise RuntimeError("linkedin is down")

    monkeypatch.setattr("app.api.v1.oauth._provider_post", refuse, raising=False)
    res = await api_client.delete("/api/v1/oauth/linkedin/disconnect", headers={"X-Workspace-Id": ws_id})

    assert res.status_code == 200
    assert await workspace_connections.count_documents({"workspace_id": ws_id, "platform": "linkedin"}) == 0
