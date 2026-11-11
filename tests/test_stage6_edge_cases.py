"""Stage 6 edge cases found while walking each module (see docs/Edgecase_Failures_flow.md)."""

import pytest
from pydantic import ValidationError

from app.models.campaign import CreateCampaignRequest, UpdateCampaignRequest


def _campaign(**over):
    base = dict(name="Launch", brand_id="b", topic_cluster="a topic", platforms=["LinkedIn"])
    base.update(over)
    return CreateCampaignRequest(**base)


def test_a_campaign_name_is_tidied_and_cannot_be_empty_or_huge():
    assert _campaign(name="  Spring   launch  ").name == "Spring launch"
    for bad in ("", "   ", "x" * 201):
        with pytest.raises(ValidationError):
            _campaign(name=bad)
    with pytest.raises(ValidationError):
        UpdateCampaignRequest(name="   ")
    assert UpdateCampaignRequest().name is None  # leaving the name out leaves it alone


def test_a_campaign_needs_a_topic_link_or_recording():
    for bad in ("", "  \n"):
        with pytest.raises(ValidationError):
            _campaign(topic_cluster=bad)


def test_a_platform_listed_twice_makes_one_post_a_day_not_two():
    assert _campaign(platforms=["LinkedIn", "Blog", "LinkedIn"]).platforms == ["LinkedIn", "Blog"]
    assert _campaign(platforms_by_day=[["Blog", "Blog"], ["LinkedIn"]]).platforms_by_day == [["Blog"], ["LinkedIn"]]
    assert UpdateCampaignRequest(platforms=["Blog", "Blog"]).platforms == ["Blog"]
    assert UpdateCampaignRequest().platforms is None


async def test_the_platform_filter_on_account_results_has_a_sensible_size(signup_user):
    client, _ = await signup_user()
    assert (await client.get("/api/v1/analytics/accounts", params={"platforms": "instagram," * 200})).status_code == 422
    assert (await client.get("/api/v1/analytics/accounts", params={"platforms": "instagram,threads"})).status_code in (200, 404)


# ── Rows that were shown by reading the code only, now with a test each ──────
def test_a_post_with_no_reach_has_a_rate_of_zero_not_an_error():
    from app.pipelines.analytics.base import AnalyticsFetcher

    rate = AnalyticsFetcher._compute_engagement_rate
    assert rate(None, likes=5, comments=2, shares=1, reach=0) == 0.0  # N1
    assert rate(None, likes=5, comments=3, shares=2, reach=100) == 10.0


async def test_a_script_over_the_limit_is_refused_before_anything_is_made(signup_user, monkeypatch):  # A1
    from app.core.config import settings
    from tests.test_audio_assets import _brand
    from tests.conftest import create_workspace

    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Audio limit WS")
    brand_id = await _brand(client, ws_id)
    monkeypatch.setattr(settings, "AUDIO_SCRIPT_MAX_CHARS", 50)
    res = await client.post(
        "/api/v1/audio-assets/generate", json={"title": "Long", "brand_id": brand_id, "script": "word " * 40}, headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 422 and "too long to narrate" in res.json()["detail"]
    assert "Split it into parts" in res.json()["detail"]


async def test_the_library_page_size_is_capped_at_one_hundred(signup_user):  # H6
    client, _ = await signup_user()
    assert (await client.get("/api/v1/content/sessions", params={"limit": 101})).status_code == 422
    assert (await client.get("/api/v1/content/sessions", params={"limit": 0})).status_code == 422
    assert (await client.get("/api/v1/content/sessions", params={"limit": 100})).status_code == 200


async def test_a_bulk_action_takes_at_most_two_hundred_posts_and_at_least_one(signup_user):  # H5
    client, _ = await signup_user()
    too_many = {"piece_ids": [f"p{i}" for i in range(201)]}
    assert (await client.post("/api/v1/content/pieces/approve-many", json=too_many)).status_code == 422
    assert (await client.post("/api/v1/content/pieces/approve-many", json={"piece_ids": []})).status_code == 422


async def test_an_admin_cannot_hand_out_the_owner_role(api_client, make_client):  # V3
    from tests.conftest import create_workspace, invite_and_accept, signup_new_user, unique_email

    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Owner role WS", tier="large")
    admin_client, _ = await invite_and_accept(api_client, make_client, ws_id, "admin")
    refused = await admin_client.post(f"/api/v1/invites/{ws_id}", json={"email": unique_email(), "role": "owner"})
    assert refused.status_code == 403
    allowed = await admin_client.post(f"/api/v1/invites/{ws_id}", json={"email": unique_email(), "role": "editor"})
    assert allowed.status_code == 201  # an admin may still invite everyone else


async def test_deleting_the_default_voice_hands_the_default_to_another(api_client):  # B3
    from app.db.mongo import brand_profiles
    from tests.conftest import signup_new_user

    await signup_new_user(api_client)
    first = (await api_client.post("/api/v1/brand/", json={"brand_type": "Person"})).json()["brand_profile_id"]
    second = (await api_client.post("/api/v1/brand/", json={"brand_type": "Person"})).json()["brand_profile_id"]
    await brand_profiles.update_many({"id": {"$in": [first, second]}}, {"$set": {"is_default": False}})
    await brand_profiles.update_one({"id": first}, {"$set": {"is_default": True}})

    assert (await api_client.delete(f"/api/v1/brand/{first}")).status_code == 200
    assert (await brand_profiles.find_one({"id": second}))["is_default"] is True
    assert await brand_profiles.find_one({"id": first}) is None


async def test_a_run_that_does_not_fit_the_credits_is_refused_whole_and_gives_back_what_it_set_aside(monkeypatch):  # T4, T5
    from fastapi import HTTPException

    from app.billing import credits
    from app.models.text import Platform
    from app.pipelines.text import orchestrator

    reserved, released, written = [], [], []

    class _Hold:
        def __init__(self, step):
            self.step = step

    async def reserve(workspace_id, user_id, action, units=1, *, step=None, **_):
        if len(reserved) == 1:
            raise HTTPException(status_code=402, detail="Not enough credits.")
        reserved.append(step)
        return _Hold(step)

    async def release(hold):
        released.append(hold.step)

    async def write(*args, **kwargs):
        written.append(1)

    monkeypatch.setattr(credits, "reserve", reserve)
    monkeypatch.setattr(credits, "release", release)
    monkeypatch.setattr(orchestrator, "_run_text_pipeline", write)

    with pytest.raises(HTTPException) as err:
        await orchestrator.run_text_pipeline(workspace_id="w", user_id="u", platforms=[Platform("LinkedIn"), Platform("Blog")])
    assert err.value.status_code == 402
    assert written == [], "nothing is written for a run that does not fit"
    assert released == reserved and len(reserved) == 1, "the credits set aside for the first platform are given back"
