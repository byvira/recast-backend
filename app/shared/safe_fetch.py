"""Downloading a file without ever holding more than a set number of bytes.

`client.get(url)` reads the whole answer into memory, whatever its size, so one address that serves a huge or endless body can
use up a small server. `get_limited` streams the answer and stops at the limit.
"""
from __future__ import annotations

import httpx


class ResponseTooLarge(Exception):
    """The answer is larger than the limit that was asked for."""


async def get_limited(
    client: httpx.AsyncClient,
    url: str,
    max_bytes: int,
    *,
    truncate: bool = False,
    **kwargs,
) -> tuple[httpx.Response, bytes]:
    """GET `url`, reading at most `max_bytes`. Returns the response (status and headers) and the bytes read.

    With `truncate` an oversize answer is cut at the limit (a web page's first part is enough for a link card); without it
    `ResponseTooLarge` is raised, as soon as the declared length or the bytes received pass the limit. HTTP errors are not raised
    here, so the caller can look at the status first.
    """
    async with client.stream("GET", url, **kwargs) as response:
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes and not truncate:
            raise ResponseTooLarge(f"The file at {url} is larger than {max_bytes} bytes.")
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body += chunk
            if len(body) > max_bytes:
                if truncate:
                    return response, bytes(body[:max_bytes])
                raise ResponseTooLarge(f"The file at {url} is larger than {max_bytes} bytes.")
        return response, bytes(body)
