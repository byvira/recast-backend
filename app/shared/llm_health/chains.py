"""The fallback chain for every kind of work, in the order the product tries them. This is the one list the health page reads, so a
provider added to a chain shows on the page without anyone editing the page, and a test fails if a chain names a provider the page
does not know. Whether a step is switched on is read from settings each time, never copied here.

Each step names the model ids it uses (`models`); the scheduled check (`probe.py`) keys its readings the same way, so a step shows when it was
last checked and whether it answered."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from app.core.config import settings
from app.shared.llm import EMBED_MODEL, GeminiModel, GroqModel
from app.shared.llm_health import probe


@dataclass(frozen=True)
class Step:
    provider: str
    #: what the page shows
    model: str
    #: False when the step is switched off on purpose (a setting), which is different from a missing key
    on: Callable[[], bool] = lambda: True
    #: private content never goes to a step marked False; only plain business writing may
    note: str = ""
    #: the model ids the scheduled check tests for this step; empty for a step that is never checked (paid, so a test call would cost money)
    models: tuple[str, ...] = ()


def _backup_picture_models() -> list[str]:
    return [m.strip() for m in settings.CLOUDFLARE_IMAGE_BACKUP_MODELS.split(",") if m.strip()]


def all_chains() -> dict[str, dict]:
    """Built each time it is read, so a changed setting shows without a restart."""
    return {
        "writing": {
            "label": "Writing",
            "steps": [
                Step("groq", "gpt-oss-120b (3 tries)", models=(GroqModel.BALANCED.value,)), Step("groq", "gpt-oss-20b", models=(GroqModel.FAST.value,)),
                Step("gemini", "flash", models=(GeminiModel.FLASH.value,)),
                Step("mistral", settings.MISTRAL_MODEL, note="not for private content", models=(settings.MISTRAL_MODEL,)),
                Step("nvidia", settings.NVIDIA_MODEL, note="not for private content", models=(settings.NVIDIA_MODEL,)),
                Step("openrouter", settings.OPENROUTER_MODEL, note="not for private content", models=tuple(m for m in (settings.OPENROUTER_MODEL, settings.OPENROUTER_MODEL_2) if m)),
                Step("pollinations", "openai", lambda: settings.POLLINATIONS_ENABLED, "not for private content", models=("openai",)),
            ],
        },
        "pictures": {
            "label": "Pictures",
            "steps": [
                Step("cloudflare", "flux-1-schnell", models=("@cf/black-forest-labs/flux-1-schnell",)), Step("gemini", "image (paid, capped)"),
                *[Step("cloudflare", m.split("/")[-1], models=(m,)) for m in _backup_picture_models()],
                Step("pollinations", "flux", lambda: settings.POLLINATIONS_ENABLED, models=("flux",)),
                Step("stablehorde", "anonymous queue", lambda: settings.STABLEHORDE_ENABLED, "off by default: the description goes to a public service", models=("anonymous",)),
                Step("huggingface", settings.HUGGINGFACE_IMAGE_MODEL, lambda: settings.HUGGINGFACE_IMAGE_ENABLED),
            ],
        },
        "voice": {
            "label": "Voice",
            "steps": [
                Step("elevenlabs", "eleven_multilingual_v2", lambda: settings.ELEVENLABS_ENABLED, models=("eleven_multilingual_v2",)),
                Step("deepgram", "aura-2", models=("aura-2",)),
                Step("cloudflare", "melotts (English, Spanish, French, Chinese, Japanese, Korean)", lambda: settings.CLOUDFLARE_TTS_ENABLED, models=("@cf/myshell-ai/melotts",)),
                Step("gemini", settings.GEMINI_TTS_MODEL, lambda: settings.GEMINI_TTS_ENABLED, models=(settings.GEMINI_TTS_MODEL,)),
                Step("edge", "neural voices (not for a member's own footage)", lambda: settings.EDGE_TTS_ENABLED, "unofficial, off by default", models=("neural",)),
            ],
        },
        "speech_to_text": {
            "label": "Speech to text",
            "steps": [
                Step("groq", "whisper-large-v3 / turbo", models=(GroqModel.WHISPER.value, GroqModel.WHISPER_TURBO.value)),
                Step("deepgram", "nova-3", models=("nova-3",)), Step("mistral", settings.VOXTRAL_MODEL, models=(settings.VOXTRAL_MODEL,)),
            ],
        },
        "picture_understanding": {
            "label": "Picture understanding",
            "steps": [
                Step("gemini", "flash", models=(GeminiModel.FLASH.value,)),
                Step("openrouter", settings.OPENROUTER_VISION_MODEL, models=tuple(m for m in (settings.OPENROUTER_VISION_MODEL, settings.OPENROUTER_VISION_MODEL_2, settings.OPENROUTER_VISION_MODEL_3) if m)),
            ],
        },
        "embeddings": {
            "label": "Embeddings",
            "steps": [
                Step("gemini", EMBED_MODEL, models=(EMBED_MODEL,)),
                Step("mistral", settings.MISTRAL_EMBED_MODEL, models=(settings.MISTRAL_EMBED_MODEL,)),
                Step("cloudflare", settings.CLOUDFLARE_EMBED_MODEL.split("/")[-1], models=(settings.CLOUDFLARE_EMBED_MODEL,)),
            ],
        },
    }


def providers_in_chains() -> set[str]:
    return {s.provider for chain in all_chains().values() for s in chain["steps"]}


def probe_keys(chain_id: str, step: Step) -> list[str]:
    return [probe.key_for(chain_id, step.provider, m) for m in step.models]


def _step_from_readings(base: str, keys: list[str], readings: dict[str, dict]) -> tuple[str, dict[str, Any]]:
    """The status of a step once the scheduled check is taken into account, plus what to show about it. The check only changes the status when
    it is specific: a step with several models is down only when all of them are."""
    found = [readings[k] for k in keys if k in readings]
    if not found:
        return base, {}
    states = [probe.step_state(r) for r in found]
    known = [s for s in states if s]
    extra: dict[str, Any] = {
        "checked_at": max((r.get("checked_at") for r in found if r.get("checked_at")), default=None),
        "retired": any(r.get("state") == "retired" for r in found),
    }
    bad = next((r for r in found if r.get("state") != "ok"), None)
    if bad:
        extra["detail"] = bad.get("detail")
    if len(known) == len(keys) and known and all(s in ("down", "limit_reached") for s in known):
        return ("down" if "down" in known else "limit_reached"), extra
    if "healthy" in known and base == "idle":
        return "healthy", extra
    return base, extra


def describe(providers: dict[str, dict], readings: Optional[dict[str, dict]] = None) -> list[dict]:
    """Every chain with each step's live state. `providers` is what `service.provider_states()` returns and `readings` what the scheduled
    check stored (`probe.all_readings()`). A chain is "thin" when only one step is working, and "broken" when none is."""
    readings = readings or {}
    out = []
    for key, chain in all_chains().items():
        steps = []
        for s in chain["steps"]:
            info = providers.get(s.provider) or {}
            switched_on = s.on()
            configured = bool(info.get("configured", False)) and switched_on
            status = info.get("status", "idle") if configured else ("off" if not switched_on else "not_set")
            extra: dict[str, Any] = {}
            if configured:
                status, extra = _step_from_readings(status, probe_keys(key, s), readings)
            steps.append({"provider": s.provider, "label": info.get("label") or s.provider.capitalize(), "model": s.model, "status": status, "note": s.note,
                          "usable": configured and status not in ("down", "limit_reached"), "checked": bool(s.models), **extra})
        usable = sum(1 for st in steps if st["usable"])
        out.append({"id": key, "label": chain["label"], "steps": steps, "usable": usable, "depth": "broken" if usable == 0 else "thin" if usable == 1 else "ok"})
    return out
