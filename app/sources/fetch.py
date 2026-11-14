"""Fetching an address a person typed, safely.

Every hop (the address and each redirect) must be https, free of embedded passwords, and resolve to public addresses only, so a source can never be
pointed at our own network or a cloud metadata address. The answer is read in a stream and stops at a size limit, under a time limit, and a page
that redirects more than twice is refused. Plain words for every refusal, because the member sees them.

The safety checks are the ones the webhooks already use (`app.pipelines.publish.generic.safe_url`)."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional
from urllib.parse import urljoin

import httpx

from app.pipelines.publish.generic.safe_url import UnsafeUrl, assert_safe_url
from app.shared.safe_fetch import ResponseTooLarge, get_limited

MAX_BYTES = 2 * 1024 * 1024
TIMEOUT_S = 15.0
MAX_REDIRECTS = 2
USER_AGENT = "EkavoSourceReader/1.0 (+reads public feeds and pages you added; one request per check)"

Guard = Callable[[str], Awaitable[None]]


class FetchError(Exception):
    """The address could not be read. `code` is stable, the message is plain words for the member, `retryable` says whether a later check may work."""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable


@dataclass(frozen=True)
class Fetched:
    url: str                 # where it ended up, after redirects
    content_type: str        # without parameters, lower case
    body: bytes


async def fetch(url: str, *, max_bytes: int = MAX_BYTES, client: Optional[httpx.AsyncClient] = None, guard: Guard = assert_safe_url) -> Fetched:
    """One address, read once. `client` and `guard` are replaceable so tests need no network."""
    own = client is None
    http = client or httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=False, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    deadline = time.monotonic() + TIMEOUT_S
    current = (url or "").strip()
    try:
        for _hop in range(MAX_REDIRECTS + 1):
            try:
                await guard(current)
            except UnsafeUrl as exc:
                raise FetchError("address_refused", str(exc)) from exc
            try:
                response, body = await get_limited(http, current, max_bytes, follow_redirects=False, timeout=max(1.0, deadline - time.monotonic()))
            except ResponseTooLarge as exc:
                raise FetchError("too_large", f"That is larger than {max_bytes // (1024 * 1024)} MB, which is more than a source can read.") from exc
            except httpx.TimeoutException as exc:
                raise FetchError("too_slow", "That site took too long to answer. It will be tried again at the next check.", True) from exc
            except httpx.HTTPError as exc:
                raise FetchError("cant_open", "That address could not be reached. Check it, or it will be tried again at the next check.", True) from exc
            status = response.status_code
            if status in (301, 302, 303, 307, 308):
                where = response.headers.get("location")
                if not where:
                    raise FetchError("cant_open", "That address redirects without saying where.")
                current = urljoin(current, where)
                continue
            if status in (401, 403, 407, 451):
                raise FetchError("needs_login", "That address needs a login or does not allow reading, so it cannot be used as a source.")
            if status in (404, 410):
                raise FetchError("not_found", "Nothing is at that address any more.")
            if status == 429 or status >= 500:
                raise FetchError("site_busy", "That site is busy or down right now. It will be tried again at the next check.", True)
            if status >= 400:
                raise FetchError("cant_open", "That address could not be opened.")
            return Fetched(current, response.headers.get("content-type", "").split(";")[0].strip().lower(), body)
        raise FetchError("too_many_redirects", "That address sends you around too many times to read.")
    finally:
        if own:
            await http.aclose()
