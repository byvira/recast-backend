"""What the Blog and Newsletter destinations (WordPress, Ghost, Mailchimp) share: the site address check, turning a post into HTML, and
turning a failed reply into a plain message.

A destination is connected with the member's own site or account details, not by signing in through a pop-up. The secret (an application
password or an API key) is stored encrypted like every other connection; the site address is kept beside it.
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import httpx
import mistune

from app.pipelines.publish.base import PublishResult
from app.pipelines.publish.generic.safe_url import UnsafeUrl, assert_safe_url

TIMEOUT = 30.0

#: Raw HTML in the post is shown as text, never run, and unsafe links are neutralised.
_markdown = mistune.create_markdown(escape=True)


class DestinationError(Exception):
    """Something the member can fix, in words they can read."""


def to_html(text: str) -> str:
    """A post written in Markdown as HTML."""
    return _markdown(text or "").strip()


async def clean_site_url(raw: str) -> str:
    """The site's address without a trailing slash. Refused when it is not https or points inside a private network."""
    value = (raw or "").strip()
    if value and "://" not in value:
        value = f"https://{value}"
    parts = urlsplit(value)
    cleaned = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
    try:
        await assert_safe_url(cleaned)
    except UnsafeUrl as exc:
        raise DestinationError(str(exc)) from exc
    return cleaned


def failure(platform: str, piece_id: str, response: Optional[httpx.Response] = None, *, message: str = "", reconnect_label: str = "") -> PublishResult:
    """A failed send as a result. A refused sign-in says to reconnect; other replies carry the destination's own message."""
    status = response.status_code if response is not None else 500
    if status in (401, 403):
        kind, text = "AUTH", f"{reconnect_label or 'The destination'} did not accept the saved details. Reconnect it and try again."
    elif status == 429 or status >= 500:
        kind, text = "TRANSIENT", message or "The destination is busy or not answering. Try again in a few minutes."
    else:
        kind, text = "FATAL", message or "The destination did not accept the post."
    return PublishResult(success=False, platform=platform, piece_id=piece_id, error_type=kind, error_code=status, error_message=text)


def reply_message(response: httpx.Response) -> str:
    """The destination's own explanation from an error reply, kept short, or an empty string."""
    try:
        body = response.json()
    except ValueError:
        return ""
    if isinstance(body, dict):
        for key in ("message", "detail", "title"):
            if isinstance(body.get(key), str):
                return body[key][:300]
        errors = body.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            return str(errors[0].get("message") or errors[0].get("context") or "")[:300]
    return ""
