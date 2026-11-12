"""Which model does which job in Ask Ekavo. Four roles, each with a model, a token budget and a privacy flag; staff can change them in Ops.

- decide: choose one tool, or a plan, or ask a short question (the small model; a closed choice, so it needs little).
- plan: write the steps of a multi-step request (the larger model, only when more than one step is needed).
- respond: put a short answer in the person's language.
- explain: say why something was refused or failed, in plain words.

The models are the ones the product already has, with the fallback chain it already has: a role names a starting model and the shared layer falls
through to the others when it cannot answer. Everything a person writes is private content, so it only ever goes to the main provider and its
approved backups, never to a free open-model service (`private`). A role cannot name a model outside the allowed list."""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any

from app.db.mongo import get_db
from app.shared.llm import GroqModel, call_llm_structured, private_content

logger = logging.getLogger(__name__)

#: models a role may use; Ops cannot point a role at anything else
ALLOWED_MODELS = {GroqModel.FAST.value, GroqModel.BALANCED.value}
EFFORTS = {"low", "medium", "high"}


@dataclass(frozen=True)
class Role:
    name: str
    model: str
    max_tokens: int
    reasoning_effort: str = "low"
    private: bool = True


DEFAULTS: dict[str, Role] = {
    "decide": Role("decide", GroqModel.FAST.value, 400),
    "plan": Role("plan", GroqModel.BALANCED.value, 1500),
    "respond": Role("respond", GroqModel.FAST.value, 300),
    "explain": Role("explain", GroqModel.BALANCED.value, 800),
}


def config():
    return get_db()["ask_router"]


async def roles() -> dict[str, Role]:
    """The four roles as they stand: the defaults, with any value Ops saved that is still allowed."""
    out = dict(DEFAULTS)
    try:
        doc = await config().find_one({"_id": "roles"}) or {}
    except Exception as exc:  # noqa: BLE001 - the defaults are always safe
        logger.debug("Could not read the Ask router settings: %s", exc)
        return out
    for name, saved in (doc.get("roles") or {}).items():
        base = out.get(name)
        if base is None or not isinstance(saved, dict):
            continue
        model = saved.get("model", base.model)
        effort = saved.get("reasoning_effort", base.reasoning_effort)
        tokens = saved.get("max_tokens", base.max_tokens)
        if model in ALLOWED_MODELS and effort in EFFORTS and isinstance(tokens, int) and 100 <= tokens <= 4000:
            out[name] = replace(base, model=model, reasoning_effort=effort, max_tokens=tokens)
    return out


async def save_roles(changes: dict[str, dict[str, Any]]) -> dict[str, Role]:
    """Saves changes to roles. Raises ValueError, with a plain sentence, for anything not allowed. Returns the roles as they now stand."""
    current = await roles()
    saved: dict[str, dict[str, Any]] = {}
    for name, values in changes.items():
        if name not in DEFAULTS:
            raise ValueError(f"There is no role called {name}.")
        if values.get("model", current[name].model) not in ALLOWED_MODELS:
            raise ValueError("That model is not one Ask Ekavo may use.")
        if values.get("reasoning_effort", current[name].reasoning_effort) not in EFFORTS:
            raise ValueError("Effort must be low, medium or high.")
        tokens = values.get("max_tokens", current[name].max_tokens)
        if not isinstance(tokens, int) or not 100 <= tokens <= 4000:
            raise ValueError("The token budget must be between 100 and 4000.")
        saved[name] = {"model": values.get("model", current[name].model), "reasoning_effort": values.get("reasoning_effort", current[name].reasoning_effort), "max_tokens": tokens}
    await config().update_one({"_id": "roles"}, {"$set": {f"roles.{n}": v for n, v in saved.items()}}, upsert=True)
    return await roles()


async def think(role_name: str, prompt: str, system: str) -> dict[str, Any]:
    """One structured answer from the model this role names. A person's words are private content. Raises the shared "busy" error when no model
    can answer."""
    role = (await roles())[role_name]
    if role.private:
        async with private_content():
            return await call_llm_structured(prompt, system=system, model=GroqModel(role.model), max_tokens=role.max_tokens, reasoning_effort=role.reasoning_effort)
    return await call_llm_structured(prompt, system=system, model=GroqModel(role.model), max_tokens=role.max_tokens, reasoning_effort=role.reasoning_effort)
