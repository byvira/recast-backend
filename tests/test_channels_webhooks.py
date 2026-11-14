"""The chat services' doors: only a call that proves it is from Meta or Telegram is read; a forged or unsigned call is refused; the parsers turn each
service's messages into the engine's one shape; and the member's linking routes work. No network is called."""
import hashlib
import hmac
import json
from uuid import uuid4

import pytest

from app.channels import telegram, whatsapp
from app.channels import link as links
from app.core.config import settings
from app.db.mongo import get_db
from tests.conftest import create_workspace

W = "/api/channels/whatsapp/webhook"
T = "/api/channels/telegram/webhook"


@pytest.fixture
def keys(monkeypatch):
    for name, value in (("WHATSAPP_VERIFY_TOKEN", "verify-me"), ("WHATSAPP_APP_SECRET", "app-secret"), ("WHATSAPP_ACCESS_TOKEN", "tok"), ("WHATSAPP_PHONE_NUMBER_ID", "111"),
                        ("WHATSAPP_DISPLAY_NUMBER", "+1 555 0100"), ("TELEGRAM_BOT_TOKEN", "bot"), ("TELEGRAM_WEBHOOK_SECRET", "tg-secret"), ("TELEGRAM_BOT_USERNAME", "EkavoBot")):
        monkeypatch.setattr(settings, name, value)


def sign(body: bytes, secret="app-secret") -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def wa_payload(*messages):
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {"messages": list(messages)}}]}]}


# ── WhatsApp ─────────────────────────────────────────────────────────────────

