"""The request body cap (app.core.middleware.MaxBodySizeMiddleware).

Regression for image uploads showing a CORS error: the cap sits outside the CORS
layer, so a rejection it made carried no CORS headers and the browser reported
"No 'Access-Control-Allow-Origin' header" instead of "too large". Real upload
routes are now exempt (they check their own size), and any rejection that
remains carries the CORS headers for allowed origins.

Uses a tiny standalone app, so no database or login is needed.
"""

import httpx
import pytest
from fastapi import FastAPI

from app.core.middleware import MAX_REQUEST_BODY_BYTES, MaxBodySizeMiddleware

ORIGIN = "https://recastbyvira.vercel.app"
BIG = MAX_REQUEST_BODY_BYTES + 1


def _client(allowed=(ORIGIN,)) -> httpx.AsyncClient:
    app = FastAPI()
    app.add_middleware(MaxBodySizeMiddleware, allowed_origins=list(allowed))

    @app.post("/{full_path:path}")
    async def echo(full_path: str) -> dict:
        return {"ok": True, "path": full_path}

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _post(client, path, size=BIG, origin=ORIGIN):
    headers = {"content-length": str(size)}
    if origin:
        headers["origin"] = origin
    return await client.post(path, content=b"x", headers=headers)


@pytest.mark.parametrize("path", [
    "/api/v1/image-assets/upload",
    "/api/v1/audio-assets/upload",
    "/api/v1/support/uploads",
    "/api/v1/ops/support/uploads",
    "/api/v1/media",
])
async def test_real_upload_routes_accept_a_large_body(path):
    async with _client() as client:
        res = await _post(client, path)
    assert res.status_code == 200


@pytest.mark.parametrize("path", [
    "/api/v1/text/generate",
    "/api/v1/image-assets/generate",
    "/api/v1/image-assets/upload/extra",
    "/api/v1/image-assets/uploads",
    "/api/v1/audio-assets/upload-not",
    "/prefix/api/v1/image-assets/upload",
])
async def test_json_routes_and_lookalike_paths_keep_the_cap(path):
    async with _client() as client:
        res = await _post(client, path)
    assert res.status_code == 413


async def test_a_body_within_the_cap_passes():
    async with _client() as client:
        res = await _post(client, "/api/v1/text/generate", size=MAX_REQUEST_BODY_BYTES)
    assert res.status_code == 200


async def test_a_rejection_carries_cors_headers_for_an_allowed_origin():
    async with _client() as client:
        res = await _post(client, "/api/v1/text/generate")
    assert res.status_code == 413
    assert res.json() == {"detail": "Request body too large."}
    assert res.headers["access-control-allow-origin"] == ORIGIN
    assert res.headers["access-control-allow-credentials"] == "true"
    assert "origin" in res.headers["vary"].lower()


async def test_a_rejection_never_grants_cors_to_an_unknown_origin():
    async with _client() as client:
        res = await _post(client, "/api/v1/text/generate", origin="https://evil.example")
    assert res.status_code == 413
    assert "access-control-allow-origin" not in res.headers


async def test_a_rejection_without_an_origin_has_no_cors_headers():
    async with _client() as client:
        res = await _post(client, "/api/v1/text/generate", origin=None)
    assert res.status_code == 413
    assert "access-control-allow-origin" not in res.headers
