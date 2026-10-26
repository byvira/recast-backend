"""Which product feature and prompt a model call belongs to, carried in context variables so no call site
has to pass it down. Set once per request or agent run, like `usage_workspace` in app.shared.llm."""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Iterator

_feature: contextvars.ContextVar[str | None] = contextvars.ContextVar("llm_feature", default=None)
_prompt_path: contextvars.ContextVar[str | None] = contextvars.ContextVar("llm_prompt_path", default=None)
_request_path: contextvars.ContextVar[str | None] = contextvars.ContextVar("llm_request_path", default=None)

# When no call site names its feature, it is worked out: first from the prompt file being used (every prompt goes through
# load_prompt, which records its path), then from the web route that started the work. So a new feature is labelled without
# anyone remembering to add a label, and an explicit llm_context(feature=...) still wins.
_PROMPT_FOLDER_FEATURES = {
    "text": "text", "media": "image", "audio": "audio", "video": "video", "brand": "brand_voice", "campaigns": "campaigns",
    "presets": "presets", "analytics": "analytics", "personal": "remy", "supervisor": "odette", "support": "support",
    "fragments": None, "localized": None, "fixtures": None,
}
_ROUTE_FEATURES = (
    ("/api/v1/support", "support"), ("/api/v1/ops/support", "support"), ("/api/v1/search", "search"),
    ("/api/v1/suggest", "suggestions"), ("/api/v1/image-assets", "image"), ("/api/v1/audio-assets", "audio"),
    ("/api/v1/video", "video"), ("/api/v1/text", "text"), ("/api/v1/pipeline", "text"), ("/api/v1/content", "text"),
    ("/api/v1/campaigns", "campaigns"), ("/api/v1/brand", "brand_voice"), ("/api/v1/voices", "brand_voice"),
    ("/api/v1/presets", "presets"), ("/api/v1/analytics", "analytics"), ("/api/v1/personal", "remy"),
    ("/api/v1/supervisor", "odette"), ("/api/v1/publish", "publish"), ("/api/v1/content-safety", "safety"),
)

UNKNOWN_FEATURE = "unknown"


@contextmanager
def llm_context(feature: str | None = None, prompt_path: str | None = None) -> Iterator[None]:
    """Everything the model is called for inside this block is attributed to `feature` / `prompt_path`."""
    tokens = []
    if feature is not None:
        tokens.append((_feature, _feature.set(feature)))
    if prompt_path is not None:
        tokens.append((_prompt_path, _prompt_path.set(prompt_path)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def set_prompt_path(path: str | None) -> None:
    """Called by `load_prompt`: the last prompt rendered is the one the next model call is for."""
    _prompt_path.set(path)


def set_request_path(path: str | None) -> None:
    """Called once per request by the logging middleware, so model calls in it can be labelled by route."""
    _request_path.set(path)


def feature_for(prompt_path: str | None, request_path: str | None) -> str | None:
    """The feature a call belongs to, from its prompt file's folder, else from the route; None when neither says."""
    folder = (prompt_path or "").replace("\\", "/").strip("/").split("/")[0]
    if folder in _PROMPT_FOLDER_FEATURES and _PROMPT_FOLDER_FEATURES[folder]:
        return _PROMPT_FOLDER_FEATURES[folder]
    path = request_path or ""
    return next((feature for prefix, feature in _ROUTE_FEATURES if path.startswith(prefix)), None)


def current_feature() -> str:
    return _feature.get() or feature_for(_prompt_path.get(), _request_path.get()) or UNKNOWN_FEATURE


def current_prompt_path() -> str | None:
    return _prompt_path.get()


def current_request_id() -> str | None:
    try:
        import structlog

        value = structlog.contextvars.get_contextvars().get("request_id")
        return str(value) if value else None
    except Exception:  # noqa: BLE001 - a missing logger context is not a reason to fail a call
        return None
