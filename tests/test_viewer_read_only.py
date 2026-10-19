"""A viewer is read only: they cannot stop or restart a teammate's run, and they cannot use the AI helpers that spend the workspace's allowance.

Only the viewer's calls reach the AI routes (they are refused before any model is asked), so no provider quota is used.
"""

import pytest

from tests.conftest import create_workspace, invite_and_accept


async def _viewer(signup_user, make_client):
    owner_client, _ = await signup_user()
    ws_id = await create_workspace(owner_client, "Viewer Read Only WS")
    viewer_client, _ = await invite_and_accept(owner_client, make_client, ws_id, "viewer")
    return owner_client, viewer_client, {"X-Workspace-Id": ws_id}


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/v1/pipeline/session/s1/control", {"action": "cancel"}),
        ("/api/v1/pipeline/session/s1/resume", {}),
        ("/api/v1/suggest/complete", {"kind": "post_idea", "text": "A post about our new launch"}),
        ("/api/v1/search/agentic", {"query": "launch"}),
        ("/api/v1/text/preview-url", {"url": "https://example.com/post"}),
        ("/api/v1/analytics/ask", {"question": "How did we do?"}),
    ],
)
async def test_a_viewer_is_refused(signup_user, make_client, path, body):
    _, viewer_client, headers = await _viewer(signup_user, make_client)
    res = await viewer_client.post(path, json=body, headers=headers)
    assert res.status_code == 403, res.text


async def test_an_owner_can_still_control_a_run(signup_user, make_client):
    owner_client, _, headers = await _viewer(signup_user, make_client)
    res = await owner_client.post("/api/v1/pipeline/session/s1/control", json={"action": "cancel"}, headers=headers)
    assert res.status_code == 404  # allowed in, and there is simply no such run
