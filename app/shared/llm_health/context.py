"""Which product feature and prompt a model call belongs to, carried in context variables so no call site
has to pass it down. Set once per request or agent run, like `usage_workspace` in app.shared.llm."""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Iterator

_feature: contextvars.ContextVar[str | None] = contextvars.ContextVar("llm_feature", default=None)
_prompt_path: contextvars.ContextVar[str | None] = contextvars.ContextVar("llm_prompt_path", default=None)

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


def current_feature() -> str:
    return _feature.get() or UNKNOWN_FEATURE


def current_prompt_path() -> str | None:
    return _prompt_path.get()


def current_request_id() -> str | None:
    try:
        import structlog

        value = structlog.contextvars.get_contextvars().get("request_id")
        return str(value) if value else None
    except Exception:  # noqa: BLE001 - a missing logger context is not a reason to fail a call
        return None
