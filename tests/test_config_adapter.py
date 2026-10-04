"""Webhook and manual-handoff platforms publish through one thin adapter.

Nothing here talks to a real service. Webhooks are answered by an in-memory transport; the address check is
exercised with IP literals and a stubbed resolver so no real DNS lookup happens.
"""

import json
import socket
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.db.mongo import content_pieces, platform_configs, platform_ops
from app.models.media import MediaAsset
from app.pipelines.publish.base import PublishRequest
from app.pipelines.publish.generic import manual_handoff_publisher as manual
from app.pipelines.publish.generic import webhook_publisher as hook
from app.pipelines.publish.generic.adapter import ConfigPublisherAdapter, NotApplicableForPattern
from app.pipelines.publish.generic.safe_url import UnsafeUrl, assert_safe_url, check_https_url
from app.pipelines.platform_ops.store import get_ops, save_ops
from app.pipelines.publish.platform_config_store import PLATFORM_WIDE, get_effective_config, save_platform_config
from app.pipelines.publish.registry import adapter_for, get_publisher
from app.pipelines.publish.spine import schedule_blocker
from app.platforms.base import get_platform, import_all
from app.workers import scheduled_posts as worker
from tests.conftest import create_workspace
from tests.test_publish_spine import H, _approve, _later, _seed

import_all()


# ── the address check ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "http://example.com/hook",             # not https
    "https://user:pw@example.com/hook",    # embedded password
    "https://localhost/hook",
    "https://metadata.google.internal/x",
    "https://printer.local/hook",
    "ftp://example.com/x",
    "https:///nohost",
])
def test_unsafe_addresses_are_refused_by_shape(url):
    with pytest.raises(UnsafeUrl):
        check_https_url(url)


@pytest.mark.parametrize("literal", ["127.0.0.1", "10.0.0.5", "192.168.1.9", "169.254.169.254", "172.16.0.1", "[::1]", "0.0.0.0"])
async def test_private_ip_addresses_are_refused(literal):
    with pytest.raises(UnsafeUrl):
        await assert_safe_url(f"https://{literal}/hook")


async def test_a_name_that_resolves_to_a_private_address_is_refused(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("10.1.2.3", 443))])
    with pytest.raises(UnsafeUrl):
        await assert_safe_url("https://innocent.example.com/hook")


async def test_a_name_with_one_private_answer_among_public_ones_is_refused(monkeypatch):
    answers = [(2, 1, 6, "", ("8.8.8.8", 443)), (2, 1, 6, "", ("192.168.0.4", 443))]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: answers)
    with pytest.raises(UnsafeUrl):
        await assert_safe_url("https://mixed.example.com/hook")


