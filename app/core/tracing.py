"""LangSmith tracing for the whole app — every LLM call, every tool loop, every
agent graph, whether it goes through LangGraph or not.

Enable by setting in ``.env`` / the process environment::

    LANGCHAIN_TRACING_V2=true
    LANGCHAIN_API_KEY=ls__...
    LANGCHAIN_PROJECT=recast-agents      # optional, this is the default

``init_tracing()`` copies those from ``Settings`` into ``os.environ`` (LangChain
reads environment variables, not our Settings object) and must run once per
process before the first agent call — it's called from the API lifespan and the
arq worker startup.

Primitives:
  * ``wrap_groq(client)``      — patch the raw Groq SDK client so every
    ``chat.completions.create`` (llm.py helpers AND the supervisor ReAct loop)
    is a traced LLM run with tool-call rendering.
  * ``traced_agent(agent, ws, user_id, **meta)`` — context manager that tags
    every run created inside it with ``agent:<name>`` + ``ws:<id>`` and puts
    ``workspace_id`` / ``user_id`` in metadata. Tags + metadata propagate to
    every nested run (LLM, tool, embedding).
  * ``ainvoke_traced(graph, state, ...)`` — invoke a compiled LangGraph inside a
    named span; returns ``(final_state, run_url)``.
  * ``tool_run(name)`` — decorator that makes one tool invocation its own
    ``tool`` child run with its name + args visible.

Everything here is a transparent pass-through when tracing is disabled, and any
tracing-infrastructure failure is swallowed — tracing can never break a real call.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator

logger = logging.getLogger(__name__)

_initialised = False
_DEFAULT_PROJECT = "recast-agents"


# ─────────────────────────────────────────────────────────────────────────────
# init / status
# ─────────────────────────────────────────────────────────────────────────────

def init_tracing() -> bool:
    """Push LANGCHAIN_* settings into ``os.environ``. Returns whether tracing is
    active (flag on AND an API key present). Idempotent."""
    global _initialised
    from app.core.config import settings

    if settings.LANGCHAIN_API_KEY:
        os.environ.setdefault("LANGCHAIN_API_KEY", settings.LANGCHAIN_API_KEY)
        os.environ.setdefault("LANGSMITH_API_KEY", settings.LANGCHAIN_API_KEY)
    os.environ.setdefault(
        "LANGCHAIN_ENDPOINT", settings.LANGCHAIN_ENDPOINT or "https://api.smith.langchain.com"
    )
    os.environ.setdefault("LANGCHAIN_PROJECT", settings.LANGCHAIN_PROJECT or _DEFAULT_PROJECT)
    if settings.LANGCHAIN_TRACING_V2:
        os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")

    enabled = tracing_enabled()
    if not _initialised:
        logger.info(
            "LangSmith tracing %s (project=%s)",
            "ENABLED" if enabled else "disabled", os.environ.get("LANGCHAIN_PROJECT"),
        )
        _initialised = True
    return enabled


def tracing_enabled() -> bool:
    return (
        os.environ.get("LANGCHAIN_TRACING_V2", "").lower() in ("true", "1", "yes")
        and bool(os.environ.get("LANGCHAIN_API_KEY"))
    )


def project_name() -> str:
    return os.environ.get("LANGCHAIN_PROJECT", _DEFAULT_PROJECT)


# ─────────────────────────────────────────────────────────────────────────────
# tag / metadata convention — one place so every call site is consistent
# ─────────────────────────────────────────────────────────────────────────────

def agent_tags(
    agent: str,
    workspace_id: str | None,
    user_id: str | None = None,
    *,
    extra_tags: "tuple[str, ...] | list[str]" = (),
    metadata: dict[str, Any] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    tags = [f"agent:{agent}"]
    if workspace_id:
        tags.append(f"ws:{workspace_id}")
    tags.extend(t for t in extra_tags if t)

    meta: dict[str, Any] = {"agent": agent}
    if workspace_id:
        meta["workspace_id"] = workspace_id
    if user_id:
        meta["user_id"] = user_id
    for k, v in (metadata or {}).items():
        if v is not None:
            meta[k] = v
    return tags, meta


# ─────────────────────────────────────────────────────────────────────────────
# raw Groq client wrapper
# ─────────────────────────────────────────────────────────────────────────────

def wrap_groq(client: Any) -> Any:
    """Return *client* patched so every ``chat.completions.create`` is a traced
    LLM run. Idempotent; returns the original client if tracing is off or the
    wrap fails.

    langsmith's ``wrap_openai`` expects both ``.chat.completions`` and a legacy
    ``.completions`` namespace; the Groq SDK only has the former, so we attach a
    never-called stub for ``.completions`` before wrapping.
    """
    if not tracing_enabled() or client is None:
        return client
    if getattr(client, "_ls_wrapped", False):
        return client
    try:
        import types as _types

        from langsmith.wrappers import wrap_openai

        if not hasattr(client, "completions"):
            async def _unused_create(*_a: Any, **_k: Any) -> Any:  # pragma: no cover
                raise RuntimeError("legacy /completions endpoint is not used by this app")

            client.completions = _types.SimpleNamespace(create=_unused_create)

        wrapped = wrap_openai(client, chat_name="groq.chat")
        try:
            wrapped._ls_wrapped = True
        except Exception:  # noqa: BLE001
            pass
        logger.info("LangSmith: Groq client wrapped for tracing")
        return wrapped
    except Exception as exc:  # noqa: BLE001
        logger.error("wrap_groq failed — using unwrapped client: %s", exc)
        return client


# ─────────────────────────────────────────────────────────────────────────────
# traced_agent — blanket-tag a block of work
# ─────────────────────────────────────────────────────────────────────────────

@contextmanager
def traced_agent(
    agent: str,
    workspace_id: str | None = None,
    user_id: str | None = None,
    **extra_meta: Any,
) -> Iterator[None]:
    """Tag every LangSmith run created inside this block with ``agent:<name>``,
    ``ws:<workspace_id>`` and metadata. Pass-through when tracing is off or on
    any setup failure."""
    if not tracing_enabled():
        yield
        return
    try:
        from langsmith.run_helpers import tracing_context

        tags, meta = agent_tags(agent, workspace_id, user_id, metadata=extra_meta)
        cm = tracing_context(tags=tags, metadata=meta)
    except Exception as exc:  # noqa: BLE001
        logger.warning("traced_agent(%s): setup failed, running untraced: %s", agent, exc)
        yield
        return
    with cm:
        yield


# ─────────────────────────────────────────────────────────────────────────────
# ainvoke_traced — a compiled LangGraph inside a named span
# ─────────────────────────────────────────────────────────────────────────────

async def ainvoke_traced(
    graph,
    state: Any,
    *,
    run_name: str,
    agent: str,
    workspace_id: str | None = None,
    user_id: str | None = None,
    extra_tags: "tuple[str, ...] | list[str]" = (),
    metadata: dict[str, Any] | None = None,
) -> tuple[Any, str]:
    """Invoke a compiled LangGraph, tagged for LangSmith.

    Returns ``(final_state, run_url)``. ``run_url`` is ``""`` when tracing is
    disabled or the URL can't be resolved. On any tracing failure this falls
    back to a plain untraced invoke — it never raises for a tracing reason.
    """
    tags, meta = agent_tags(agent, workspace_id, user_id, extra_tags=extra_tags, metadata=metadata)
    # Also passed straight to LangGraph so its internal node runs carry the same
    # tags/metadata even where langsmith context propagation doesn't reach.
    config = {"run_name": run_name, "tags": tags, "metadata": meta}

    if not tracing_enabled():
        return await graph.ainvoke(state, config=config), ""

    try:
        from langsmith.run_helpers import trace, tracing_context

        with tracing_context(tags=tags, metadata=meta):
            with trace(name=run_name, run_type="chain", tags=tags, metadata=meta) as rt:
                result = await graph.ainvoke(state, config=config)
                url = ""
                try:
                    url = rt.get_url() or ""
                except Exception as exc:  # noqa: BLE001
                    logger.warning("tracing: could not resolve run url: %s", exc)
        return result, url
    except Exception as exc:  # noqa: BLE001
        logger.error("tracing: traced invoke failed, retrying untraced: %s", exc)
        return await graph.ainvoke(state, config=config), ""


# ─────────────────────────────────────────────────────────────────────────────
# tool_run — one tool invocation → one traced child run
# ─────────────────────────────────────────────────────────────────────────────

def _identity_decorator(fn):
    return fn


def tool_run(name: str):
    """Decorator: wrap a single tool callable so its invocation shows up as a
    ``tool`` run named *name* with its arguments visible. No-op when tracing off."""
    if not tracing_enabled():
        return _identity_decorator
    try:
        from langsmith import traceable

        return traceable(run_type="tool", name=name)
    except Exception:  # noqa: BLE001
        return _identity_decorator


# ─────────────────────────────────────────────────────────────────────────────
# traceable — safe re-export for decorating llm.py's Gemini helpers
# ─────────────────────────────────────────────────────────────────────────────

try:  # pragma: no cover - import guard
    from langsmith import traceable as _traceable
except Exception:  # noqa: BLE001
    def _traceable(*args: Any, **_kwargs: Any):  # type: ignore[misc]
        if args and callable(args[0]) and len(args) == 1:
            return args[0]
        return _identity_decorator


def traceable(*args: Any, **kwargs: Any):
    """``langsmith.traceable`` when available, else an identity decorator.

    ``@traceable`` is itself a cheap pass-through at call time when tracing is
    disabled, so decorating unconditionally is safe.
    """
    return _traceable(*args, **kwargs)


def add_run_metadata(**kv: Any) -> None:
    """Attach metadata (token counts, model, dims…) to the current run, if any.
    Silent no-op when there is no active run or tracing is off."""
    if not tracing_enabled():
        return
    try:
        from langsmith.run_helpers import get_current_run_tree

        rt = get_current_run_tree()
        if rt is not None:
            rt.add_metadata({k: v for k, v in kv.items() if v is not None})
    except Exception:  # noqa: BLE001
        pass
