"""Ghost through its Admin API, signed in with an Admin API key (made under Settings, Integrations, in the member's own Ghost). The key is
"id:secret"; each request carries a short lived token made from it. Built from Ghost's documented Admin API; not yet confirmed against a
live site.
"""
from __future__ import annotations

import time

import httpx
import jwt

from app.pipelines.publish.base import PublishResult
from app.pipelines.publish.destinations.common import DestinationError, TIMEOUT, clean_site_url, failure, reply_message, to_html

LABEL = "Ghost"


def split_key(admin_key: str) -> tuple[str, str]:
    key_id, _, secret = (admin_key or "").strip().partition(":")
    try:
        bytes.fromhex(secret)
    except ValueError:
        secret = ""
    if not key_id or not secret:
        raise DestinationError("The Admin API key should look like two parts joined by a colon. Copy it again from Ghost.")
    return key_id, secret


def make_token(admin_key: str, now: float | None = None) -> str:
    key_id, secret = split_key(admin_key)
    issued = int(now if now is not None else time.time())
    return jwt.encode({"iat": issued, "exp": issued + 5 * 60, "aud": "/admin/"}, bytes.fromhex(secret), algorithm="HS256", headers={"kid": key_id})


def _headers(admin_key: str) -> dict:
    return {"Authorization": f"Ghost {make_token(admin_key)}", "Accept-Version": "v5.0", "Accept": "application/json"}


def _api(site: str, path: str) -> str:
    return f"{site}/ghost/api/admin/{path}"


async def verify(site_url: str, admin_key: str) -> dict:
    site = await clean_site_url(site_url)
    split_key(admin_key)
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.get(_api(site, "site/"), headers=_headers(admin_key))
    except httpx.HTTPError as exc:
        raise DestinationError("That site could not be reached. Check the address.") from exc
    if response.status_code in (401, 403):
        raise DestinationError("Ghost did not accept that Admin API key.")
    if response.status_code == 404:
        raise DestinationError("That address does not look like a Ghost site.")
    if response.status_code != 200:
        raise DestinationError("Ghost could not confirm the key. Try again.")
    return {"site_url": site, "name": (response.json().get("site") or {}).get("title") or site}


async def publish(*, site: str, admin_key: str, piece_id: str, title: str, content: str, excerpt: str, slug: str, status: str,
                  tags: list[str], pictures: list) -> PublishResult:
    post: dict = {"title": title, "html": to_html(content), "status": "published" if status == "publish" else "draft"}
    if excerpt:
        post["custom_excerpt"] = excerpt[:300]
    if slug:
        post["slug"] = slug
    if tags:
        post["tags"] = [{"name": t} for t in tags[:10]]
    if pictures:
        post["feature_image"] = pictures[0].url
        if getattr(pictures[0], "alt_text", None):
            post["feature_image_alt"] = pictures[0].alt_text[:125]
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.post(_api(site, "posts/"), params={"source": "html"}, json={"posts": [post]}, headers=_headers(admin_key))
    except httpx.TimeoutException:
        return PublishResult(success=False, platform="ghost", piece_id=piece_id, error_type="TRANSIENT", error_code=408, error_message="Ghost took too long to answer. Check the site before trying again.")
    except httpx.HTTPError:
        return PublishResult(success=False, platform="ghost", piece_id=piece_id, error_type="FATAL", error_code=502, error_message="That site could not be reached.")
    if response.status_code in (200, 201):
        created = (response.json().get("posts") or [{}])[0]
        return PublishResult(success=True, platform="ghost", piece_id=piece_id, platform_post_id=created.get("id"), platform_post_url=created.get("url"))
    return failure("ghost", piece_id, response, message=reply_message(response), reconnect_label=LABEL)
