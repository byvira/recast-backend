"""Extra fallbacks for when Gemini (text, pictures) and Cloudflare (pictures) are not answering, so the product keeps
working until the paid tiers are switched on. All of them run open models on free plans.

Text, tried in order after Gemini: Mistral, then OpenRouter's free models. Pictures, tried in order after Cloudflare
and Gemini: Hugging Face FLUX.1 schnell, then Pollinations (no key needed).

A provider without a key is skipped, never an error. Every attempt goes through `track`, so each one shows on the LLM
health page as a fallback and a failed attempt opens an issue like any other. Model names are settings, because free
model lists change often; if one is retired, change the setting, not the code."""
from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from app.core.config import settings
from app.shared.llm_health.track import fallback_scope, track

logger = logging.getLogger(__name__)

TEXT_TIMEOUT_S = 45.0
IMAGE_TIMEOUT_S = 90.0


@dataclass(frozen=True)
class TextProvider:
    name: str
    url: str
    key: str
    model: str


def text_providers() -> list[TextProvider]:
    """The configured open-model text fallbacks, in the order they are tried."""
    out: list[TextProvider] = []
    if settings.MISTRAL_API_KEY:
        out.append(TextProvider("mistral", "https://api.mistral.ai/v1/chat/completions", settings.MISTRAL_API_KEY, settings.MISTRAL_MODEL))
    if settings.NVIDIA_API_KEY:
        out.append(TextProvider("nvidia", "https://integrate.api.nvidia.com/v1/chat/completions", settings.NVIDIA_API_KEY, settings.NVIDIA_MODEL))
    if settings.OPENROUTER_API_KEY:
        for model in (settings.OPENROUTER_MODEL, settings.OPENROUTER_MODEL_2):
            if model:
                out.append(TextProvider("openrouter", "https://openrouter.ai/api/v1/chat/completions", settings.OPENROUTER_API_KEY, model))
    return out


# providers whose chat endpoint is known to accept a JSON mode (OpenRouter's free models differ by model, so it is left out)
_JSON_MODE = {"mistral", "nvidia"}


async def _chat(p: TextProvider, prompt: str, system: str, json_mode: bool = False) -> str:
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    body: dict = {"model": p.model, "messages": messages, "temperature": 0.7}
    if json_mode and p.name in _JSON_MODE:
        body["response_format"] = {"type": "json_object"}
    async with track(p.name, p.model, feature=None):
        async with httpx.AsyncClient(timeout=TEXT_TIMEOUT_S) as client:
            r = await client.post(p.url, headers={"Authorization": f"Bearer {p.key}"}, json=body)
            r.raise_for_status()
            text = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        if not text.strip():
            raise RuntimeError("The provider returned an empty answer.")
        return text


async def open_text_fallback(prompt: str, system: str = "", json_mode: bool = False) -> str | None:
    """The first open-model provider that answers, or None when none is set up or all of them fail."""
    for p in text_providers():
        try:
            with fallback_scope():
                text = await _chat(p, prompt, system, json_mode)
            logger.info("Served this answer via the %s fallback.", p.name)
            return text
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s text fallback failed: %s", p.name, exc)
    return None


async def open_vision_fallback(prompt: str, image_bytes: bytes, mime_type: str = "image/jpeg") -> str | None:
    """Describes a picture with a free OpenRouter model that accepts images. None when there is no key or both fail."""
    if not settings.OPENROUTER_API_KEY or not image_bytes:
        return None
    data_uri = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode()}"
    content = [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": data_uri}}]
    for model in (settings.OPENROUTER_VISION_MODEL, settings.OPENROUTER_VISION_MODEL_2, settings.OPENROUTER_VISION_MODEL_3):
        if not model:
            continue
        try:
            with fallback_scope():
                async with track("openrouter", model, feature="vision"):
                    async with httpx.AsyncClient(timeout=TEXT_TIMEOUT_S) as client:
                        r = await client.post(
                            "https://openrouter.ai/api/v1/chat/completions",
                            headers={"Authorization": f"Bearer {settings.OPENROUTER_API_KEY}"},
                            json={"model": model, "messages": [{"role": "user", "content": content}], "temperature": 0.2},
                        )
                        r.raise_for_status()
                        text = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                    if not text.strip():
                        raise RuntimeError("The provider returned an empty answer.")
            logger.info("Looked at this picture via the %s fallback.", model)
            return text
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s vision fallback failed: %s", model, exc)
    return None


async def _huggingface(prompt: str) -> bytes:
    model = settings.HUGGINGFACE_IMAGE_MODEL
    async with track("huggingface", model, feature="image_generation"):
        async with httpx.AsyncClient(timeout=IMAGE_TIMEOUT_S) as client:
            r = await client.post(
                f"https://router.huggingface.co/{settings.HUGGINGFACE_IMAGE_PROVIDER}/v1/images/generations",
                headers={"Authorization": f"Bearer {settings.HUGGINGFACE_API_TOKEN}"},
                json={"model": model, "prompt": prompt, "response_format": "b64_json", "size": "1024x1024"},
            )
            r.raise_for_status()
        encoded = ((r.json().get("data") or [{}])[0]).get("b64_json")
        if not encoded:
            raise RuntimeError("Hugging Face did not return a picture.")
        return base64.b64decode(encoded)


async def _pollinations(prompt: str) -> bytes:
    async with track("pollinations", "flux", feature="image_generation"):
        async with httpx.AsyncClient(timeout=IMAGE_TIMEOUT_S, follow_redirects=True) as client:
            r = await client.get(
                f"https://image.pollinations.ai/prompt/{quote(prompt[:900])}",
                params={"model": "flux", "width": 1024, "height": 1024, "nologo": "true", "seed": int(time.time()) % 100000},
            )
            r.raise_for_status()
        if not r.headers.get("content-type", "").startswith("image/") or not r.content:
            raise RuntimeError("Pollinations did not return a picture.")
        return r.content


async def open_image_fallback(prompt: str, errors: list[str] | None = None) -> bytes | None:
    """A picture from the first open-model provider that answers, or None. Each provider that fails adds a plain note to
    `errors` when given."""
    steps = []
    if settings.HUGGINGFACE_API_TOKEN:
        steps.append(("huggingface", _huggingface))
    if settings.POLLINATIONS_ENABLED:
        steps.append(("pollinations", _pollinations))
    for name, fn in steps:
        try:
            with fallback_scope():
                data = await fn(prompt)
            logger.info("Served this image via the %s fallback.", name)
            return data
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s image fallback failed: %s", name, exc)
            if errors is not None:
                errors.append(f"{name.capitalize()} did not return a picture ({_brief(exc)}).")
    if not steps and errors is not None:
        errors.append("No backup picture service is switched on.")
    return None


def _brief(exc: BaseException) -> str:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return f"error {status}" if status else exc.__class__.__name__
