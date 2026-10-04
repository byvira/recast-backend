"""URL checks for anything Recast sends to an address someone typed in (webhooks, compose links).

A webhook URL is entered by a person, so it must never be able to reach inside our own network: only
https, no embedded passwords, and the host must resolve to public internet addresses only (no loopback,
private ranges, link-local ranges such as the cloud metadata address, or reserved blocks). The check runs
when the URL is saved and again just before every send.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit


class UnsafeUrl(ValueError):
    """The URL may not be used. The message is plain enough to show to the person who typed it."""


_BLOCKED_NAMES = {"localhost", "localhost.localdomain", "metadata.google.internal"}


def check_https_url(url: str) -> str:
    """Shape checks that need no network. Returns the host."""
    parts = urlsplit((url or "").strip())
    if parts.scheme != "https":
        raise UnsafeUrl("The address must start with https://")
    if not parts.hostname:
        raise UnsafeUrl("The address needs a host name.")
    if parts.username or parts.password:
        raise UnsafeUrl("The address cannot contain a user name or password.")
    host = parts.hostname.lower().rstrip(".")
    if host in _BLOCKED_NAMES or host.endswith(".internal") or host.endswith(".local"):
        raise UnsafeUrl("That address points inside a private network.")
    return host


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def check_resolved_public(host: str, port: int = 443) -> None:
    """Every address the host resolves to must be a public one."""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not _is_public(str(literal)):
            raise UnsafeUrl("That address points inside a private network.")
        return
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise UnsafeUrl("That address could not be found.")
    addresses = {info[4][0] for info in infos}
    if not addresses or not all(_is_public(a) for a in addresses):
        raise UnsafeUrl("That address points inside a private network.")


async def assert_safe_url(url: str) -> None:
    """Full check (shape, then DNS), safe to call from async code."""
    host = check_https_url(url)
    port = urlsplit(url).port or 443
    await asyncio.to_thread(check_resolved_public, host, port)
