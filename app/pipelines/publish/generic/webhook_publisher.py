"""Generic publisher for token_webhook platforms (Telegram, Discord, Slack, Microsoft Teams, Google Chat, ...).
POSTs to a configured webhook address. No per-platform Python code is needed; the Ops settings say what to send.

Settings (platform config, see platform_config_store): `webhook_url` (secret), optional `signing_secret` (secret),
and fields `payload_template`, `payload_format` ("json" default, or "text"), `extra_headers`, `success_range`
("200-299" default), `external_id_path` (a dotted path into a JSON reply) and `timeout_seconds` (10, at most 30).

Template placeholders: {title} (first line), {body} (the post), {link} (empty before posting), {hashtags},
{media_urls} (a JSON list, written without quotes). Text values are escaped so they cannot break the JSON.

Safety: https only, public addresses only (checked on save and again before each send), no redirects, a small
reply size, an Idempotency-Key on every send so a retry cannot post twice, and the address, headers and signing
secret are never written to a log.
"""

import hashlib
import hmac
import json
import logging
import re
import time
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx
from httpx import AsyncClient

from app.pipelines.publish.base import PublishResult
from app.pipelines.publish.generic.safe_url import UnsafeUrl, assert_safe_url
from app.pipelines.publish.platform_config_store import get_platform_config_secrets

logger = logging.getLogger(__name__)

MAX_REPLY_BYTES = 64 * 1024
DEFAULT_TIMEOUT = 10
MAX_TIMEOUT = 30
_HASHTAG = re.compile(r"#\w+", re.UNICODE)


def parse_success_range(value: Any) -> tuple[int, int]:
    """"200-299" -> (200, 299). Anything unreadable falls back to the default."""
    try:
        low, _, high = str(value or "").partition("-")
        low_i, high_i = int(low), int(high or low)
        if 100 <= low_i <= high_i <= 599:
            return low_i, high_i
    except ValueError:
        pass
    return 200, 299


def _clamp_timeout(value: Any) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    return max(1.0, min(seconds, MAX_TIMEOUT))


def _escape(value: str) -> str:
    """The inside of a JSON string for this text, without the surrounding quotes."""
    return json.dumps(value, ensure_ascii=False)[1:-1]


def render_payload(content: str, fields: dict, media_urls: list[str]) -> tuple[bytes, str]:
    """(body bytes, content type). Raises ValueError with a plain message when the template is broken."""
    template = str(fields.get("payload_template") or "")
    title = (content.strip().splitlines() or [""])[0][:100]
    hashtags = " ".join(_HASHTAG.findall(content))
    if not template:
        body = {"content": content, "title": title, "media_urls": media_urls}
        return json.dumps(body, ensure_ascii=False).encode("utf-8"), "application/json"

    if str(fields.get("payload_format") or "json").lower() == "text":
        rendered = (
            template.replace("{title}", title).replace("{body}", content).replace("{link}", "")
            .replace("{hashtags}", hashtags).replace("{media_urls}", " ".join(media_urls))
        )
        return rendered.encode("utf-8"), "text/plain; charset=utf-8"

    rendered = (
        template.replace("{media_urls}", json.dumps(media_urls))
        .replace("{title}", _escape(title)).replace("{body}", _escape(content))
        .replace("{link}", "").replace("{hashtags}", _escape(hashtags))
    )
    try:
        parsed = json.loads(rendered)
    except ValueError:
        raise ValueError("The payload template is not valid JSON once the post is filled in.")
    return json.dumps(parsed, ensure_ascii=False).encode("utf-8"), "application/json"


def idempotency_key(piece_id: str, platform: str) -> str:
    return hashlib.sha256(f"{piece_id}:{platform}".encode("utf-8")).hexdigest()


def sign_body(secret: str, timestamp: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), timestamp.encode("utf-8") + b"." + body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _read_path(data: Any, path: str) -> Optional[str]:
    for part in [p for p in path.split(".") if p]:
        if isinstance(data, dict) and part in data:
            data = data[part]
        elif isinstance(data, list) and part.isdigit() and int(part) < len(data):
            data = data[int(part)]
        else:
            return None
    return None if data is None or isinstance(data, (dict, list)) else str(data)


