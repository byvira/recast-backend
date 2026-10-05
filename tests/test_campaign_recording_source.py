"""A recording is a campaign source: its transcript becomes the topic, long recordings are spread across, and bad ones say why."""
from uuid import uuid4

from app.db.mongo import audio_assets, brand_profiles
from app.pipelines.campaigns import sources
from tests.conftest import signup_new_user
from tests.test_campaigns import _create_brand, _valid_body


def _words(text: str, seconds_per_word: float = 0.4) -> list[dict]:
    out, t = [], 0.0
    for w in text.split():
        out.append({"word": w, "start_s": t, "end_s": t + seconds_per_word})
        t += seconds_per_word
    return out


def test_a_short_transcript_is_used_as_it_is():
    assert sources.condense("Hello   there.\nThis is short.") == "Hello there. This is short."


def test_a_long_transcript_is_spread_across_the_whole_recording():
    import re

    long_text = " ".join(f"Sentence {i} is about subject {i}." for i in range(3000))

    out = sources.condense(long_text)

    assert len(out) <= sources.MAX_SOURCE_CHARS + 2 * sources._WINDOWS
    seen = sorted({int(n) for n in re.findall(r"Sentence (\d+) is", out)})
    assert seen[0] < 100                                       # the opening is there
    assert seen[-1] > 2700                                     # and the end, not just the first minutes
    assert any(1300 < n < 1700 for n in seen)                  # and the middle
    assert len(seen) > 100


async def test_a_recording_in_this_workspace_gives_its_transcript_and_a_reference():
    ws_id = f"ws-{uuid4()}"
    asset_id = f"a-{uuid4()}"
    await audio_assets.insert_one({"id": asset_id, "workspace_id": ws_id, "title": "Episode 4", "transcript": _words("Customers want one clear answer.")})

    text, ref = await sources.topic_from_recording(asset_id, ws_id)

    assert text == "Customers want one clear answer."
    assert ref["type"] == "audio_asset" and ref["id"] == asset_id and ref["title"] == "Episode 4"


async def test_a_missing_empty_or_too_long_recording_is_refused_with_a_plain_reason():
    ws_id = f"ws-{uuid4()}"
    no_transcript = f"a-{uuid4()}"
    too_long = f"a-{uuid4()}"
    await audio_assets.insert_one({"id": no_transcript, "workspace_id": ws_id, "transcript": []})
    await audio_assets.insert_one({"id": too_long, "workspace_id": ws_id, "transcript": [{"word": "hi", "start_s": 0, "end_s": 61 * 60}]})

    for asset_id, expected in (
        ("nope", "wasn't found"),
        (no_transcript, "no transcript yet"),
        (too_long, "up to 60 minutes"),
    ):
        try:
            await sources.topic_from_recording(asset_id, ws_id)
        except sources.SourceError as err:
            assert expected in str(err)
        else:
            raise AssertionError(f"{asset_id} should have been refused")


async def test_another_workspaces_recording_cannot_be_used():
    asset_id = f"a-{uuid4()}"
    await audio_assets.insert_one({"id": asset_id, "workspace_id": "somebody-else", "transcript": _words("Private words.")})
    try:
        await sources.topic_from_recording(asset_id, f"ws-{uuid4()}")
    except sources.SourceError as err:
        assert "wasn't found" in str(err)
    else:
        raise AssertionError("a recording from another workspace was accepted")


async def test_creating_a_campaign_from_a_recording_stores_the_transcript_as_its_topic(api_client):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    ws_id = (await brand_profiles.find_one({"id": brand_id}))["workspace_id"]
    asset_id = f"a-{uuid4()}"
    await audio_assets.insert_one({"id": asset_id, "workspace_id": ws_id, "title": "Founder talk",
                                   "transcript": _words("We cut stand-ups from five hours to thirty minutes.")})

    res = await api_client.post(
        "/api/v1/campaigns/", json=_valid_body(brand_id, source_type="audio_upload", topic_cluster=asset_id),
    )

    assert res.status_code == 201, res.text
    campaign = res.json()
    assert campaign["topic_cluster"] == "We cut stand-ups from five hours to thirty minutes."
    assert campaign["source_type"] == "audio_upload"
    assert campaign["source_ref"]["id"] == asset_id and campaign["source_ref"]["title"] == "Founder talk"


async def test_a_campaign_from_a_recording_with_no_transcript_is_a_clear_400(api_client):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    ws_id = (await brand_profiles.find_one({"id": brand_id}))["workspace_id"]
    asset_id = f"a-{uuid4()}"
    await audio_assets.insert_one({"id": asset_id, "workspace_id": ws_id, "transcript": []})

    res = await api_client.post(
        "/api/v1/campaigns/", json=_valid_body(brand_id, source_type="audio_upload", topic_cluster=asset_id),
    )

    assert res.status_code == 400
    assert "use Transcribe first" in res.json()["detail"]
