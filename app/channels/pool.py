"""One kept-open connection pool for the calls Ekavo makes to the chat services.

Each call used to open a new connection, which means a new secure handshake every time (a few hundred milliseconds before a single byte of the message is
sent). A message that shows "typing", then sends a reply, then maybe a second one, paid for that several times over. One pool per event loop keeps the
connections open and reuses them, so only the first call pays. The pool is closed when the app shuts down."""
from __future__ import annotations

import asyncio
import weakref

import httpx

LIMITS = httpx.Limits(max_keepalive_connections=20, max_connections=60, keepalive_expiry=60.0)
_clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient]" = weakref.WeakKeyDictionary()


def shared(timeout: float) -> httpx.AsyncClient:
    """The pool for the running event loop, made on first use. Never close it from a call: `close_all` does that at shutdown."""
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(timeout=timeout, limits=LIMITS)
        _clients[loop] = client
    return client


async def close_all() -> None:
    """Closes the pool of the running loop (the app's, at shutdown)."""
    client = _clients.pop(asyncio.get_running_loop(), None)
    if client is not None and not client.is_closed:
        await client.aclose()
