"""Integration tests for the 7 already-wired Onboarding/Brand endpoints.

Per the module scope these were "wired and working — verify, don't re-wire";
this file exists so all 26 endpoints have coverage, not to re-litigate their
design. Lighter than the Auth/Workspace/Invites suites by intent.
"""

from tests.conftest import create_workspace, invite_and_accept, signup_new_user


async def test_draft_get_returns_404_when_none_exists(api_client):
    await signup_new_user(api_client)
    res = await api_client.get("/api/v1/onboarding/draft")
    assert res.status_code == 404


async def test_draft_save_and_get_roundtrip(api_client):
    await signup_new_user(api_client)
    payload = {
        "brand_id": None,
        "brand_type": "Business",
        "current_step": 2,
        "total_steps": 7,
        "is_complete": False,
        "identity": {"name": "Acme"},
    }
    res = await api_client.post("/api/v1/onboarding/draft", json=payload)
    assert res.status_code == 200
    body = res.json()
    assert body["current_step"] == 2
    assert body["identity"] == {"name": "Acme"}

    res = await api_client.get("/api/v1/onboarding/draft")
    assert res.status_code == 200
    assert res.json()["brand_type"] == "Business"


async def test_draft_delete_is_idempotent_and_clears_state(api_client):
    await signup_new_user(api_client)
    res = await api_client.delete("/api/v1/onboarding/draft")
    assert res.status_code == 204

    await api_client.post(
        "/api/v1/onboarding/draft",
        json={"current_step": 1, "total_steps": 6},
    )
    res = await api_client.delete("/api/v1/onboarding/draft")
    assert res.status_code == 204

    res = await api_client.get("/api/v1/onboarding/draft")
    assert res.status_code == 404


async def test_brand_create_requires_edit_brand_voice_permission(api_client, make_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Brand Perms", tier="large")
    viewer_client, _ = await invite_and_accept(api_client, make_client, ws_id, "viewer")

    res = await viewer_client.post(
        "/api/v1/brand/",
        json={"brand_type": "Business"},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 403


async def test_brand_create_get_step_and_complete_lifecycle(api_client):
    await signup_new_user(api_client)

    res = await api_client.post("/api/v1/brand/", json={"brand_type": "Business"})
    assert res.status_code == 201
    brand_id = res.json()["brand_profile_id"]

    res = await api_client.get(f"/api/v1/brand/{brand_id}")
    assert res.status_code == 200
    assert res.json()["is_complete"] is False

    res = await api_client.put(
        f"/api/v1/brand/{brand_id}/step",
        json={"step": 2, "data": {"companyName": "Acme Inc"}},
    )
    assert res.status_code == 200
    assert res.json()["next_step"] == 3

    res = await api_client.get(f"/api/v1/brand/{brand_id}")
    assert res.json()["identity"] == {"company_name": "Acme Inc"}

    res = await api_client.put(f"/api/v1/brand/{brand_id}/complete")
    assert res.status_code == 200
    assert res.json()["is_complete"] is True

    res = await api_client.get("/api/v1/users/me")
    assert res.json()["onboarding_done"] is True


async def test_brand_not_found(api_client):
    await signup_new_user(api_client)
    res = await api_client.get("/api/v1/brand/does-not-exist")
    assert res.status_code == 404


# ── Funnel telemetry ──────────────────────────────────────────────────────────

async def test_funnel_event_requires_edit_brand_voice_permission(api_client, make_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Funnel Perms", tier="large")
    viewer_client, _ = await invite_and_accept(api_client, make_client, ws_id, "viewer")

    res = await viewer_client.post(
        "/api/v1/onboarding/funnel-event",
        json={"event": "step_reached", "step": 1, "step_title": "Operation Type"},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 403


async def test_funnel_event_logs_and_report_reflects_it(api_client):
    await signup_new_user(api_client)

    for step, title in [(1, "Operation Type"), (2, "Core Identity"), (2, "Core Identity")]:
        res = await api_client.post(
            "/api/v1/onboarding/funnel-event",
            json={
                "event": "step_reached",
                "step": step,
                "step_title": title,
                "total_steps": 6,
                "brand_type": "Person",
            },
        )
        assert res.status_code == 202

    res = await api_client.post(
        "/api/v1/onboarding/funnel-event",
        json={"event": "completed", "step": 6, "step_title": "Platforms", "brand_type": "Person"},
    )
    assert res.status_code == 202

    res = await api_client.get("/api/v1/onboarding/funnel-report")
    assert res.status_code == 200
    body = res.json()
    assert body["started"] == 1
    assert body["completed"] == 1
    assert body["completion_rate"] == 1.0

    by_step = {(r["event"], r["step"]): r["count"] for r in body["by_step"]}
    assert by_step[("step_reached", 1)] == 1
    assert by_step[("step_reached", 2)] == 2  # logged twice — reflected, not deduped
    assert by_step[("completed", 6)] == 1


async def test_funnel_report_is_scoped_to_active_workspace(api_client, make_client):
    await signup_new_user(api_client)
    # Personal (default) workspace gets one event.
    res = await api_client.post(
        "/api/v1/onboarding/funnel-event",
        json={"event": "step_reached", "step": 1, "step_title": "Operation Type"},
    )
    assert res.status_code == 202

    # A second, separate workspace owned by the same user starts with none.
    ws_id = await create_workspace(api_client, "Second Workspace", tier="duo")
    res = await api_client.get(
        "/api/v1/onboarding/funnel-report", headers={"X-Workspace-Id": ws_id}
    )
    assert res.status_code == 200
    assert res.json()["started"] == 0
