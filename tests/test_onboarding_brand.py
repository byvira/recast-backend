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


async def test_brand_step3_type_specific_data_survives_the_round_trip(api_client):
    """_build_step_update() writes pillars_data/icp_data/positioning_data
    straight into the Mongo document, but BrandProfile (the response model)
    never declared those fields — _doc_to_brand_profile() silently dropped
    them from every GET response even though they were really saved. This
    is what made the onboarding wizard's resume flow (and any other real
    consumer of GET /brand/{id}) unable to see step 3's answers at all for
    Personal Brand/Business/Product types."""
    await signup_new_user(api_client)

    res = await api_client.post("/api/v1/brand/", json={"brand_type": "Business"})
    brand_id = res.json()["brand_profile_id"]

    icp_payload = {
        "companySize": "SMBs (10-100)",
        "jobTitles": "Head of Growth",
        "painPoint": "Manual repurposing eats a full day a week",
        "decisionMakers": "VP Marketing",
    }
    res = await api_client.put(
        f"/api/v1/brand/{brand_id}/step", json={"step": 3, "data": icp_payload},
    )
    assert res.status_code == 200, res.text

    res = await api_client.get(f"/api/v1/brand/{brand_id}")
    assert res.status_code == 200
    body = res.json()
    assert body["icp_data"] is not None
    assert body["icp_data"]["painPoint"] == "Manual repurposing eats a full day a week"
    # The other two type-specific fields stay genuinely empty, not silently
    # populated with something — this brand is Business, not Personal
    # Brand or Product.
    assert body["pillars_data"] is None
    assert body["positioning_data"] is None


async def test_update_voice_sets_only_the_fields_provided(api_client):
    """#9c — inline editing on the Voice Blueprint view. PATCH /voice must
    set voice_tone/manual_data independently — unlike PUT /step's "setup"
    step, which overwrites manual_data wholesale alongside
    extraction_data/setup_path from the same payload, omitting a field
    here must leave it untouched rather than nulling it out."""
    await signup_new_user(api_client)
    res = await api_client.post("/api/v1/brand/", json={"brand_type": "Person"})
    brand_id = res.json()["brand_profile_id"]

    # Seed manual_data via the real setup step first, same as onboarding would.
    res = await api_client.put(
        f"/api/v1/brand/{brand_id}/step",
        json={"step": 5, "data": {
            "setup_path": "manual",
            "manual_data": {"openers": ["Here's the thing."], "closers": [], "phrases": [], "banned_words": [], "preferred_synonyms": []},
        }},
    )
    assert res.status_code == 200, res.text

    # Now edit only voice_tone via the new endpoint.
    res = await api_client.patch(
        f"/api/v1/brand/{brand_id}/voice",
        json={"voice_tone": {"tones": ["direct", "witty"], "humor": "Subtle", "emoji": "Never", "style": "Short sentences."}},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["voice_tone"]["tones"] == ["direct", "witty"]
    # manual_data (seeded above, not touched by this call) must survive untouched.
    assert body["manual_data"]["openers"] == ["Here's the thing."]


async def test_update_voice_edits_manual_data_independently(api_client):
    await signup_new_user(api_client)
    res = await api_client.post("/api/v1/brand/", json={"brand_type": "Person"})
    brand_id = res.json()["brand_profile_id"]

    res = await api_client.patch(
        f"/api/v1/brand/{brand_id}/voice",
        json={"manual_data": {"openers": [], "closers": [], "phrases": [], "banned_words": ["synergy"], "preferred_synonyms": []}},
    )
    assert res.status_code == 200, res.text
    assert res.json()["manual_data"]["banned_words"] == ["synergy"]


async def test_update_voice_sets_default_tone_and_round_trips(api_client):
    """My Voices > Calibration tab's persistent default tone — a brand's
    own default for every generation when no per-run ToneSelector override
    is picked (app/pipelines/text/orchestrator.py::_build_metadata)."""
    await signup_new_user(api_client)
    res = await api_client.post("/api/v1/brand/", json={"brand_type": "Person"})
    brand_id = res.json()["brand_profile_id"]

    res = await api_client.patch(
        f"/api/v1/brand/{brand_id}/voice",
        json={"default_tone": "professional"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["default_tone"] == "professional"

    res = await api_client.get(f"/api/v1/brand/{brand_id}")
    assert res.json()["default_tone"] == "professional"


async def test_update_voice_resets_default_tone_to_brand(api_client):
    """An explicit "brand" value (not omitting the field) is how a caller
    clears a previously-set persistent default."""
    await signup_new_user(api_client)
    res = await api_client.post("/api/v1/brand/", json={"brand_type": "Person"})
    brand_id = res.json()["brand_profile_id"]

    await api_client.patch(f"/api/v1/brand/{brand_id}/voice", json={"default_tone": "casual"})
    res = await api_client.patch(f"/api/v1/brand/{brand_id}/voice", json={"default_tone": "brand"})
    assert res.status_code == 200, res.text
    assert res.json()["default_tone"] == "brand"


async def test_new_brand_has_no_default_tone(api_client):
    await signup_new_user(api_client)
    res = await api_client.post("/api/v1/brand/", json={"brand_type": "Person"})
    brand_id = res.json()["brand_profile_id"]

    res = await api_client.get(f"/api/v1/brand/{brand_id}")
    assert res.json()["default_tone"] is None


async def test_update_voice_404_for_nonexistent_brand(api_client):
    await signup_new_user(api_client)
    res = await api_client.patch(
        "/api/v1/brand/does-not-exist/voice",
        json={"voice_tone": {"tones": ["direct"]}},
    )
    assert res.status_code == 404


async def test_update_voice_400_when_nothing_provided(api_client):
    await signup_new_user(api_client)
    res = await api_client.post("/api/v1/brand/", json={"brand_type": "Person"})
    brand_id = res.json()["brand_profile_id"]

    res = await api_client.patch(f"/api/v1/brand/{brand_id}/voice", json={})
    assert res.status_code == 400


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