async def test_a_public_address_passes(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    await assert_safe_url("https://hooks.example.com/abc")


# ── building and signing the payload ──────────────────────────────────────────

def test_the_default_payload_carries_the_post_and_media():
    body, kind = hook.render_payload("Hello world\nMore", {}, ["https://x/a.png"])
    assert kind == "application/json"
    data = json.loads(body)
    assert data == {"content": "Hello world\nMore", "title": "Hello world", "media_urls": ["https://x/a.png"]}


def test_a_json_template_cannot_be_broken_by_quotes_or_newlines():
    template = '{"text": "{title}: {body}", "tags": "{hashtags}", "files": {media_urls}}'
    content = 'He said "hi"\nsecond line #launch #team'
    body, kind = hook.render_payload(content, {"payload_template": template}, ["https://x/a.png"])
    data = json.loads(body)
    assert data["text"] == 'He said "hi": He said "hi"\nsecond line #launch #team'
    assert data["tags"] == "#launch #team" and data["files"] == ["https://x/a.png"]


def test_a_template_that_is_not_json_is_a_plain_error():
    with pytest.raises(ValueError, match="not valid JSON"):
        hook.render_payload("Hi", {"payload_template": "{ this is {body} not json"}, [])


def test_a_text_template_is_sent_as_plain_text():
    body, kind = hook.render_payload("Hi there", {"payload_template": "NEW: {body}", "payload_format": "text"}, [])
    assert body == b"NEW: Hi there" and kind.startswith("text/plain")


def test_the_signature_and_idempotency_key_are_stable():
    assert hook.sign_body("s3cret", "1700000000", b"{}") == hook.sign_body("s3cret", "1700000000", b"{}")
    assert hook.sign_body("s3cret", "1700000000", b"{}") != hook.sign_body("other", "1700000000", b"{}")
    assert hook.idempotency_key("p1", "slack") == hook.idempotency_key("p1", "slack")
    assert hook.idempotency_key("p1", "slack") != hook.idempotency_key("p2", "slack")


def test_success_range_falls_back_when_unreadable():
    assert hook.parse_success_range("200-204") == (200, 204)
    assert hook.parse_success_range("202") == (202, 202)
    assert hook.parse_success_range("banana") == (200, 299)
    assert hook.parse_success_range("") == (200, 299)


# ── sending a webhook ─────────────────────────────────────────────────────────

class Wire:
    """Stands in for the network: records every request, answers with a chosen reply."""

    def __init__(self, status=200, body=None, headers=None, raise_exc=None):
        self.requests: list[httpx.Request] = []
        self.status, self.body, self.headers, self.raise_exc = status, body or {}, headers or {}, raise_exc

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raise_exc:
            raise self.raise_exc
        return httpx.Response(self.status, json=self.body, headers=self.headers)


@pytest.fixture
def wire(monkeypatch):
    holder = {}
    real = hook.AsyncClient

    def make(wire: Wire) -> Wire:
        holder["wire"] = wire
        return wire

    def factory(**kwargs):
        kwargs.pop("transport", None)
        return real(transport=httpx.MockTransport(holder["wire"].handler), **kwargs)

    monkeypatch.setattr(hook, "AsyncClient", factory)
    monkeypatch.setattr(hook, "assert_safe_url", AsyncMock(return_value=None))
    return make


SECRETS = {"webhook_url": "https://hooks.example.com/T123/secret-token", "signing_secret": "shh"}


async def _send(fields=None, secrets=SECRETS, content="Launch day #news", media=None):
    return await hook.WebhookPublisher().publish(
        workspace_id="ws", platform="slack", content=content, fields=fields or {}, media_urls=media or [],
        piece_id="piece-1", secrets=secrets,
    )


async def test_a_delivered_webhook_is_a_success_and_reads_the_id(wire):
    w = wire(Wire(200, {"ok": True, "data": {"id": "msg-77"}}))
    result = await _send({"external_id_path": "data.id"})
    assert result.success and result.platform_post_id == "msg-77"
    sent = w.requests[0]
    assert sent.headers["idempotency-key"] == hook.idempotency_key("piece-1", "slack")
    ts = sent.headers["x-recast-timestamp"]
    assert sent.headers["x-recast-signature"] == hook.sign_body("shh", ts, sent.content)


async def test_unsigned_when_no_signing_secret(wire):
    w = wire(Wire(200))
    await _send(secrets={"webhook_url": SECRETS["webhook_url"]})
    assert "x-recast-signature" not in w.requests[0].headers


async def test_a_slow_down_reply_is_retryable_and_keeps_the_wait(wire):
    wire(Wire(429, headers={"Retry-After": "30"}))
    result = await _send()
    assert not result.success and result.error_type == "TRANSIENT" and result.retry_after == 30 and result.error_code == 429


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_a_server_error_is_retryable(wire, status):
    wire(Wire(status))
    result = await _send()
    assert result.error_type == "TRANSIENT" and result.error_code == status


async def test_a_refusal_is_not_retried(wire):
    wire(Wire(404))
    result = await _send()
    assert result.error_type == "FIXABLE" and result.error_code == 404


async def test_a_redirect_is_not_followed(wire):
    w = wire(Wire(302, headers={"Location": "https://10.0.0.1/elsewhere"}))
    result = await _send()
    assert not result.success and result.error_type == "FIXABLE"
    assert len(w.requests) == 1


async def test_a_timeout_is_retryable(wire):
    wire(Wire(raise_exc=httpx.ReadTimeout("slow")))
    result = await _send()
    assert result.error_type == "TRANSIENT"


async def test_a_custom_success_range_is_honoured(wire):
    wire(Wire(202))
    assert (await _send({"success_range": "202"})).success
    wire(Wire(200))
    assert not (await _send({"success_range": "202"})).success


async def test_no_address_means_a_plain_fixable_error(wire):
    w = wire(Wire(200))
    result = await _send(secrets={})
    assert not result.success and result.error_type == "FIXABLE" and not w.requests


async def test_a_private_address_is_never_contacted(monkeypatch):
    calls = []
    monkeypatch.setattr(hook, "AsyncClient", lambda **k: calls.append(k))
    result = await _send(secrets={"webhook_url": "https://169.254.169.254/latest/meta-data"})
    assert not result.success and result.error_type == "FIXABLE" and not calls


async def test_the_secret_address_and_signing_secret_never_reach_a_log_or_the_result(wire):
    import logging

    records: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = Collect(level=logging.DEBUG)
    hook.logger.addHandler(handler)
    previous = hook.logger.level
    hook.logger.setLevel(logging.DEBUG)
    try:
        wire(Wire(500))
        result = await _send()
    finally:
        hook.logger.removeHandler(handler)
        hook.logger.setLevel(previous)
    assert records, "the failed send should have been logged"
    everything = " ".join(records) + (result.error_message or "")
    assert "secret-token" not in everything and "shh" not in everything and "T123" not in everything


async def test_a_long_reply_is_cut_off(wire):
    wire(Wire(200, {"data": "x" * 200_000}))
    assert (await _send({"external_id_path": "data"})).success  # still succeeds; the body is not trusted or stored


# ── manual handoff ────────────────────────────────────────────────────────────

async def test_a_manual_handoff_never_reports_success():
    result = await manual.ManualHandoffPublisher().publish(
        "ws", "twitter", "Hello & welcome #x",
        {"compose_url_template": "https://x.example.com/compose?text={text}", "instructions": "Press Post."},
        piece_id="p1",
    )
    assert result.success is False
    assert result.manual_action_url == "https://x.example.com/compose?text=Hello%20%26%20welcome%20%23x"
    assert result.manual_instructions == "Press Post."


@pytest.mark.parametrize("template,fragment", [
    ("", "No compose link"),
    ("http://x.example.com/c?text={text}", "https"),
    ("https://x.example.com/c", "{text}"),
    ("https://localhost/c?text={text}", "private"),
])
async def test_a_bad_compose_link_is_a_plain_error(template, fragment):
    result = await manual.ManualHandoffPublisher().publish("ws", "twitter", "Hi", {"compose_url_template": template})
    assert not result.success and result.manual_action_url is None
    assert fragment.lower() in (result.error_message or "").lower()


# ── the adapter itself ────────────────────────────────────────────────────────

def _adapter(key="twitter", fields=None, secrets=None):
    return ConfigPublisherAdapter(get_platform(key), {"enabled": True, "fields": fields or {}, "secrets": secrets or {}})


def _request(platform="twitter", content="Hello", media=None):
    return PublishRequest(
        piece_id="p1", user_id="u1", brand_id="b1", platform=platform, content=content, workspace_id="ws",
        media=media or [],
    )


async def test_the_adapter_has_no_sign_in():
    adapter = _adapter()
    assert adapter.uses_oauth_token is False
    with pytest.raises(NotApplicableForPattern):
        adapter.build_auth_url("state")
    with pytest.raises(NotApplicableForPattern):
        await adapter.exchange_token("code")
    with pytest.raises(NotApplicableForPattern):
        await adapter.refresh_token("token")


async def test_a_manual_adapter_never_returns_success_with_any_access_token():
    adapter = _adapter(fields={"compose_url_template": "https://x.example.com/c?text={text}"})
    for token in ("", "anything"):
        result = await adapter.publish(_request(), token)
        assert result.success is False and result.manual_action_url


def test_the_adapter_checks_the_registry_limit():
    adapter = _adapter("slack")
    ok, issues = adapter.validate_content("short")
    assert ok and not issues
    assert not adapter.validate_content("   ")[0]
    twitter = _adapter("twitter")
    twitter.definition = twitter.definition.model_copy(update={"max_chars": 10})
    ok, issues = twitter.validate_content("this is much longer than ten")
    assert not ok and "too long" in issues[0].lower()


async def test_media_the_platform_does_not_take_is_reported(wire):
    w = wire(Wire(200))
    adapter = _adapter("slack", secrets={"webhook_url": "https://hooks.example.com/x"})
    adapter.definition = adapter.definition.model_copy(update={"native_formats": {"text": "native", "image": "native"}})
    media = [
        MediaAsset(id="m1", url="https://x/a.png", kind="image", mime_type="image/png", workspace_id="ws",
                   source="uploaded", created_by="u1", created_at=datetime.now(timezone.utc)),
        MediaAsset(id="m2", url="https://x/b.mp4", kind="video", mime_type="video/mp4", workspace_id="ws",
                   source="uploaded", created_by="u1", created_at=datetime.now(timezone.utc)),
    ]
    result = await adapter.publish(_request("slack", media=media), "")
    assert result.success and "video" in result.media_dropped_reason
    assert json.loads(w.requests[0].content)["media_urls"] == ["https://x/a.png"]


# ── resolving the adapter (real database) ─────────────────────────────────────

@pytest.fixture
async def clean_configs():
    async def wipe():
        await platform_configs.delete_many({"platform": {"$in": ["twitter", "slack", "linkedin"]}})
        await platform_ops.delete_many({"platform_key": {"$in": ["twitter", "slack", "linkedin"]}})
    await wipe()
    yield
    await wipe()


async def test_without_saved_settings_a_config_platform_is_still_not_supported(signup_user, clean_configs):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Adapter WS 1")
    assert await adapter_for("slack", ws_id) is None
    assert await adapter_for("twitter", ws_id) is None
    with pytest.raises(ValueError):
        get_publisher("slack")


async def test_saved_enabled_settings_make_an_adapter(signup_user, clean_configs):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Adapter WS 2")
    await save_platform_config(ws_id, "slack", "Team Slack", True, {"success_range": "200-204"}, {"webhook_url": "https://hooks.example.com/x"})
    adapter = await adapter_for("slack", ws_id)
    assert isinstance(adapter, ConfigPublisherAdapter)
    assert adapter.config["fields"]["success_range"] == "200-204"
    assert adapter.config["secrets"]["webhook_url"] == "https://hooks.example.com/x"


async def test_switched_off_settings_make_nothing(signup_user, clean_configs):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Adapter WS 3")
    await save_platform_config(ws_id, "slack", None, False, {}, {"webhook_url": "https://hooks.example.com/x"})
    assert await adapter_for("slack", ws_id) is None


async def test_a_real_platform_never_gets_an_adapter(signup_user, clean_configs):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Adapter WS 4")
    await save_platform_config(ws_id, "linkedin", None, True, {}, {})
    assert await adapter_for("linkedin", ws_id) is None  # an api_publish platform
    assert get_publisher("linkedin").__class__.__name__ == "LinkedInPublisher"


async def test_workspace_settings_lay_over_the_platform_wide_ones(signup_user, clean_configs):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Adapter WS 5")
    await save_platform_config(PLATFORM_WIDE, "slack", "Slack", True, {"success_range": "200-299", "timeout_seconds": 5}, {"signing_secret": "wide"})
    await save_platform_config(ws_id, "slack", None, True, {"timeout_seconds": 8}, {"webhook_url": "https://hooks.example.com/mine"})
    config = await get_effective_config(ws_id, "slack")
    assert config["fields"] == {"success_range": "200-299", "timeout_seconds": 8}
    assert config["secrets"] == {"signing_secret": "wide", "webhook_url": "https://hooks.example.com/mine"}
    other = await create_workspace(client, "Adapter WS 6")
    only_wide = await get_effective_config(other, "slack")
    assert only_wide["secrets"] == {"signing_secret": "wide"}


# ── through the real routes and the worker ────────────────────────────────────

async def _open_to_everyone(key: str) -> None:
    """Ops has taken the platform live for every workspace (a platform with settings is otherwise not offered)."""
    definition = get_platform(key)
    current = await get_ops(definition)
    await save_ops(
        definition, {"ops_stage": "live", "rollout": {"scope": "everyone", "workspace_ids": []}},
        expected_version=current["version"], actor_id="test",
    )


async def test_publish_now_on_a_manual_platform_hands_back_a_link_and_never_publishes(signup_user, clean_configs):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Manual WS 1")
    await save_platform_config(
        ws_id, "twitter", None, True,
        {"compose_url_template": "https://x.example.com/compose?text={text}", "instructions": "Tap Post."}, {},
    )
    piece_id = await _seed(ws_id, profile["id"], platform="Twitter/X")
    await _approve(client, ws_id, piece_id)

    res = await client.post("/api/v1/publish/now", json={"piece_id": piece_id}, headers=H(ws_id))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["success"] is False and body["manual"] is True
    assert body["manual_action_url"].startswith("https://x.example.com/compose?text=")
    assert body["instructions"] == "Tap Post."
    doc = await content_pieces.find_one({"piece_id": piece_id})
    assert doc["publish_status"] != "published" and not doc.get("platform_post_url")


async def test_without_settings_publish_now_on_x_is_unchanged(signup_user, clean_configs):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Manual WS 2")
    piece_id = await _seed(ws_id, profile["id"], platform="Twitter/X")
    await _approve(client, ws_id, piece_id)
    res = await client.post("/api/v1/publish/now", json={"piece_id": piece_id}, headers=H(ws_id))
    assert res.status_code == 400 and "not connected" in res.json()["detail"]


async def test_publish_now_on_a_webhook_platform_delivers_and_marks_published(signup_user, clean_configs, wire):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Hook WS 1")
    await _open_to_everyone("slack")
    await save_platform_config(ws_id, "slack", None, True, {"external_id_path": "id"}, {"webhook_url": "https://hooks.example.com/x"})
    w = wire(Wire(200, {"id": "m-1"}))
    piece_id = await _seed(ws_id, profile["id"], platform="Slack")
    await _approve(client, ws_id, piece_id)

    res = await client.post("/api/v1/publish/now", json={"piece_id": piece_id}, headers=H(ws_id))
    assert res.status_code == 200, res.text
    assert res.json()["success"] is True and res.json()["platform_post_id"] == "m-1"
    assert len(w.requests) == 1
    assert (await content_pieces.find_one({"piece_id": piece_id}))["publish_status"] == "published"


async def test_a_refused_webhook_does_not_leave_the_piece_publishing(signup_user, clean_configs, wire):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Hook WS 2")
    await _open_to_everyone("slack")
    await save_platform_config(ws_id, "slack", None, True, {}, {"webhook_url": "https://hooks.example.com/x"})
    wire(Wire(404))
    piece_id = await _seed(ws_id, profile["id"], platform="Slack")
    await _approve(client, ws_id, piece_id)
    res = await client.post("/api/v1/publish/now", json={"piece_id": piece_id}, headers=H(ws_id))
    assert res.status_code == 200 and res.json()["success"] is False and res.json()["status"] == "failed"
    assert (await content_pieces.find_one({"piece_id": piece_id}))["publish_status"] == "failed"


async def test_the_scheduling_gate_knows_a_manual_platform_is_connected_and_a_webhook_needs_its_address(signup_user, clean_configs):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Gate WS 9")
    manual_piece = {"platform": "Twitter/X", "content": "Hello", "media": []}
    before = await schedule_blocker(manual_piece, ws_id)
    assert before and before[0] == 400  # no saved settings yet: still refused, as before this module existed
    await save_platform_config(ws_id, "twitter", None, True, {"compose_url_template": "https://x.example.com/c?text={text}"}, {})
    assert await schedule_blocker(manual_piece, ws_id) is None

    await _open_to_everyone("slack")
    hook_piece = {"platform": "Slack", "content": "Hello", "media": []}
    await save_platform_config(ws_id, "slack", None, True, {}, {})
    blocked = await schedule_blocker(hook_piece, ws_id)
    assert blocked and blocked[0] == 400 and "webhook address" in blocked[1]
    await save_platform_config(ws_id, "slack", None, True, {}, {"webhook_url": "https://hooks.example.com/x"})
    assert await schedule_blocker(hook_piece, ws_id) is None


async def test_the_worker_never_publishes_a_manual_post_it_hands_it_back(signup_user, clean_configs):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Manual WS 3")
    await save_platform_config(
        ws_id, "twitter", None, True, {"compose_url_template": "https://x.example.com/compose?text={text}"}, {},
    )
    piece_id = await _seed(ws_id, profile["id"], platform="Twitter/X")
    await _approve(client, ws_id, piece_id)
    res = await client.patch(
        f"/api/v1/content/pieces/{piece_id}/schedule", json={"scheduled_at": _later().isoformat()}, headers=H(ws_id),
    )
    assert res.status_code == 200, res.text
    await content_pieces.update_one(
        {"piece_id": piece_id}, {"$set": {"publish_scheduled_at": datetime.now(timezone.utc) - timedelta(minutes=1)}},
    )
    await worker.process_scheduled_posts.__wrapped__()

    doc = await content_pieces.find_one({"piece_id": piece_id})
    assert doc["publish_status"] == "pending" and doc["manual_post_due"] is True
    assert doc["manual_action_url"].startswith("https://x.example.com/compose?text=")
    assert "yourself" in doc["schedule_note"]
    assert not doc.get("platform_post_url") and not doc.get("published_at")