class WebhookPublisher:
    """Not a PlatformPublisher subclass: that contract is sign-in shaped (build_auth_url, exchange_token,
    refresh_token), which does not apply to a webhook with no user consent step."""

    async def publish(
        self,
        workspace_id: str,
        platform: str,
        content: str,
        fields: dict,
        media_urls: list[str] | None = None,
        piece_id: str = "",
        secrets: Optional[dict] = None,
    ) -> PublishResult:
        def failed(kind: str, message: str, code: Optional[int] = None, retry_after: Optional[int] = None) -> PublishResult:
            return PublishResult(
                success=False, platform=platform, piece_id=piece_id,
                error_type=kind, error_code=code, error_message=message, retry_after=retry_after,
            )

        if secrets is None:
            secrets = await get_platform_config_secrets(workspace_id, platform)
        webhook_url = (secrets or {}).get("webhook_url")
        if not webhook_url:
            return failed("FIXABLE", "No webhook address is set for this platform yet.")

        try:
            await assert_safe_url(webhook_url)
            body, content_type = render_payload(content, fields, media_urls or [])
        except UnsafeUrl as exc:
            return failed("FIXABLE", str(exc))
        except ValueError as exc:
            return failed("FIXABLE", str(exc))

        headers = {
            "Content-Type": content_type,
            "Idempotency-Key": idempotency_key(piece_id, platform),
            "User-Agent": "Recast-Webhook/1.0",
        }
        extra = fields.get("extra_headers")
        if isinstance(extra, dict):
            headers.update({str(k): str(v) for k, v in extra.items() if str(k).lower() not in {"host", "content-length"}})
        signing_secret = (secrets or {}).get("signing_secret")
        if signing_secret:
            timestamp = str(int(time.time()))
            headers["X-Recast-Timestamp"] = timestamp
            headers["X-Recast-Signature"] = sign_body(signing_secret, timestamp, body)

        low, high = parse_success_range(fields.get("success_range"))
        host = urlsplit(webhook_url).hostname or "webhook"
        timeout = _clamp_timeout(fields.get("timeout_seconds"))

        try:
            async with AsyncClient(timeout=timeout, follow_redirects=False) as client:
                async with client.stream("POST", webhook_url, content=body, headers=headers) as response:
                    status = response.status_code
                    reply = b""
                    async for chunk in response.aiter_bytes():
                        reply += chunk
                        if len(reply) >= MAX_REPLY_BYTES:
                            reply = reply[:MAX_REPLY_BYTES]
                            break
                    retry_header = response.headers.get("retry-after", "")
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            logger.warning("Webhook send failed, host=%s platform=%s kind=%s", host, platform, type(exc).__name__)
            return failed("TRANSIENT", "The webhook did not answer in time. It will be tried again.")

        if low <= status <= high:
            post_id = None
            path = str(fields.get("external_id_path") or "")
            if path and reply:
                try:
                    post_id = _read_path(json.loads(reply.decode("utf-8", "ignore")), path)
                except ValueError:
                    post_id = None
            logger.info("Webhook delivered, host=%s platform=%s status=%s", host, platform, status)
            return PublishResult(success=True, platform=platform, piece_id=piece_id, platform_post_id=post_id)

        logger.warning("Webhook refused, host=%s platform=%s status=%s", host, platform, status)
        if status == 429:
            wait = int(retry_header) if retry_header.isdigit() else None
            return failed("TRANSIENT", "The webhook asked us to slow down. It will be tried again.", status, wait)
        if status >= 500:
            return failed("TRANSIENT", f"The webhook had a problem ({status}). It will be tried again.", status)
        if 300 <= status < 400:
            return failed("FIXABLE", "The webhook address redirects somewhere else. Use the final address.", status)
        return failed("FIXABLE", f"The webhook refused the post ({status}).", status)
