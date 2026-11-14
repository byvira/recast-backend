"""WhatsApp through Twilio: only a call signed with the account's auth token is read, Twilio's form fields become the engine's one shape, a reply with buttons
becomes numbered choices that the person's number turns back into the button press, and a signed link message gets a reply posted to Twilio. No network is called."""
import asyncio
import json
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest
from twilio.request_validator import RequestValidator

from app.channels import link as links
from app.channels import twilio_whatsapp as tw
from app.core.config import settings
from app.db.mongo import get_db
from tests.conftest import create_workspace

URL = "http://test/api/channels/twilio-whatsapp/webhook"
TOKEN = "twilio-token"


@pytest.fixture
def keys(monkeypatch):
    for name, value in (("TWILIO_ACCOUNT_SID", "ACtest"), ("TWILIO_AUTH_TOKEN", TOKEN), ("TWILIO_WHATSAPP_NUMBER", "+14155238886"), ("TWILIO_WHATSAPP_WEBHOOK_URL", URL)):
        monkeypatch.setattr(settings, name, value)
    tw._choices.clear()


def signed(form: dict, token: str = TOKEN) -> dict:
    return {"X-Twilio-Signature": RequestValidator(token).compute_signature(URL, form)}


def text_form(body: str, sender: str = "whatsapp:+919999900077", sid: str = "SM1") -> dict:
    return {"From": sender, "To": "whatsapp:+14155238886", "Body": body, "MessageSid": sid, "NumMedia": "0"}


async def test_a_call_without_the_right_signature_is_refused_and_a_switched_off_channel_does_not_answer(api_client, keys, monkeypatch):
    seen: list = []

    async def spy(adapter, msg):
        seen.append(msg)

    monkeypatch.setattr("app.api.channels_webhooks._safely", spy)
    form = text_form("hi")
    assert (await api_client.post(URL, data=form)).status_code == 403
    assert (await api_client.post(URL, data=form, headers=signed(form, "other-token"))).status_code == 403
    assert (await api_client.post(URL, data={**form, "Body": "changed"}, headers=signed(form))).status_code == 403          # a field changed after signing
    assert seen == []
    ok = await api_client.post(URL, data=form, headers=signed(form))
    assert ok.status_code == 200 and ok.json() == {"ok": True}
    monkeypatch.setattr(settings, "TWILIO_WHATSAPP_NUMBER", "")
    assert (await api_client.post(URL, data=form, headers=signed(form))).status_code == 404


def test_twilio_fields_become_the_engines_one_shape():
    text = tw.parse(text_form("  Hello there "))
    assert text.channel == "whatsapp" and text.sender == "919999900077" and text.kind == "text" and text.text == "Hello there" and text.message_id == "SM1"
    voice = tw.parse({**text_form(""), "NumMedia": "1", "MediaUrl0": "https://api.twilio.com/m/1", "MediaContentType0": "audio/ogg"})
    assert voice.kind == "voice" and voice.media == {"url": "https://api.twilio.com/m/1"}
    photo = tw.parse({**text_form("look like this"), "NumMedia": "1", "MediaUrl0": "https://api.twilio.com/m/2", "MediaContentType0": "image/jpeg"})
    assert photo.kind == "image" and photo.text == "look like this"
    assert tw.parse({**text_form(""), "NumMedia": "1", "MediaUrl0": "https://x/y", "MediaContentType0": "video/mp4"}) is None
    assert tw.parse({"MessageStatus": "delivered", "MessageSid": "SM2"}) is None and tw.parse(text_form("")) is None


async def test_buttons_become_numbered_choices_and_the_number_comes_back_as_the_button(keys):
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append({k: v[0] for k, v in parse_qs(request.content.decode()).items()} | {"auth": request.headers.get("authorization", "")[:6]})
        return httpx.Response(201, json={"sid": "SMx"})

    adapter = tw.TwilioWhatsAppAdapter(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await adapter.send("919999900077", "Ready to start?", [("Go", "go:run1"), ("Cancel", "no:run1")]) is True
    assert sent[0]["From"] == "whatsapp:+14155238886" and sent[0]["To"] == "whatsapp:+919999900077"
    assert "1. Go" in sent[0]["Body"] and "2. Cancel" in sent[0]["Body"] and sent[0]["auth"] == "Basic "
    press = tw.parse(text_form("1", sid="SM3"))
    assert press.kind == "button" and press.payload == "go:run1"
    assert tw.parse(text_form("7", sid="SM4")).kind == "text"            # not one of the choices: an ordinary message


async def test_a_send_that_fails_returns_false_and_never_raises(keys):
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": 63015, "message": "not joined"})

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    assert await tw.TwilioWhatsAppAdapter(client=httpx.AsyncClient(transport=httpx.MockTransport(refuse))).send("91", "hi") is False
    assert await tw.TwilioWhatsAppAdapter(client=httpx.AsyncClient(transport=httpx.MockTransport(boom))).send("91", "hi") is False


@pytest.fixture
async def member(signup_user):
    client, profile = await signup_user()
    ws = await create_workspace(client, f"Twilio {uuid4().hex[:6]}")
    await links.create_indexes()
    await get_db()["channel_links"].delete_many({})
    await get_db()["channel_codes"].delete_many({})
    return client, profile["id"], ws, {"X-Workspace-Id": ws}


async def test_a_signed_link_message_links_the_chat_and_the_reply_goes_to_twilio(member, keys, monkeypatch):
    from app.api import channels_webhooks

    client, user, ws, h = member
    page = (await client.get("/api/v1/channels", headers=h)).json()
    assert [c["channel"] for c in page["available"]] == ["whatsapp"] and page["available"][0]["address"] == "+14155238886"
    made = (await client.post("/api/v1/channels/link-code", headers=h, json={"channel": "whatsapp"})).json()
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        return httpx.Response(201, json={"sid": "SMy"})

    stand_in = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("app.channels.pool.shared", lambda timeout: stand_in)
    form = text_form(made["message"], sid=f"SM{uuid4().hex[:8]}")
    assert (await client.post(URL, data=form, headers=signed(form))).status_code == 200
    await asyncio.gather(*list(channels_webhooks._tasks))
    assert sent, "no reply was sent to Twilio"
    assert all(s["To"] == "whatsapp:+919999900077" and s["From"] == "whatsapp:+14155238886" for s in sent)
    assert "linked" in json.dumps(sent[0]).lower()
    assert (await get_db()["channel_links"].count_documents({"workspace_id": ws})) == 1
