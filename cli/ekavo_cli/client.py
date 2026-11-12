"""The HTTP side of the Ekavo command line, with nothing but the standard library.

Every call goes through one `Client`. A read is tried again, a short while later, when the server says trying again can help; a call that makes or
changes something is never tried again on its own, because that could do it twice (a repeat with the same `--idempotency-key` is safe, and the person
makes it). The way a call is sent is a small function, so tests can replace it."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional

TIMEOUT = 60
MAX_WAIT = 20          # the longest the command waits on its own before a retry, in seconds
Transport = Callable[[str, str, dict[str, str], Optional[dict[str, Any]]], "Response"]


@dataclass
class Response:
    status: int
    body: dict[str, Any]
    headers: dict[str, str]


class ApiError(Exception):
    """What the server refused or could not do, in its own stable code and plain words."""

    def __init__(self, status: int, code: str, message: str, *, retryable: bool = False, retry_after: Optional[int] = None, next_step: str = "", body: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.status, self.code, self.message, self.retryable, self.retry_after, self.next, self.body = status, code, message, retryable, retry_after, next_step, body or {}


def urllib_transport(method: str, url: str, headers: dict[str, str], body: Optional[dict[str, Any]]) -> Response:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={"Accept": "application/json", **({"Content-Type": "application/json"} if data else {}), **headers})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as reply:  # noqa: S310 - the address is the one the person gave to login
            return Response(reply.status, _json(reply.read()), dict(reply.headers))
    except urllib.error.HTTPError as exc:
        return Response(exc.code, _json(exc.read()), dict(exc.headers or {}))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ApiError(0, "network_error", f"Could not reach the server ({getattr(exc, 'reason', exc)}). Check the address and your connection.", retryable=True)


def _json(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8") or "{}")
        return value if isinstance(value, dict) else {"data": value}
    except ValueError:
        return {}


class Client:
    def __init__(self, base_url: str, token: str = "", *, transport: Transport = urllib_transport, sleep: Callable[[float], None] = time.sleep):
        self.base = base_url.rstrip("/")
        self.token = token
        self.transport = transport
        self.sleep = sleep

    def _url(self, path: str) -> str:
        return f"{self.base}/api/public/v1{path}"

    def request(self, method: str, path: str, body: Optional[dict[str, Any]] = None, *, headers: Optional[dict[str, str]] = None) -> dict[str, Any]:
        """Send one request. Returns the answer, or raises `ApiError`. Only a GET is tried again on its own, and only when the server says it can help."""
        head = {**({"Authorization": f"Bearer {self.token}"} if self.token else {}), **(headers or {})}
        attempts = 3 if method == "GET" else 1
        for attempt in range(attempts):
            try:
                reply = self.transport(method, self._url(path), head, body)
            except ApiError as exc:
                if attempt + 1 < attempts and exc.retryable:
                    self.sleep(min(2 * (attempt + 1), MAX_WAIT))
                    continue
                raise
            if reply.status < 400:
                return reply.body
            error = reply.body.get("error") if isinstance(reply.body.get("error"), dict) else {}
            exc = ApiError(reply.status, str(error.get("code") or "error"), str(error.get("message") or "That did not work."), retryable=bool(error.get("retryable")),
                           retry_after=error.get("retry_after_seconds"), next_step=str(error.get("next") or ""), body=reply.body)
            if attempt + 1 < attempts and exc.retryable and (exc.retry_after or 0) <= MAX_WAIT:
                self.sleep(exc.retry_after or 2)
                continue
            raise exc
        raise ApiError(0, "network_error", "Could not reach the server.", retryable=True)  # pragma: no cover - the loop always returns or raises

    # ── sign-in ──────────────────────────────────────────────────────────────
    def start_login(self, label: str) -> dict[str, Any]:
        return self.request("POST", "/device/start", {"client": "cli", "label": label})

    def poll_login(self, device_code: str) -> dict[str, Any]:
        return self.request("POST", "/device/token", {"device_code": device_code})

    def revoke_self(self) -> dict[str, Any]:
        return self.request("POST", "/device/revoke", {})

    # ── tools ────────────────────────────────────────────────────────────────
    def tools(self, group: str = "", q: str = "") -> list[dict[str, Any]]:
        from urllib.parse import urlencode

        query = urlencode({k: v for k, v in (("group", group), ("q", q)) if v})
        return list(self.request("GET", f"/tools{'?' + query if query else ''}").get("tools") or [])

    def tool(self, name: str) -> dict[str, Any]:
        return self.request("GET", f"/tools/{name}")

    def call(self, name: str, arguments: dict[str, Any], *, dry_run: bool = False, idempotency_key: str = "", confirmed_credits: Optional[int] = None, approval_token: str = "") -> dict[str, Any]:
        body: dict[str, Any] = {"arguments": arguments, "dry_run": dry_run}
        if confirmed_credits is not None:
            body["confirmed_credits"] = confirmed_credits
        if approval_token:
            body["approval_token"] = approval_token
        return self.request("POST", f"/tools/{name}/call", body, headers={"Idempotency-Key": idempotency_key} if idempotency_key else None)
