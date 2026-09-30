"""3 Fresh Angles for audio recordings and images (not only written posts).

The source-text builder is pure. The route tests run with the LLM mocked and
never call a real provider.
"""

import pytest

from app.db.mongo import audio_assets, image_assets
from app.pipelines.text.angle_sources import NO_TEXT_MESSAGE, text_for_angles
from tests.conftest import create_workspace
from tests.test_angles import _create_brand


# ── The text builder ─────────────────────────────────────────────────────────

def test_audio_uses_the_script_when_there_is_one():
    doc = {"title": "Episode 1", "script": "Good systems beat willpower.", "transcript": [{"word": "ignored"}]}
    assert text_for_angles("audio", doc) == "Episode 1\n\nGood systems beat willpower."


def test_audio_falls_back_to_the_transcript_words():
    doc = {"title": "Interview", "script": None, "transcript": [{"word": "Buffer"}, {"word": "wins"}, {"word": ""}]}
    assert text_for_angles("audio", doc) == "Interview\n\nBuffer wins"


def test_audio_with_no_words_has_no_text():
    assert text_for_angles("audio", {"title": "Silent", "script": "  ", "transcript": []}) is None


def test_image_uses_headline_description_alt_text_and_prompt_without_repeats():
    doc = {
        "title": "Quote card",
        "slides": [
            {"text_content": {"headline": "Clarity beats scale"}},
            {"text_content": {"headline": "Clarity beats scale"}},
            {"text_content": {"headline": "Start small"}},
        ],
        "og_description": "A card about focus",
        "alt_text": "Dark card with white text",
        "prompt": "calm sunrise",
    }
    text = text_for_angles("image", doc)
    assert text.startswith("Quote card\n\nClarity beats scale")
    assert text.count("Clarity beats scale") == 1
    for part in ("Start small", "A card about focus", "Dark card with white text", "calm sunrise"):
        assert part in text


def test_an_image_with_no_words_has_no_text():
    assert text_for_angles("image", {"title": "Photo", "slides": [{"text_content": {}}]}) is None


def test_a_title_already_in_the_text_is_not_repeated():
    assert text_for_angles("audio", {"title": "Hello", "script": "Hello world"}) == "Hello world"


def test_each_kind_has_a_plain_no_text_message():
    assert "transcri" in NO_TEXT_MESSAGE["audio"].lower()
    assert "headline" in NO_TEXT_MESSAGE["image"].lower()


# ── The route ────────────────────────────────────────────────────────────────

async def _setup(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Angle Assets WS")
    return client, profile, ws_id, await _create_brand(client, ws_id)


def _angles_reply():
    return {"angles": [
        {"name": "Contrarian", "rationale": "r1", "content": "one"},
        {"name": "Personal story", "rationale": "r2", "content": "two"},
        {"name": "Concrete outcome", "rationale": "r3", "content": "three"},
    ]}


@pytest.fixture
def seen_content(monkeypatch):
    """Records the text the angle prompt was built from, and returns three angles."""
    from app.api.v1 import text as text_module

    seen = {}

    async def fake_agent(task):
        seen["content"] = task.content
        from app.models.text import AgentResult

        return AgentResult(agent="angles", success=True, output=_angles_reply())

    monkeypatch.setattr(text_module, "run_angles_agent", fake_agent)
    return seen


async def _post(client, ws_id, body):
    return await client.post("/api/v1/text/angles", json=body, headers={"X-Workspace-Id": ws_id})


async def test_angles_from_an_audio_recording(signup_user, seen_content):
    client, profile, ws_id, brand_id = await _setup(signup_user)
    await audio_assets.insert_one({
        "id": "aud-1", "workspace_id": ws_id, "brand_id": brand_id, "title": "Ep 1",
        "script": "Good systems beat willpower every time.", "transcript": [],
    })

    res = await _post(client, ws_id, {"platform": "LinkedIn", "brand_id": brand_id, "asset_kind": "audio", "asset_id": "aud-1"})
    assert res.status_code == 200, res.text
    assert len(res.json()["angles"]) == 3
    assert "Good systems beat willpower" in seen_content["content"]


async def test_angles_from_an_image(signup_user, seen_content):
    client, profile, ws_id, brand_id = await _setup(signup_user)
    await image_assets.insert_one({
        "id": "img-1", "workspace_id": ws_id, "brand_id": brand_id, "title": "Card",
        "slides": [{"text_content": {"headline": "Clarity beats scale"}}],
    })

    res = await _post(client, ws_id, {"platform": "LinkedIn", "brand_id": brand_id, "asset_kind": "image", "asset_id": "img-1"})
    assert res.status_code == 200, res.text
    assert "Clarity beats scale" in seen_content["content"]


async def test_an_asset_with_no_words_gets_a_clear_message(signup_user, seen_content):
    client, profile, ws_id, brand_id = await _setup(signup_user)
    await audio_assets.insert_one({"id": "aud-2", "workspace_id": ws_id, "brand_id": brand_id, "title": "Silent", "script": None, "transcript": []})

    res = await _post(client, ws_id, {"platform": "LinkedIn", "brand_id": brand_id, "asset_kind": "audio", "asset_id": "aud-2"})
    assert res.status_code == 400
    assert res.json()["detail"] == NO_TEXT_MESSAGE["audio"]
    assert "content" not in seen_content


async def test_an_unknown_asset_is_not_found(signup_user, seen_content):
    client, profile, ws_id, brand_id = await _setup(signup_user)
    res = await _post(client, ws_id, {"platform": "LinkedIn", "brand_id": brand_id, "asset_kind": "image", "asset_id": "nope"})
    assert res.status_code == 404


async def test_another_workspaces_asset_is_not_reachable(signup_user, seen_content):
    client, profile, ws_id, brand_id = await _setup(signup_user)
    await image_assets.insert_one({
        "id": "img-other", "workspace_id": "some-other-workspace", "brand_id": "x", "title": "Secret",
        "slides": [{"text_content": {"headline": "Private"}}],
    })
    res = await _post(client, ws_id, {"platform": "LinkedIn", "brand_id": brand_id, "asset_kind": "image", "asset_id": "img-other"})
    assert res.status_code == 404
    assert "content" not in seen_content


async def test_no_content_and_no_asset_is_rejected(signup_user, seen_content):
    client, profile, ws_id, brand_id = await _setup(signup_user)
    res = await _post(client, ws_id, {"platform": "LinkedIn", "brand_id": brand_id})
    assert res.status_code == 400
