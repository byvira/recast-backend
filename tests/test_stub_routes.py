"""The two old pipeline routes that never started anything now say so, and name the route that works."""
from tests.conftest import signup_new_user


async def test_the_old_audio_route_says_it_starts_nothing_and_names_the_real_one(api_client):
    await signup_new_user(api_client)
    res = await api_client.post("/api/v1/audio", json={"script": "Hello there."})
    assert res.status_code == 501
    assert "/api/v1/audio-assets/generate" in res.json()["detail"]


async def test_the_old_video_route_says_it_starts_nothing(api_client):
    await signup_new_user(api_client)
    res = await api_client.post("/api/v1/video", json={"source_url": "https://example.com/a.mp4"})
    assert res.status_code == 501, res.text
    assert "/api/v1/audio-assets/{id}/video" in res.json()["detail"]


async def test_the_audio_health_route_still_answers(api_client):
    res = await api_client.get("/api/v1/audio")
    assert res.status_code == 200
    assert res.json()["pipeline"] == "audio"
