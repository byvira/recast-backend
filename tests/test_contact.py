"""The public contact form: it stores the message, refuses bad input and respects the bot check."""

import pytest

from app.core.config import settings
from app.db.mongo import contact_messages
from tests.conftest import unique_email


@pytest.fixture(autouse=True)
async def _clean():
    yield
    await contact_messages.delete_many({"email": {"$regex": "@example.com$"}})


def _body(**over):
    return {"name": "Maya", "email": unique_email(), "message": "We are a team of twelve and need more seats.", **over}


async def test_a_message_is_stored(api_client):
    body = _body(topic="seats")
    res = await api_client.post("/api/v1/contact", json=body)
    assert res.status_code == 200 and res.json() == {"ok": True}
    saved = await contact_messages.find_one({"email": body["email"].lower()})
    assert saved["name"] == "Maya" and saved["topic"] == "seats"


async def test_a_bad_email_or_short_message_is_refused(api_client):
    assert (await api_client.post("/api/v1/contact", json=_body(email="nope"))).status_code == 422
    assert (await api_client.post("/api/v1/contact", json=_body(message="hi"))).status_code == 422


async def test_the_bot_check_is_required_when_a_secret_is_set(api_client, monkeypatch):
    monkeypatch.setattr(settings, "TURNSTILE_SECRET_KEY", "secret", raising=False)
    res = await api_client.post("/api/v1/contact", json=_body())
    assert res.status_code == 400
