"""Cross-instance resume/status for streamed text runs (PAR-005) —
app.agents.text.session_relay + the /pipeline/session/* routes."""

import asyncio
from uuid import uuid4

from app.agents.text import session_relay
from app.api.v1 import text_stream
from app.db.redis import set_cache
from tests.conftest import create_workspace, signup_new_user


async def _remote_session(ws_id: str) -> str:
    """A session registered and beating as if another instance owned it —
    nothing in this process's _active_sessions."""
    sid = str(uuid4())
    await set_cache(f"pipeline_session:{sid}", {"workspace_id": ws_id, "status": "active"}, ttl=600)
    await session_relay._beat(sid, ws_id)
    return sid


async def test_status_and_resume_work_for_a_session_on_another_instance(api_client, monkeypatch):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Relay", tier="large")
    sid = await _remote_session(ws_id)
    h = {"X-Workspace-Id": ws_id}

    status = (await api_client.get(f"/api/v1/pipeline/session/{sid}/status", headers=h)).json()
    assert status == {"session_id": sid, "active": True, "can_resume": True, "status": "active"}

    received: list[tuple] = []

    async def owner_handler(session_id, workspace_id, choice):   # stands in for the owning instance
        received.append((session_id, workspace_id, choice))
        return True

    session_relay.start_listener(owner_handler)
    await asyncio.sleep(0.5)                                      # let the subscriber attach
    try:
        res = await api_client.post(f"/api/v1/pipeline/session/{sid}/resume",
                                    json={"choice": "angle_2"}, headers=h)
        assert res.status_code == 200, res.text
        assert res.json()["relayed"] is True
        for _ in range(20):
            if received:
                break
            await asyncio.sleep(0.1)
        assert received == [(sid, ws_id, "angle_2")]
    finally:
        await session_relay.stop_listener()


async def test_session_whose_instance_died_is_reported_lost(api_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Lost", tier="large")
    sid = str(uuid4())
    # Registered, but no instance is beating for it any more.
    await set_cache(f"pipeline_session:{sid}", {"workspace_id": ws_id, "status": "active"}, ttl=600)
    h = {"X-Workspace-Id": ws_id}

    status = (await api_client.get(f"/api/v1/pipeline/session/{sid}/status", headers=h)).json()
    assert status["status"] == "lost" and status["can_resume"] is False
    res = await api_client.post(f"/api/v1/pipeline/session/{sid}/resume", json={"choice": "x"}, headers=h)
    assert res.status_code == 410


async def test_other_workspace_cannot_resume_via_relay(api_client, make_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Owner WS", tier="large")
    sid = await _remote_session(ws_id)

    other = make_client()
    await signup_new_user(other)
    other_ws = await create_workspace(other, "Other WS", tier="large")
    res = await other.post(f"/api/v1/pipeline/session/{sid}/resume", json={"choice": "x"},
                           headers={"X-Workspace-Id": other_ws})
    assert res.status_code == 404


async def test_local_resume_handler_only_applies_to_owned_sessions():
    assert await text_stream._apply_local_resume("not-mine", "ws", "c") is False