async def test_meta_checks_the_address_with_the_verify_token_only(api_client, keys):
    ok = await api_client.get(W, params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "12345"})
    assert ok.status_code == 200 and ok.text == "12345"
    assert (await api_client.get(W, params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1"})).status_code == 403
    assert (await api_client.get(W, params={"hub.mode": "subscribe", "hub.challenge": "1"})).status_code == 403


async def test_a_whatsapp_call_without_the_right_signature_is_refused_before_it_is_read(api_client, keys, monkeypatch):
    seen: list = []

    async def spy(adapter, msg):
        seen.append(msg)

    monkeypatch.setattr("app.api.channels_webhooks._safely", spy)
    body = json.dumps(wa_payload({"from": "919999900001", "id": "wamid.1", "type": "text", "text": {"body": "hi"}})).encode()
    assert (await api_client.post(W, content=body)).status_code == 403
    assert (await api_client.post(W, content=body, headers={"X-Hub-Signature-256": sign(body, "other-secret")})).status_code == 403
    assert (await api_client.post(W, content=body + b" ", headers={"X-Hub-Signature-256": sign(body)})).status_code == 403          # the body was changed after signing
    assert seen == []
    ok = await api_client.post(W, content=body, headers={"X-Hub-Signature-256": sign(body)})
    assert ok.status_code == 200 and ok.json() == {"ok": True}


async def test_a_channel_with_no_keys_set_does_not_answer(api_client):
    assert (await api_client.post(W, content=b"{}")).status_code == 404
    assert (await api_client.post(T, json={})).status_code == 404


def test_whatsapp_messages_become_the_engines_one_shape_and_receipts_are_skipped():
    out = whatsapp.parse(wa_payload(
        {"from": "91", "id": "a", "type": "text", "text": {"body": "write a post"}},
        {"from": "91", "id": "b", "type": "audio", "audio": {"id": "media-1"}},
        {"from": "91", "id": "c", "type": "image", "image": {"id": "media-2", "caption": "like this"}},
        {"from": "91", "id": "d", "type": "interactive", "interactive": {"button_reply": {"id": "go:abc", "title": "Go ahead"}}},
        {"from": "91", "id": "e", "type": "sticker"},
        {"id": "no-sender", "type": "text"},
    ) | {"statuses": [{"id": "x"}]})
    assert [(m.kind, m.text, m.payload, (m.media or {}).get("id")) for m in out] == [
        ("text", "write a post", "", None), ("voice", "", "", "media-1"), ("image", "like this", "", "media-2"), ("button", "", "go:abc", None)]
    assert all(m.channel == "whatsapp" and m.sender == "91" for m in out)


async def test_whatsapp_replies_carry_buttons_and_stay_inside_the_limits(monkeypatch, keys):
    import httpx

    sent: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append((str(request.url), json.loads(request.content), request.headers["authorization"]))
        return httpx.Response(200, json={"messages": [{"id": "wamid.out"}]})

    adapter = whatsapp.WhatsAppAdapter(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await adapter.send("91", "x" * 5000) is True
    assert await adapter.send("91", "Price is 4 credits", [("Go ahead", "go:" + "a" * 32), ("A very long cancel label here", "no:b"), ("c", "c"), ("d", "d")]) is True
    url, text_body, auth = sent[0]
    assert url.endswith("/111/messages") and auth == "Bearer tok" and text_body["type"] == "text" and len(text_body["text"]["body"]) == 4000
    buttons = sent[1][1]["interactive"]["action"]["buttons"]
    assert len(buttons) == 3 and all(len(b["reply"]["title"]) <= 20 for b in buttons)


async def test_a_whatsapp_send_that_fails_returns_false_and_never_raises(keys):
    import httpx

    def refuse(request):
        return httpx.Response(401, json={"error": "bad token"})

    assert await whatsapp.WhatsAppAdapter(client=httpx.AsyncClient(transport=httpx.MockTransport(refuse))).send("91", "hi") is False

    def boom(request):
        raise httpx.ConnectError("down")

    assert await whatsapp.WhatsAppAdapter(client=httpx.AsyncClient(transport=httpx.MockTransport(boom))).send("91", "hi") is False


# ── Telegram ─────────────────────────────────────────────────────────────────

async def test_a_telegram_call_without_the_secret_is_refused(api_client, keys, monkeypatch):
    seen: list = []

    async def spy(adapter, msg):
        seen.append(msg)

    monkeypatch.setattr("app.api.channels_webhooks._safely", spy)
    update = {"update_id": 7, "message": {"chat": {"id": 55, "type": "private"}, "text": "hi"}}
    assert (await api_client.post(T, json=update)).status_code == 403
    assert (await api_client.post(T, json=update, headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"})).status_code == 403
    assert seen == []
    assert (await api_client.post(T, json=update, headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"})).status_code == 200


def test_telegram_updates_become_the_engines_one_shape_and_a_group_is_never_read():
    chat = {"id": 55, "type": "private"}
    assert telegram.parse({"update_id": 1, "message": {"chat": chat, "text": "hello"}}).text == "hello"
    voice = telegram.parse({"update_id": 2, "message": {"chat": chat, "voice": {"file_id": "f1"}}})
    assert voice.kind == "voice" and voice.media == {"id": "f1"} and voice.sender == "55"
    photo = telegram.parse({"update_id": 3, "message": {"chat": chat, "photo": [{"file_id": "small"}, {"file_id": "big"}], "caption": "this look"}})
    assert photo.kind == "image" and photo.media == {"id": "big"} and photo.text == "this look"
    tap = telegram.parse({"update_id": 4, "callback_query": {"id": "cb", "data": "go:xyz", "message": {"chat": chat}}})
    assert tap.kind == "button" and tap.payload == "go:xyz"
    assert telegram.parse({"update_id": 5, "message": {"chat": {"id": -100, "type": "supergroup"}, "text": "hi"}}) is None
    assert telegram.parse({"update_id": 6, "message": {"chat": chat, "sticker": {}}}) is None
    assert telegram.parse({"message": {"chat": chat, "text": "no id"}}) is None


# ── the member's linking routes ──────────────────────────────────────────────

@pytest.fixture
async def member(signup_user):
    client, profile = await signup_user()
    ws = await create_workspace(client, f"Link {uuid4().hex[:6]}")
    await links.create_indexes()
    await get_db()["channel_links"].delete_many({})
    await get_db()["channel_codes"].delete_many({})
    return client, profile["id"], ws, {"X-Workspace-Id": ws}


async def test_only_the_switched_on_chat_apps_are_offered_and_a_code_is_made_for_one(member, keys):
    client, user, ws, h = member
    page = (await client.get("/api/v1/channels", headers=h)).json()
    assert [c["channel"] for c in page["available"]] == ["whatsapp", "telegram"] and page["links"] == [] and page["max"] == links.MAX_LINKS_PER_MEMBER
    made = await client.post("/api/v1/channels/link-code", headers=h, json={"channel": "telegram"})
    assert made.status_code == 201 and made.json()["message"].startswith("LINK ") and len(made.json()["code"]) == 9
    assert (await client.post("/api/v1/channels/link-code", headers=h, json={"channel": "email"})).status_code == 422


async def test_a_chat_app_that_is_not_switched_on_cannot_be_linked(member):
    client, user, ws, h = member
    assert (await client.get("/api/v1/channels", headers=h)).json()["available"] == []
    assert (await client.post("/api/v1/channels/link-code", headers=h, json={"channel": "whatsapp"})).status_code == 409


async def test_linked_chats_are_listed_without_the_number_and_can_be_unlinked_only_by_their_owner(member, keys, signup_user):
    client, user, ws, h = member
    made = (await client.post("/api/v1/channels/link-code", headers=h, json={"channel": "telegram"})).json()
    from app.tools.services import LiveServices

    link = await links.redeem("telegram", "555123456", made["code"], LiveServices())
    shown = (await client.get("/api/v1/channels", headers=h)).json()["links"]
    assert len(shown) == 1 and shown[0]["sender_tail"] == "3456" and "555123456" not in json.dumps(shown) and "sender_hash" not in shown[0]
    stranger, _ = await signup_user()
    assert (await stranger.delete(f"/api/v1/channels/{link['id']}", headers={"X-Workspace-Id": ws})).status_code in (403, 404)
    assert (await client.delete(f"/api/v1/channels/{link['id']}", headers=h)).json() == {"ok": True}
    assert (await client.get("/api/v1/channels", headers=h)).json()["links"] == []
    assert (await client.delete(f"/api/v1/channels/{link['id']}", headers=h)).status_code == 404


async def test_a_member_can_link_only_a_few_chats(member, keys):
    client, user, ws, h = member
    from app.tools.services import LiveServices

    for n in range(links.MAX_LINKS_PER_MEMBER):
        made = (await client.post("/api/v1/channels/link-code", headers=h, json={"channel": "telegram"})).json()
        assert await links.redeem("telegram", f"77700000{n}", made["code"], LiveServices()) is not None
    assert (await client.post("/api/v1/channels/link-code", headers=h, json={"channel": "telegram"})).status_code == 409


async def test_a_signed_link_message_through_the_door_links_the_chat_and_a_reply_goes_back(member, keys, monkeypatch):
    """The whole path with Meta replaced by a stand-in: a signed call, the engine, the link, and the reply posted to the Graph address."""
    import asyncio

    import httpx

    from app.api import channels_webhooks

    client, user, ws, h = member
    made = (await client.post("/api/v1/channels/link-code", headers=h, json={"channel": "whatsapp"})).json()
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append({"url": str(request.url), "body": json.loads(request.content), "auth": request.headers.get("authorization")})
        return httpx.Response(200, json={"messages": [{"id": "wamid.out"}]})

    stand_in = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("app.channels.pool.shared", lambda timeout: stand_in)
    body = json.dumps(wa_payload({"from": "919999900077", "id": "wamid.in", "type": "text", "text": {"body": made["message"]}})).encode()
    res = await client.post(W, content=body, headers={"X-Hub-Signature-256": sign(body)})
    assert res.status_code == 200
    await asyncio.gather(*list(channels_webhooks._tasks))

    replies = [s for s in sent if s["body"].get("type") in ("text", "interactive")]
    assert replies, f"no reply was sent; calls made: {[s['body'] for s in sent]}"
    assert all(s["url"].endswith("/111/messages") and s["auth"] == "Bearer tok" for s in sent)
    assert "linked" in json.dumps(replies[0]["body"]).lower()
    assert (await get_db()["channel_links"].count_documents({"workspace_id": ws})) == 1


def test_a_failed_delivery_report_gives_the_code_and_title_and_nothing_about_the_person():
    payload = {"entry": [{"changes": [{"value": {"statuses": [
        {"id": "wamid.x", "status": "failed", "recipient_id": "919999900001", "errors": [{"code": 131031, "title": "Business Account locked"}]},
        {"id": "wamid.y", "status": "delivered", "recipient_id": "919999900001"},
    ]}}]}]}
    assert whatsapp.failed_deliveries(payload) == [(131031, "Business Account locked")]
    assert whatsapp.failed_deliveries({}) == [] and whatsapp.failed_deliveries({"entry": [None]}) == []
