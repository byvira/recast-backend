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


# ── Leaving a refusing provider alone for a while ────────────────────────────
# A provider that refused for a reason the next call will not fix (wrong model name, no credit, a used-up minute) was tried
# again on every call: in production, 90 "model not found" and 14 "payment required" in 30 days, each one a wasted call before
# the next backup. Each provider and model is now left alone for a while after such a refusal. Order and answers are unchanged:
# the next provider in line is tried, exactly as when this one fails, and when every one is being left alone they are all
# tried anyway, so a call never fails without trying.
_skip_until: dict[str, float] = {}


def _skip_seconds(exc: BaseException) -> float:
    """How long to leave a provider alone after this failure. 0 for a failure worth trying again on the next call."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status in (401, 402, 403, 404):
        return 3600.0  # wrong key, no credit, no access or no such model: a person has to change something
    if status == 429:
        try:
            asked = float((response.headers or {}).get("retry-after") or 0)
        except (TypeError, ValueError, AttributeError):
            asked = 0.0
        return min(max(asked, 60.0), 600.0)
    if isinstance(status, int) and status >= 500:
        return 30.0
    return 0.0


def _note_failure(key: str, exc: BaseException) -> None:
    seconds = _skip_seconds(exc)
    if seconds:
        _skip_until[key] = time.monotonic() + seconds


def _in_turn(keys: list[str]) -> list[int]:
    """The positions to try, in order: those not being left alone, or every one when all of them are."""
    now = time.monotonic()
    ready = [i for i, k in enumerate(keys) if _skip_until.get(k, 0.0) <= now]
    return ready or list(range(len(keys)))


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
_JSON_MODE = {"mistral", "nvidia", "cloudflare"}


def _count_usage(payload: dict, prompt: str, system: str, text: str) -> None:
    """Adds the call to the workspace's daily AI usage, like a call answered by the main provider. When the provider reports no
    token count it is estimated from the text length, so a day answered by a fallback is not counted as free."""
    from app.shared.llm import _record_workspace_usage

    used = (payload.get("usage") or {}).get("total_tokens")
    if not isinstance(used, int) or used <= 0:
        used = estimate_tokens(prompt, system, text)
    _record_workspace_usage(used)


def estimate_tokens(*parts: str) -> int:
    """A rough token count: about one token for every three characters."""
    return max(1, sum(len(part or "") for part in parts) // 3)


async def _chat(p: TextProvider, prompt: str, system: str, json_mode: bool = False) -> str:
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    body: dict = {"model": p.model, "messages": messages, "temperature": 0.7}
    if json_mode and p.name in _JSON_MODE:
        body["response_format"] = {"type": "json_object"}
    async with track(p.name, p.model, feature=None):
        async with httpx.AsyncClient(timeout=TEXT_TIMEOUT_S) as client:
            r = await client.post(p.url, headers={"Authorization": f"Bearer {p.key}"}, json=body)
            r.raise_for_status()
            payload = r.json()
            text = ((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        if not text.strip():
            raise RuntimeError("The provider returned an empty answer.")
        _count_usage(payload, prompt, system, text)
        return text


async def cloudflare_text(prompt: str, system: str = "", json_mode: bool = False) -> str | None:
    """One answer from Workers AI (OpenAI-compatible endpoint), or None when it is not set up or does not answer. This is the only
    backup that text from a member's own footage may go to."""
    if not settings.CLOUDFLARE_API_TOKEN or not settings.CLOUDFLARE_ACCOUNT_ID or not settings.CLOUDFLARE_TEXT_MODEL:
        return None
    p = TextProvider(
        "cloudflare", f"https://api.cloudflare.com/client/v4/accounts/{settings.CLOUDFLARE_ACCOUNT_ID}/ai/v1/chat/completions",
        settings.CLOUDFLARE_API_TOKEN, settings.CLOUDFLARE_TEXT_MODEL,
    )
    try:
        with fallback_scope():
            text = await _chat(p, prompt, system, json_mode)
        logger.info("Served this answer via the cloudflare fallback.")
        return text
    except Exception as exc:  # noqa: BLE001
        # the only backup for a member's own footage, so it is never left alone: it is always tried
        logger.warning("cloudflare text fallback failed: %s", exc)
        return None


async def open_text_fallback(prompt: str, system: str = "", json_mode: bool = False) -> str | None:
    """The first open-model provider that answers, or None when none is set up or all of them fail. A provider that just refused
    for a lasting reason is passed over for a while (see _skip_until)."""
    providers = text_providers()
    keys = [f"text:{p.name}:{p.model}" for p in providers]
    for i in _in_turn(keys):
        p = providers[i]
        try:
            with fallback_scope():
                text = await _chat(p, prompt, system, json_mode)
            logger.info("Served this answer via the %s fallback.", p.name)
            return text
        except Exception as exc:  # noqa: BLE001
            _note_failure(keys[i], exc)
            logger.warning("%s text fallback failed: %s", p.name, exc)
    return None


async def open_vision_fallback(prompt: str, image_bytes: bytes, mime_type: str = "image/jpeg") -> str | None:
    """Describes a picture with a free OpenRouter model that accepts images. None when there is no key or both fail."""
    if not settings.OPENROUTER_API_KEY or not image_bytes:
        return None
    data_uri = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode()}"
    content = [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": data_uri}}]
    models = [m for m in (settings.OPENROUTER_VISION_MODEL, settings.OPENROUTER_VISION_MODEL_2, settings.OPENROUTER_VISION_MODEL_3) if m]
    keys = [f"vision:openrouter:{m}" for m in models]
    for i in _in_turn(keys):
        model = models[i]
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
            _note_failure(keys[i], exc)
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
    keys = [f"image:{name}" for name, _ in steps]
    for i in _in_turn(keys):
        name, fn = steps[i]
        try:
            with fallback_scope():
                data = await fn(prompt)
            logger.info("Served this image via the %s fallback.", name)
            return data
        except Exception as exc:  # noqa: BLE001
            _note_failure(keys[i], exc)
            logger.warning("%s image fallback failed: %s", name, exc)
            if errors is not None:
                errors.append(f"{name.capitalize()} did not return a picture ({_brief(exc)}).")
    if not steps and errors is not None:
        errors.append("No backup picture service is switched on.")
    return None


def _brief(exc: BaseException) -> str:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return f"error {status}" if status else exc.__class__.__name__
