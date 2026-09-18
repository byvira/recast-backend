"""Tests for POST /api/v1/text/repurpose — Module 2 Stage 10 (Quick Recast).

QuickRecast.tsx (Library's "Recast Again" modal) used to be 100% local
mock — a setTimeout with hardcoded canned strings per channel, "Add to
Queue" was a 2-second fake flash, and it wrote to a localStorage key
nothing else ever read as a fake "auto-save to library". /text/repurpose
was already a real, working endpoint (run_text_pipeline's repurpose path
calls a dedicated repurpose agent, adapting source_platform's content for
each target platform) — nothing in the frontend ever called it.

Same root-cause bug as regenerate (Stage 5) and the SSE path (Stage 1):
GeneratedPiece has no piece_id of its own, and _save_result() discarded
the real, storage-generated ids instead of returning them, so a repurposed
piece came back from this endpoint with nothing a frontend could act on
afterward (approve/edit/schedule). Fixed by zipping _save_result()'s real
return value back onto each GeneratedPiece.piece_id.

Runs the real pipeline with the LLM mocked — never a real Groq call.
"""

from app.db.mongo import brand_profiles
from tests.conftest import create_workspace


async def _create_brand(client, ws_id: str) -> str:
    res = await client.post(
        "/api/v1/brand/", json={"brand_type": "Person"}, headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code in (200, 201), res.text
    brand_id = res.json()["brand_profile_id"]
    await brand_profiles.update_one({"id": brand_id}, {"$set": {"is_complete": True}})
    return brand_id


async def test_repurpose_returns_real_piece_ids(signup_user, mock_llm):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Repurpose WS")
    brand_id = await _create_brand(client, ws_id)

    mock_llm.set_structured({"content": "Repurposed for the target platform."})
    mock_llm.set_plain("Repurposed for the target platform.")

    res = await client.post(
        "/api/v1/text/repurpose",
        json={
            "source_content": "Most founders overestimate scale and underestimate clarity.",
            "source_platform": "LinkedIn",
            "target_platforms": ["Twitter/X", "Instagram"],
            "brand_id": brand_id,
        },
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text
    body = res.json()

    assert len(body["pieces"]) == 2
    for piece in body["pieces"]:
        assert piece["piece_id"], "piece_id is empty — pieces exist but nothing can act on them"
        assert piece["repurposed"] is True


async def test_repurposed_piece_is_real_and_fetchable(signup_user, mock_llm):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Repurpose WS")
    brand_id = await _create_brand(client, ws_id)

    mock_llm.set_structured({"content": "Adapted for Instagram."})
    mock_llm.set_plain("Adapted for Instagram.")

    res = await client.post(
        "/api/v1/text/repurpose",
        json={
            "source_content": "A long LinkedIn post about distribution strategy.",
            "source_platform": "LinkedIn",
            "target_platforms": ["Instagram"],
            "brand_id": brand_id,
        },
        headers={"X-Workspace-Id": ws_id},
    )
    piece_id = res.json()["pieces"][0]["piece_id"]

    check = await client.get(f"/api/v1/content/pieces/{piece_id}", headers={"X-Workspace-Id": ws_id})
    assert check.status_code == 200, check.text
    piece = check.json()
    assert piece["platform"] == "Instagram"
    assert piece["approval_status"] == "pending"
    assert piece["stage"] == "drafting"


async def test_repurposed_piece_appears_on_the_calendar(signup_user, mock_llm):
    """_run_single_repurpose() used to store the raw schedule_mode value
    ("now"/"scheduled") directly as publish_status, which isn't a real
    PublishStatus value — get_calendar's $or (published / has a
    publish_scheduled_at / pending-or-failed with created_at in range)
    never matched "now", so every Quick-Recast piece was invisible on
    every month's calendar, permanently. publish_status must map to the
    same "pending"/"queued" values app/agents/text/nodes.py uses for the
    normal generate path."""
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Repurpose Calendar WS")
    brand_id = await _create_brand(client, ws_id)

    mock_llm.set_structured({"content": "Repurposed for the calendar."})
    mock_llm.set_plain("Repurposed for the calendar.")

    res = await client.post(
        "/api/v1/text/repurpose",
        json={
            "source_content": "Original long-form content.",
            "source_platform": "LinkedIn",
            "target_platforms": ["Twitter/X"],
            "brand_id": brand_id,
        },
        headers={"X-Workspace-Id": ws_id},
    )
    piece_id = res.json()["pieces"][0]["piece_id"]

    check = await client.get(f"/api/v1/content/pieces/{piece_id}", headers={"X-Workspace-Id": ws_id})
    assert check.json()["publish_status"] == "pending"

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    cal_res = await client.get(
        f"/api/v1/analytics/calendar?year={now.year}&month={now.month}",
        headers={"X-Workspace-Id": ws_id},
    )
    assert cal_res.status_code == 200, cal_res.text
    all_ids = [p["id"] for day in cal_res.json()["days"].values() for p in day]
    assert piece_id in all_ids, "repurposed piece did not appear on the calendar"


async def test_repurposed_piece_can_be_approved_and_edited_for_real(signup_user, mock_llm):
    """The whole point of fixing piece_id: a repurposed piece must be a
    real, actionable piece, not just visible text with no id behind it."""
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Repurpose WS")
    brand_id = await _create_brand(client, ws_id)

    mock_llm.set_structured({"content": "Tweet-length version."})
    mock_llm.set_plain("Tweet-length version.")

    res = await client.post(
        "/api/v1/text/repurpose",
        json={
            "source_content": "Original long-form content.",
            "source_platform": "LinkedIn",
            "target_platforms": ["Twitter/X"],
            "brand_id": brand_id,
        },
        headers={"X-Workspace-Id": ws_id},
    )
    piece_id = res.json()["pieces"][0]["piece_id"]

    approve_res = await client.patch(
        f"/api/v1/content/pieces/{piece_id}/approve", headers={"X-Workspace-Id": ws_id},
    )
    assert approve_res.status_code == 200, approve_res.text
    assert approve_res.json()["approval_status"] == "approved"

    edit_res = await client.patch(
        f"/api/v1/content/pieces/{piece_id}",
        json={"content": "Manually tweaked after repurposing."},
        headers={"X-Workspace-Id": ws_id},
    )
    assert edit_res.status_code == 200, edit_res.text
    assert edit_res.json()["content"] == "Manually tweaked after repurposing."


# ─────────────────────────────────────────────────────────────────────────────
# Enforced structure_rules (Presets' "Generate with this preset" / Simulate)
# ─────────────────────────────────────────────────────────────────────────────

async def test_repurpose_with_structure_rules_returns_sections_within_limits(signup_user, mock_llm):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Repurpose WS")
    brand_id = await _create_brand(client, ws_id)

    mock_llm.set_structured({
        "sections": [
            {"section_name": "Hook", "content": "Short punchy hook."},
            {"section_name": "Body", "content": "A body section that stays well within budget."},
        ],
    })

    res = await client.post(
        "/api/v1/text/repurpose",
        json={
            "source_content": "Some long-form source content to adapt.",
            "source_platform": "Blog",
            "target_platforms": ["LinkedIn"],
            "brand_id": brand_id,
            "structure_rules": [
                {"section_name": "Hook", "char_limit": 100, "guidelines": "Grab attention"},
                {"section_name": "Body", "char_limit": 300, "guidelines": "Explain the idea"},
            ],
        },
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text
    piece = res.json()["pieces"][0]
    assert piece["quality_passed"] is True
    assert piece["sections"] is not None
    assert len(piece["sections"]) == 2
    assert piece["sections"][0]["section_name"] == "Hook"
    assert piece["sections"][0]["char_limit"] == 100
    # content is the flattened join of the sections — every existing
    # consumer (publish, chip refine, frontend cards) only reads this.
    assert "Short punchy hook." in piece["content"]
    assert "A body section" in piece["content"]


async def test_repurpose_structure_rules_flags_when_section_exceeds_limit_after_retry(signup_user, mock_llm):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Repurpose WS")
    brand_id = await _create_brand(client, ws_id)

    # The mock returns the exact same (over-limit) section on both the
    # initial call and the one retry — a real LLM might fix it on retry,
    # but this exercises the "still over after retry -> flagged" branch,
    # which the always-identical mock response makes deterministic to test.
    over_limit_content = "x" * 150
    mock_llm.set_structured({
        "sections": [{"section_name": "Hook", "content": over_limit_content}],
    })

    res = await client.post(
        "/api/v1/text/repurpose",
        json={
            "source_content": "Some long-form source content to adapt.",
            "source_platform": "Blog",
            "target_platforms": ["LinkedIn"],
            "brand_id": brand_id,
            "structure_rules": [
                {"section_name": "Hook", "char_limit": 50, "guidelines": "Grab attention"},
            ],
        },
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text
    piece = res.json()["pieces"][0]
    assert piece["quality_passed"] is False
    assert piece["flagged_for_review"] is True
    assert any("Hook" in issue and "character limit" in issue for issue in piece["quality_issues"])
