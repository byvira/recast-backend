"""Tests for POST /api/v1/text/angles — Feature 8's real "3 Fresh Angles".

Library's "Repurpose with 3 Fresh Angles" button used to open the generic
Quick Recast modal — there was no real angle concept in the backend to
preview at all (angle_used/angle_score on generated pieces were hardcoded
"auto"/0 placeholders). This is the real capability: 3 genuinely distinct
full rewrites, not tone variations, previewable before committing to one.

Runs with the LLM mocked — never a real Groq call.
"""

from uuid import uuid4

from app.models.text import AgentTask
from app.pipelines.text import angles as angles_module
from tests.conftest import create_workspace


async def _create_brand(client, ws_id: str) -> str:
    res = await client.post(
        "/api/v1/brand/", json={"brand_type": "Person"}, headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code in (200, 201), res.text
    return res.json()["brand_profile_id"]


async def test_generate_angles_returns_three_distinct_variants(signup_user, mock_llm):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Angles WS")
    brand_id = await _create_brand(client, ws_id)

    mock_llm.set_structured({
        "angles": [
            {"name": "Contrarian", "rationale": "Challenges the default assumption.", "content": "Most advice here is wrong."},
            {"name": "Personal story", "rationale": "Leads with the founder's own moment.", "content": "Three years ago I almost quit."},
            {"name": "Concrete outcome", "rationale": "Leads with the real number.", "content": "We cut churn by 40% in one quarter."},
        ],
    })

    res = await client.post(
        "/api/v1/text/angles",
        json={
            "content": "Original content to generate angles from.",
            "platform": "LinkedIn",
            "brand_id": brand_id,
        },
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert len(body["angles"]) == 3
    names = [a["name"] for a in body["angles"]]
    assert names == ["Contrarian", "Personal story", "Concrete outcome"]
    assert all(a["rationale"] for a in body["angles"])


async def test_generate_angles_strips_em_dashes(signup_user, mock_llm):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Angles WS")
    brand_id = await _create_brand(client, ws_id)

    mock_llm.set_structured({
        "angles": [
            {"name": "Contrarian", "rationale": "r1", "content": "This is bold — and true."},
        ],
    })

    res = await client.post(
        "/api/v1/text/angles",
        json={"content": "Source", "platform": "LinkedIn", "brand_id": brand_id},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text
    assert "—" not in res.json()["angles"][0]["content"]


async def test_generate_angles_502_when_llm_returns_nothing_usable(signup_user, mock_llm):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Angles WS")
    brand_id = await _create_brand(client, ws_id)

    mock_llm.set_structured({"angles": []})

    res = await client.post(
        "/api/v1/text/angles",
        json={"content": "Source", "platform": "LinkedIn", "brand_id": brand_id},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 502


async def test_angles_prompt_preserves_source_language(monkeypatch):
    """Angles rewrite *existing* content — the source's own language must
    win outright, unlike fresh generation. Before this fix, generate.jinja
    had no language slot at all and every angle came back in English
    regardless of input (e.g. Tamil source content -> English angles)."""
    captured: dict[str, str] = {}

    async def _fake_call_llm_structured(prompt: str, *args, **kwargs):
        captured["prompt"] = prompt
        return {
            "angles": [
                {"name": "Contrarian", "rationale": "r", "content": "c"},
                {"name": "Personal story", "rationale": "r", "content": "c"},
                {"name": "Concrete outcome", "rationale": "r", "content": "c"},
            ],
        }

    monkeypatch.setattr(angles_module, "call_llm_structured", _fake_call_llm_structured)

    tamil_content = "வளர்ச்சி குழுக்கள் ஒவ்வொரு வாரமும் ஒரு முழு வேலை நாளை மறுவடிவமைப்பில் வீணடிக்கின்றன."
    task = AgentTask(
        agent="angles",
        platform=None,
        content=tamil_content,
        brand_context="A B2B SaaS brand.",
        session_id=str(uuid4()),
    )

    result = await angles_module.run_angles_agent(task)

    assert result.success is True
    assert "Tamil" in captured["prompt"]


async def test_generate_angles_blocked_for_viewer(signup_user, make_client):
    from tests.conftest import invite_and_accept

    owner_client, _ = await signup_user()
    ws_id = await create_workspace(owner_client, "Angles RBAC WS")
    brand_id = await _create_brand(owner_client, ws_id)
    viewer_client, _ = await invite_and_accept(owner_client, make_client, ws_id, "viewer")

    res = await viewer_client.post(
        "/api/v1/text/angles",
        json={"content": "Source", "platform": "LinkedIn", "brand_id": brand_id},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 403
