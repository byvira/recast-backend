"""The real parts a pass needs: calling a tool through the gatekeeper as the source's owner, and judging how close an item is to the brand.

Tests give `flow.process` stand-ins for both, so the flow is tested with no provider and no network."""
from __future__ import annotations

from typing import Any, Optional

from app.core import failures
from app.sources import flow
from app.tools import executor
from app.tools.gatekeeper import Call, Gatekeeper, Refusal
from app.tools.registry import get_registry
from app.tools.services import LiveServices

#: refusals after which the rest of a pass cannot work either, so it stops (and a person sees why) instead of failing every item
STOP_CODES = {"tool_off", "plan_limit", "key_cap", "member_inactive", "role_missing", "scope_missing", "auth_invalid", "auth_expired", "auth_revoked", "human_only", "rate_limited"}
CREDIT_WORDS = ("credit", "out of")


def tool_runner(workspace_id: str, user_id: str):
    """`run_tool(name, arguments)` that goes through the gatekeeper as a `source` credential (read and create only), or raises `flow.SourceToolError`."""
    gate = Gatekeeper(get_registry(), LiveServices())
    cred = flow.credential_id(workspace_id, user_id)

    async def run_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            _, outcome = await executor.run_call(gate, Call(credential_id=cred, tool=name, arguments=arguments))
        except Refusal as refusal:
            raise flow.SourceToolError(refusal.code, refusal.message, stop=refusal.code in STOP_CODES) from refusal
        except executor.ToolFailure as failure:
            out_of_credits = failure.code in ("credits_exhausted", "out_of_credits") or any(w in failure.message.lower() for w in CREDIT_WORDS)
            raise flow.SourceToolError(failure.code, failure.message, stop=out_of_credits or not failure.retryable and failure.code in ("tool_off",)) from failure
        return {"data": outcome.data, "run": outcome.run} if outcome else {}

    return run_tool


async def relevance_score(brand_text: str, item_text: str) -> Optional[float]:
    """Cosine closeness of the item to the brand (both made by the same embedding model), or None when no embedding service answers. Never raises."""
    from app.shared.embeddings import embed_with_fallback

    try:
        vectors, space = await embed_with_fallback([brand_text, item_text])
    except Exception:  # noqa: BLE001
        return None
    if len(vectors) != 2 or not space:
        return None
    return sum(a * b for a, b in zip(vectors[0], vectors[1]))


def brand_text_of(profile: dict[str, Any]) -> str:
    """A short description of the brand to compare items against: its name, what it is, who it is for and what it says."""
    parts: list[str] = []
    identity = profile.get("identity") or {}
    for key in ("name", "brand_name", "tagline", "description", "industry", "mission", "about"):
        value = identity.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    audience = profile.get("audience") or {}
    if isinstance(audience, dict):
        parts += [str(v) for k, v in audience.items() if isinstance(v, str) and v.strip()][:4]
    for key in ("pillars_data", "positioning_data"):
        value = profile.get(key)
        if isinstance(value, dict):
            parts += [str(v) for v in value.values() if isinstance(v, str)][:4]
    return " ".join(parts)[:1500]
