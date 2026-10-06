"""WordPress through its own REST API, signed in with an application password (a member makes one in their profile; it can be revoked
alone). Works for self-hosted sites and WordPress.com sites on a plan that allows it. Built from WordPress's documented REST API; not
yet confirmed against a live site.
"""
from __future__ import annotations

import base64
from typing import Optional

import httpx

from app.pipelines.publish.base import PublishResult
from app.pipelines.publish.destinations.common import DestinationError, TIMEOUT, clean_site_url, failure, reply_message, to_html

LABEL = "WordPress"


def _headers(username: str, password: str) -> dict:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Accept": "application/json"}


def _api(site: str, path: str) -> str:
    return f"{site}/wp-json/wp/v2/{path}"


async def verify(site_url: str, username: str, password: str) -> dict:
    """Signs in once and says who the site knows the member as. Raises DestinationError with a plain reason."""
    site = await clean_site_url(site_url)
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.get(_api(site, "users/me"), params={"context": "edit"}, headers=_headers(username, password))
    except httpx.HTTPError as exc:
        raise DestinationError("That site could not be reached. Check the address.") from exc
    if response.status_code in (401, 403):
        raise DestinationError("WordPress did not accept that user name and application password.")
    if response.status_code == 404:
        raise DestinationError("That address does not look like a WordPress site with the REST API turned on.")
    if response.status_code != 200:
        raise DestinationError("WordPress could not confirm the sign in. Try again.")
    me = response.json()
    return {"site_url": site, "user_id": str(me.get("id", "")), "name": me.get("name") or username}


async def categories(site: str, username: str, password: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.get(_api(site, "categories"), params={"per_page": 100, "_fields": "id,name"}, headers=_headers(username, password))
    return [{"id": str(c["id"]), "name": c["name"]} for c in response.json()] if response.status_code == 200 else []


async def authors(site: str, username: str, password: str) -> list[dict]:
    """People the member may post as. Empty when the site does not let this account list users."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.get(_api(site, "users"), params={"per_page": 50, "context": "edit", "_fields": "id,name"}, headers=_headers(username, password))
    return [{"id": str(u["id"]), "name": u["name"]} for u in response.json()] if response.status_code == 200 else []


async def _upload_cover(client: httpx.AsyncClient, site: str, headers: dict, picture) -> Optional[int]:
    """The first picture as the post's featured image. Best effort: the post goes out without one if this fails."""
    try:
        source = await client.get(picture.url)
        source.raise_for_status()
        mime = picture.mime_type or "image/jpeg"
        extension = {"image/png": "png", "image/webp": "webp", "image/gif": "gif"}.get(mime, "jpg")
        uploaded = await client.post(
            _api(site, "media"), content=source.content,
            headers={**headers, "Content-Type": mime, "Content-Disposition": f'attachment; filename="cover.{extension}"'},
        )
        if uploaded.status_code in (200, 201):
            if getattr(picture, "alt_text", None):
                await client.post(_api(site, f"media/{uploaded.json()['id']}"), json={"alt_text": picture.alt_text[:300]}, headers=headers)
            return int(uploaded.json()["id"])
    except Exception:  # noqa: BLE001
        pass
    return None


async def publish(*, site: str, username: str, password: str, piece_id: str, title: str, content: str, excerpt: str, slug: str,
                  status: str, category_id: str, author_id: str, pictures: list) -> PublishResult:
    body: dict = {"title": title, "content": to_html(content), "status": "publish" if status == "publish" else "draft"}
    if excerpt:
        body["excerpt"] = excerpt
    if slug:
        body["slug"] = slug
    if category_id.isdigit():
        body["categories"] = [int(category_id)]
    if author_id.isdigit():
        body["author"] = int(author_id)
    headers = _headers(username, password)
    note = None
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            if pictures:
                cover = await _upload_cover(client, site, headers, pictures[0])
                if cover:
                    body["featured_media"] = cover
                else:
                    note = "The picture could not be added as the cover, so the post went out without it."
            response = await client.post(_api(site, "posts"), json=body, headers=headers)
    except httpx.TimeoutException:
        return PublishResult(success=False, platform="wordpress", piece_id=piece_id, error_type="TRANSIENT", error_code=408, error_message="WordPress took too long to answer. Check the site before trying again.")
    except httpx.HTTPError:
        return PublishResult(success=False, platform="wordpress", piece_id=piece_id, error_type="FATAL", error_code=502, error_message="That site could not be reached.")
    if response.status_code in (200, 201):
        data = response.json()
        return PublishResult(success=True, platform="wordpress", piece_id=piece_id, platform_post_id=str(data.get("id", "")), platform_post_url=data.get("link"), media_dropped_reason=note)
    return failure("wordpress", piece_id, response, message=reply_message(response), reconnect_label=LABEL)
