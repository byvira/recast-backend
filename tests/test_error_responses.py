"""A failure always reaches the browser as the failure it is. An unhandled error and a rate limit are answered from outside the CORS
layer unless the handlers add the headers themselves, and the browser then shows a CORS error that hides the real reason."""
import httpx
import pytest
from fastapi import FastAPI, Request
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.core.errors import make_handlers

ORIGIN = "https://recastbyvira.vercel.app"


def _app(origins=(ORIGIN,)) -> FastAPI:
    app = FastAPI()
    limiter = Limiter(key_func=get_remote_address)
    app.state.limiter = limiter
    rate_limit, unhandled = make_handlers(origins)
    app.add_exception_handler(RateLimitExceeded, rate_limit)
    app.add_exception_handler(Exception, unhandled)

    @app.get("/boom")
    async def boom():
        raise RuntimeError("database exploded with secret details")

    @app.get("/limited")
    @limiter.limit("1/minute")
    async def limited(request: Request):
        return {"ok": True}

    return app


async def _get(app: FastAPI, path: str, origin: str | None = ORIGIN) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, headers={"Origin": origin} if origin else {})


async def test_an_unhandled_error_is_a_clean_500_with_the_cors_header_and_no_internal_details():
    res = await _get(_app(), "/boom")

    assert res.status_code == 500
    assert res.headers["access-control-allow-origin"] == ORIGIN
    assert res.headers["access-control-allow-credentials"] == "true"
    body = res.json()
    assert body["code"] == "server_error"
    assert "secret" not in res.text and "RuntimeError" not in res.text


async def test_a_rate_limit_is_a_429_with_a_wait_time_and_the_cors_header():
    app = _app()
    await _get(app, "/limited")
    res = await _get(app, "/limited")

    assert res.status_code == 429
    assert res.headers["access-control-allow-origin"] == ORIGIN
    body = res.json()
    assert body["code"] == "rate_limited" and body["retry_after"] >= 1
    assert res.headers["retry-after"] == str(body["retry_after"])
    assert "access-control-expose-headers" in res.headers


@pytest.mark.parametrize("path", ["/boom", "/limited"])
async def test_an_origin_that_is_not_allowed_gets_no_cors_header(path):
    app = _app()
    await _get(app, "/limited", origin="https://evil.example")
    res = await _get(app, path, origin="https://evil.example")

    assert res.status_code in (429, 500)
    assert "access-control-allow-origin" not in res.headers


async def test_a_request_with_no_origin_still_gets_the_structured_error():
    res = await _get(_app(), "/boom", origin=None)
    assert res.status_code == 500 and res.json()["code"] == "server_error"
