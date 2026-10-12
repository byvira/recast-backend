"""Streaming downloads stop at their limit (app.shared.safe_fetch)."""
import httpx
import pytest

from app.shared.safe_fetch import ResponseTooLarge, get_limited


def _client(body: bytes, headers=None) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=body, headers=headers or {})))


async def test_a_small_answer_is_returned_whole():
    async with _client(b"hello") as client:
        response, body = await get_limited(client, "http://x/f", 100)
    assert response.status_code == 200 and body == b"hello"


async def test_an_oversize_answer_is_refused():
    async with _client(b"x" * 5000) as client:
        with pytest.raises(ResponseTooLarge):
            await get_limited(client, "http://x/f", 1000)


async def test_an_oversize_answer_can_be_cut_at_the_limit():
    async with _client(b"x" * 5000) as client:
        _, body = await get_limited(client, "http://x/f", 1000, truncate=True)
    assert len(body) == 1000


async def test_a_declared_length_over_the_limit_is_refused_before_reading():
    async with _client(b"x", {"content-length": "999999"}) as client:
        with pytest.raises(ResponseTooLarge):
            await get_limited(client, "http://x/f", 1000)
