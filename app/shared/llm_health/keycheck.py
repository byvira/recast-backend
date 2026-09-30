"""Checks that the keys for the pay-per-use providers are accepted, without spending any of their allowance.

Groq and Gemini are pinged with a one word prompt (`llm_health_check`). The others charge for real use (Cloudflare
neurons, ElevenLabs credits, Deepgram credit), so the test only calls the free account endpoints that say whether
the key is valid. That proves the key works, not that a picture or a voice can be made right now; real use shows that
on the provider cards."""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from app.core.config import settings
from app.shared.llm_health.scrub import scrub_message

TIMEOUT_S = 10.0


async def _get(client: httpx.AsyncClient, url: str, headers: dict[str, str]) -> dict[str, Any]:
    t0 = time.monotonic()
    try:
        r = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        return {"status": "error", "detail": scrub_message(f"{exc.__class__.__name__}: {exc}", 200)}
    ms = round((time.monotonic() - t0) * 1000)
    if r.status_code < 400:
        return {"status": "ok", "latency_ms": ms, "detail": "Key accepted (no allowance used)."}
    return {"status": "error", "latency_ms": ms, "http_status": r.status_code, "detail": scrub_message(f"{r.status_code} {r.text[:150]}", 200)}


async def check_keys() -> dict[str, dict[str, Any]]:
    """One entry per provider. A provider with no key set is `not_set`, which is not a failure."""
    out: dict[str, dict[str, Any]] = {}
    jobs: dict[str, tuple[str, dict[str, str]]] = {}
    if settings.CLOUDFLARE_API_TOKEN:
        base = "https://api.cloudflare.com/client/v4"
        url = f"{base}/accounts/{settings.CLOUDFLARE_ACCOUNT_ID}/tokens/verify" if settings.CLOUDFLARE_ACCOUNT_ID else f"{base}/user/tokens/verify"
        jobs["cloudflare"] = (url, {"Authorization": f"Bearer {settings.CLOUDFLARE_API_TOKEN}"})
    else:
        out["cloudflare"] = {"status": "not_set", "detail": "No Cloudflare key is set on the server."}
    if settings.ELEVENLABS_API_KEY:
        jobs["elevenlabs"] = ("https://api.elevenlabs.io/v1/models", {"xi-api-key": settings.ELEVENLABS_API_KEY})
    else:
        out["elevenlabs"] = {"status": "not_set", "detail": "No ElevenLabs key is set on the server."}
    if settings.DEEPGRAM_API_KEY:
        jobs["deepgram"] = ("https://api.deepgram.com/v1/projects", {"Authorization": f"Token {settings.DEEPGRAM_API_KEY}"})
    else:
        out["deepgram"] = {"status": "not_set", "detail": "No Deepgram key is set on the server."}
    if jobs:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            results = await asyncio.gather(*(_get(client, url, headers) for url, headers in jobs.values()))
        out.update(dict(zip(jobs, results)))
    return out
