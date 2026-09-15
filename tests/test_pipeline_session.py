"""Tests for the Redis-backed session status registry in
app/api/v1/text_stream.py — the resume/status endpoints' ability to give an
accurate answer (rather than a blind 404) when a session is unknown to this
process's local `_active_sessions` dict but was previously registered in
Redis (e.g. this process restarted since the session began).

Exercises the /session/{id}/status and /session/{id}/resume routes directly
rather than the SSE /generate/stream endpoint itself (which requires a real
brand profile + LLM pipeline run) — the local dict is manipulated directly
via the module, same as the module's own SSE handler would.
"""

from uuid import uuid4

import app.api.v1.text_stream as text_stream_module
from tests.conftest import create_workspace


async def test_status_not_found_when_never_registered(signup_user):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Session Status WS")

    res = await client.get(
        f"/api/v1/pipeline/session/{uuid4()}/status",
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["active"] is False
    assert body["can_resume"] is False
    assert body["status"] == "not_found"


async def test_status_active_when_in_local_dict(signup_user):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Session Status WS")
    session_id = str(uuid4())

    text_stream_module._active_sessions[session_id] = (object(), ws_id)
    try:
        res = await client.get(
            f"/api/v1/pipeline/session/{session_id}/status",
            headers={"X-Workspace-Id": ws_id},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["active"] is True
        assert body["can_resume"] is True
        assert body["status"] == "active"
    finally:
        text_stream_module._active_sessions.pop(session_id, None)


async def test_status_lost_when_registered_in_redis_but_not_local(signup_user):
    """Simulates a process restart: the session was registered before the
    restart (so Redis still has it as "active" within its TTL) but the
    local dict — and the actual running pipeline task — is gone."""
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Session Status WS")
    session_id = str(uuid4())

    await text_stream_module._register_session(session_id, ws_id)

    res = await client.get(
        f"/api/v1/pipeline/session/{session_id}/status",
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["active"] is False
    assert body["can_resume"] is False
    assert body["status"] == "lost"


async def test_status_ended_when_redis_marks_ended(signup_user):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Session Status WS")
    session_id = str(uuid4())

    await text_stream_module._mark_session_ended(session_id, ws_id)

    res = await client.get(
        f"/api/v1/pipeline/session/{session_id}/status",
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ended"


async def test_status_does_not_leak_another_workspaces_session(signup_user):
    client_a, _ = await signup_user()
    ws_a = await create_workspace(client_a, "WS A")
    client_b, _ = await signup_user()
    ws_b = await create_workspace(client_b, "WS B")
    session_id = str(uuid4())

    await text_stream_module._register_session(session_id, ws_a)

    res = await client_b.get(
        f"/api/v1/pipeline/session/{session_id}/status",
        headers={"X-Workspace-Id": ws_b},
    )
    assert res.status_code == 200
    assert res.json()["status"] == "not_found"


async def test_resume_404_when_never_registered(signup_user):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Session Resume WS")

    res = await client.post(
        f"/api/v1/pipeline/session/{uuid4()}/resume",
        json={"choice": "angle_1"},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 404


async def test_resume_409_when_redis_says_ended(signup_user):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Session Resume WS")
    session_id = str(uuid4())

    await text_stream_module._mark_session_ended(session_id, ws_id)

    res = await client.post(
        f"/api/v1/pipeline/session/{session_id}/resume",
        json={"choice": "angle_1"},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 409


async def test_resume_410_when_lost_to_restart(signup_user):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Session Resume WS")
    session_id = str(uuid4())

    await text_stream_module._register_session(session_id, ws_id)

    res = await client.post(
        f"/api/v1/pipeline/session/{session_id}/resume",
        json={"choice": "angle_1"},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 410


async def test_resume_succeeds_when_present_in_local_dict(signup_user):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Session Resume WS")
    session_id = str(uuid4())

    class _FakeEmitter:
        def __init__(self):
            self.resumed_with = None

        async def resume(self, choice: str) -> None:
            self.resumed_with = choice

    fake_emitter = _FakeEmitter()
    text_stream_module._active_sessions[session_id] = (fake_emitter, ws_id)
    try:
        res = await client.post(
            f"/api/v1/pipeline/session/{session_id}/resume",
            json={"choice": "angle_1"},
            headers={"X-Workspace-Id": ws_id},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["resumed"] is True
        assert body["choice"] == "angle_1"
        assert fake_emitter.resumed_with == "angle_1"
    finally:
        text_stream_module._active_sessions.pop(session_id, None)
