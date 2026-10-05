"""A brand can carry its own language, and it is used when nothing more specific (the request, the post, the campaign) says."""
from app.db.mongo import content_pieces
from tests.conftest import signup_new_user
from tests.test_campaigns import _create_brand, _valid_body


async def test_the_brand_language_can_be_set_read_and_cleared(api_client):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)

    assert (await api_client.get(f"/api/v1/brand/{brand_id}")).json()["language"] is None
    res = await api_client.patch(f"/api/v1/brand/{brand_id}/language", json={"language": " ta+en "})
    assert res.status_code == 200 and res.json()["language"] == "ta+en"
    assert (await api_client.get(f"/api/v1/brand/{brand_id}")).json()["language"] == "ta+en"

    cleared = await api_client.patch(f"/api/v1/brand/{brand_id}/language", json={"language": ""})
    assert cleared.json()["language"] is None
    assert (await api_client.patch("/api/v1/brand/not-a-brand/language", json={"language": "en"})).status_code == 404


async def test_a_campaign_without_its_own_language_writes_in_the_brand_language(api_client, mock_llm):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    await api_client.patch(f"/api/v1/brand/{brand_id}/language", json={"language": "hi+en"})
    created = await api_client.post(
        "/api/v1/campaigns/", json=_valid_body(brand_id, cadence={"frequency": "manual", "days_per_batch": 1}),
    )
    campaign_id = created.json()["id"]
    mock_llm.set_structured({"angles": ["Only angle"]})
    mock_llm.set_plain("Real generated content for this campaign day.")

    assert (await api_client.post(f"/api/v1/campaigns/{campaign_id}/generate-next-batch")).status_code == 200
    pieces = await content_pieces.find({"campaign_id": campaign_id}).to_list(length=None)
    assert pieces and all(p.get("language") == "hi+en" for p in pieces)


async def test_the_campaign_language_beats_the_brand_language(api_client, mock_llm):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    await api_client.patch(f"/api/v1/brand/{brand_id}/language", json={"language": "hi+en"})
    created = await api_client.post(
        "/api/v1/campaigns/",
        json=_valid_body(brand_id, language="ta+en", cadence={"frequency": "manual", "days_per_batch": 1}),
    )
    campaign_id = created.json()["id"]
    mock_llm.set_structured({"angles": ["Only angle"]})
    mock_llm.set_plain("Real generated content for this campaign day.")

    assert (await api_client.post(f"/api/v1/campaigns/{campaign_id}/generate-next-batch")).status_code == 200
    pieces = await content_pieces.find({"campaign_id": campaign_id}).to_list(length=None)
    assert pieces and all(p.get("language") == "ta+en" for p in pieces)
