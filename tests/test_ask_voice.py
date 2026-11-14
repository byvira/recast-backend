"""Voice in the Ask desk: a recording becomes words the person checks before sending. The speech service is a stand-in."""
from uuid import uuid4

import pytest

from app.channels import engine
from tests.conftest import create_workspace


@pytest.fixture
async def asker(signup_user):
    client, _ = await signup_user()
    ws = await create_workspace(client, f"Voice {uuid4().hex[:6]}")
    return client, {"X-Workspace-Id": ws}


def audio(data=b"abc", name="voice.webm"):
    return {"file": (name, data, "audio/webm")}


async def test_a_recording_becomes_words_and_the_language_heard(asker, monkeypatch):
    client, h = asker

    async def heard(data, name):
        assert data == b"abc" and name == "voice.webm"
        return "என் திட்டத்தில் என்ன இருக்கிறது", "ta"

    monkeypatch.setattr(engine, "default_transcribe", heard)
    res = await client.post("/api/v1/ask/voice", headers=h, files=audio())
    assert res.status_code == 200 and res.json() == {"text": "என் திட்டத்தில் என்ன இருக்கிறது", "language": "ta"}


async def test_silence_an_empty_file_a_huge_file_and_a_speech_outage_each_get_their_own_plain_answer(asker, monkeypatch):
    client, h = asker

    async def silent(data, name):
        return "", None

    monkeypatch.setattr(engine, "default_transcribe", silent)
    assert (await client.post("/api/v1/ask/voice", headers=h, files=audio())).status_code == 422
    assert (await client.post("/api/v1/ask/voice", headers=h, files=audio(b""))).status_code == 422
    assert (await client.post("/api/v1/ask/voice", headers=h, files=audio(b"x" * (10 * 1024 * 1024 + 5)))).status_code == 413

    async def down(data, name):
        raise RuntimeError("every provider down")

    monkeypatch.setattr(engine, "default_transcribe", down)
    out = await client.post("/api/v1/ask/voice", headers=h, files=audio())
    assert out.status_code == 503 and "Nothing was charged" in out.json()["detail"]


async def test_voice_needs_a_signed_in_member(api_client):
    assert (await api_client.post("/api/v1/ask/voice", files=audio())).status_code in (401, 403)
