"""The fallback chain for every kind of work, in the order the product tries them. This is the one list the health page reads, so a
provider added to a chain shows on the page without anyone editing the page, and a test fails if a chain names a provider the page
does not know. Whether a step is switched on is read from settings each time, never copied here."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.core.config import settings


@dataclass(frozen=True)
class Step:
    provider: str
    model: str
    #: False when the step is switched off on purpose (a setting), which is different from a missing key
    on: Callable[[], bool] = lambda: True
    #: private content never goes to a step marked False; only plain business writing may
    note: str = ""


def _backup_picture_models() -> list[str]:
    return [m.strip() for m in settings.CLOUDFLARE_IMAGE_BACKUP_MODELS.split(",") if m.strip()]


CHAINS: dict[str, dict] = {
    "writing": {
        "label": "Writing",
        "steps": [
            Step("groq", "gpt-oss-120b (3 tries)"), Step("groq", "gpt-oss-20b"), Step("gemini", "flash"),
            Step("mistral", settings.MISTRAL_MODEL, note="not for private content"), Step("nvidia", settings.NVIDIA_MODEL, note="not for private content"),
            Step("openrouter", settings.OPENROUTER_MODEL, note="not for private content"),
            Step("pollinations", "openai", lambda: settings.POLLINATIONS_ENABLED, "not for private content"),
        ],
    },
    "pictures": {
        "label": "Pictures",
        "steps": [
            Step("cloudflare", "flux-1-schnell"), Step("gemini", "image (paid, capped)"),
            *[Step("cloudflare", m.split("/")[-1]) for m in _backup_picture_models()],
            Step("pollinations", "flux", lambda: settings.POLLINATIONS_ENABLED),
            Step("stablehorde", "anonymous queue", lambda: settings.STABLEHORDE_ENABLED, "off by default: the description goes to a public service"),
            Step("huggingface", settings.HUGGINGFACE_IMAGE_MODEL, lambda: settings.HUGGINGFACE_IMAGE_ENABLED),
        ],
    },
    "voice": {
        "label": "Voice",
        "steps": [
            Step("elevenlabs", "eleven_multilingual_v2", lambda: settings.ELEVENLABS_ENABLED),
            Step("deepgram", "aura-2"), Step("gemini", settings.GEMINI_TTS_MODEL, lambda: settings.GEMINI_TTS_ENABLED),
        ],
    },
    "speech_to_text": {
        "label": "Speech to text",
        "steps": [Step("groq", "whisper-large-v3 / turbo"), Step("deepgram", "nova-3"), Step("mistral", settings.VOXTRAL_MODEL)],
    },
    "picture_understanding": {
        "label": "Picture understanding",
        "steps": [Step("gemini", "flash"), Step("openrouter", settings.OPENROUTER_VISION_MODEL)],
    },
    "embeddings": {
        "label": "Embeddings",
        "steps": [Step("gemini", "gemini-embedding-001")],
    },
}


def providers_in_chains() -> set[str]:
    return {s.provider for chain in CHAINS.values() for s in chain["steps"]}


def describe(providers: dict[str, dict]) -> list[dict]:
    """Every chain with each step's live state. `providers` is what `service.provider_states()` returns. A chain is "thin" when only one
    step is working, and "broken" when none is."""
    out = []
    for key, chain in CHAINS.items():
        steps = []
        for s in chain["steps"]:
            info = providers.get(s.provider) or {}
            switched_on = s.on()
            configured = bool(info.get("configured", False)) and switched_on
            status = info.get("status", "idle") if configured else ("off" if not switched_on else "not_set")
            steps.append({"provider": s.provider, "label": info.get("label") or s.provider.capitalize(), "model": s.model, "status": status, "note": s.note, "usable": configured and status not in ("down", "limit_reached")})
        usable = sum(1 for st in steps if st["usable"])
        out.append({"id": key, "label": chain["label"], "steps": steps, "usable": usable, "depth": "broken" if usable == 0 else "thin" if usable == 1 else "ok"})
    return out
