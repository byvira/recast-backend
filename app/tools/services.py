"""The live answers behind the gatekeeper: who is calling, the person's current role, the switches, the rate limit, the plan limits, the
price, the credit hold, a person's approval, the safety hold and repeated requests. Each one asks the part of the app that already owns it
(credits, entitlements, roles); nothing here is a second copy of those rules."""

from __future__ import annotations

import inspect
from typing import Any, Callable, Optional

from pydantic import BaseModel

from app.billing import costs as credit_costs
from app.billing import credits
from app.core.rbac import ROLE_PERMISSIONS
from app.db.mongo import get_db
from app.shared import entitlements
from app.tools import credentials, ledger
from app.tools.contract import Scope, ToolSpec
from app.tools.gatekeeper import Credential, Estimate, Refusal, fingerprint

DEFAULT_CALLS_PER_MINUTE = 60

#: tool name -> how many units of its price row a call will use, worked out from its typed input and the workspace (minutes of narration, say).
#: It may be async, to look a length up. A tool priced by length that is not listed here is estimated as one unit and marked not exact.
ESTIMATORS: dict[str, Callable[[BaseModel, str], Any]] = {}


def register_estimator(tool: str, units: Callable[[BaseModel, str], Any]) -> None:
    ESTIMATORS[tool] = units


def _message(detail: Any, fallback: str) -> str:
    if isinstance(detail, dict):
        return str(detail.get("message") or fallback)
    return str(detail or fallback)


def _piece_ids(arguments: BaseModel) -> list[str]:
    data = arguments.model_dump(mode="json")
    ids: list[str] = []
    if isinstance(data.get("piece_id"), str):
        ids.append(data["piece_id"])
    if isinstance(data.get("piece_ids"), list):
        ids.extend(str(i) for i in data["piece_ids"])
    return ids


async def any_held_by_safety(workspace_id: str, arguments: BaseModel) -> bool:
    """True when any post named in the input was held back by the safety check."""
    ids = _piece_ids(arguments)
    if not ids:
        return False
    return await get_db()["content_pieces"].count_documents({"id": {"$in": ids}, "workspace_id": workspace_id, "flagged_for_review": True}) > 0


def member_credential_id(workspace_id: str, user_id: str) -> str:
    return f"member:{workspace_id}:{user_id}"


class LiveServices:
    async def credential(self, credential_id: str) -> Optional[Credential]:
        if credential_id.startswith(("link:", "email:")):
            # A one-tap link or a forwarded email, made by code that already checked it belongs to this person. A link may only decide; an email
            # may only make drafts. The person's current role is still read on every call.
            kind, workspace_id, user_id = credential_id.split(":", 2)
            scopes = frozenset({Scope.READ, Scope.APPROVE}) if kind == "link" else frozenset({Scope.READ, Scope.CREATE})
            return Credential(credential_id, kind, workspace_id, user_id, scopes)
        if credential_id.startswith("source:"):
            # A source (a feed or a file that makes drafts) working for the member who set it up. It can read and create, never publish, decide, delete or approve,
            # and the person's current role is still read on every call.
            _, workspace_id, user_id = credential_id.split(":", 2)
            return Credential(credential_id, "source", workspace_id, user_id, frozenset({Scope.READ, Scope.CREATE}))
        if credential_id.startswith("ask:"):
            # Ask Ekavo working for a signed-in member. It reads and creates as the person's own role allows (checked on every call) and can never publish,
            # decide, delete or approve: those are shown to the person, who does them in the app.
            _, workspace_id, user_id = credential_id.split(":", 2)
            return Credential(credential_id, "ask", workspace_id, user_id, frozenset({Scope.READ, Scope.CREATE}))
        if credential_id.startswith("member:"):
            # A signed-in member using the app itself (for example undoing a change). Only routes that already know who the person is make this
            # id; no key or outside caller can, because a key's id comes from the key table. It can read, make and publish as the person's own role
            # allows (checked on every call) and can never approve.
            _, workspace_id, user_id = credential_id.split(":", 2)
            return Credential(credential_id, "app", workspace_id, user_id, frozenset({Scope.READ, Scope.CREATE, Scope.PUBLISH}))
        return await credentials.load(credential_id)

    async def member_permissions(self, workspace_id: str, user_id: Optional[str]) -> Optional[frozenset[str]]:
        """The person's rights as they are now: an active member of a workspace that still exists, with the role they hold today."""
        if not user_id:
            return None
        db = get_db()
        member = await db["workspace_members"].find_one({"workspace_id": workspace_id, "user_id": user_id}, {"role": 1, "status": 1})
        if not member or member.get("status", "active") != "active":
            return None
        if not await db["workspaces"].find_one({"id": workspace_id, "deleted_at": {"$exists": False}}, {"_id": 1}):
            return None
        return frozenset(ROLE_PERMISSIONS.get(member.get("role", ""), set()))

    async def switched_off(self, workspace_id: str, tool: str) -> Optional[str]:
        rows = await ledger.settings().find({"$or": [
            {"scope": "global"}, {"scope": "tool", "tool": tool}, {"scope": "workspace", "workspace_id": workspace_id},
        ]}).to_list(length=10)
        for row in rows:
            if row.get("scope") == "global" and row.get("off"):
                return str(row.get("message") or "Tools are switched off for now. Try again later.")
            if row.get("scope") == "tool" and row.get("off"):
                return str(row.get("message") or "That tool is switched off for now.")
            if row.get("scope") == "workspace" and (row.get("all_off") or tool in (row.get("off_tools") or [])):
                return "This workspace has switched that off for connected apps."
        return None

    async def within_rate(self, credential: Credential) -> bool:
        ent = await entitlements.entitlements_for(credential.workspace_id)
        limit = int(ent.limits.get("api_calls_per_minute") or DEFAULT_CALLS_PER_MINUTE)
        return await ledger.hit(credential.id) <= limit

    async def plan_limit_message(self, workspace_id: str, limit_key: str) -> Optional[str]:
        """Plain words when this limit is reached, else None. While plans are off the limits are shown but never block, as in the app."""
        ent = await entitlements.entitlements_for(workspace_id)
        if not ent.enforced or ent.limits.get(limit_key) is None:
            return None
        used = await entitlements.count_usage(ent, limit_key)
        if entitlements.has_room(ent, limit_key, used):
            return None
        return entitlements.limit_message(ent, limit_key, used)

    async def _price_steps(self, credential: Credential, arguments: BaseModel) -> Estimate:
        """A compound request costs what its steps cost. Each step is priced as its own tool; a step that depends on an earlier result, or whose
        price depends on length, is priced at its base amount, so the total is a floor and the answer says so."""
        from app.tools.registry import get_registry

        registry = get_registry()
        table = await credit_costs.costs()
        total = 0
        priced = 0
        unsure = False
        for step in arguments.steps:  # type: ignore[attr-defined]
            spec = registry.get(step.tool)
            if spec is None or not spec.cost_key:
                continue
            row = table.get(spec.cost_key) or {}
            total += credit_costs.price_with(table, spec.cost_key, 1.0)
            priced += 1
            unsure = unsure or bool(row.get("per")) or "${" in str(step.arguments)
        note = f"{priced} of {len(arguments.steps)} steps cost credits." if priced else "No step costs credits."  # type: ignore[attr-defined]
        if unsure:
            note += " Steps priced by length or using an earlier result are counted at their base price, so the total can be higher."
        return Estimate(total, exact=not unsure, note=note)

    async def price(self, credential: Credential, tool: ToolSpec, arguments: BaseModel) -> Estimate:
        if tool.name == "compose.run":
            return await self._price_steps(credential, arguments)
        if not tool.cost_key:
            return Estimate(0, True, tool.cost_note, None, 0)
        from app.tools import estimators  # noqa: F401  (registers the estimators)

        table = await credit_costs.costs()
        row = table[tool.cost_key]
        estimator = ESTIMATORS.get(tool.name)
        units = 1.0
        if estimator:
            found = estimator(arguments, credential.workspace_id)
            units = float(await found if inspect.isawaitable(found) else found)
        by_length = bool(row.get("per"))
        return Estimate(credit_costs.price_with(table, tool.cost_key, units), exact=(not by_length) or estimator is not None,
                        note=tool.cost_note, cost_key=tool.cost_key, units=units)

    async def ai_cooldown_left(self, kind: str = "writing") -> int:
        from app.core import failures

        return await failures.ai_cooldown_left(kind)

    async def spent_this_month(self, credential_id: str) -> int:
        return await ledger.spent_this_month(credential_id)

    async def ensure_room(self, credential: Credential, tool: ToolSpec, estimate: Estimate) -> None:
        """Refuse when the credits would not fit, without setting any aside: the action or route the tool runs reserves and charges its own."""
        try:
            await credits.require_room(credential.workspace_id, estimate.credits, credential.user_id)
        except credits.CreditsExhausted as exc:
            raise Refusal("credits_short", _message(exc.detail, "There are not enough credits for that."), status=402)

    async def approved(self, credential: Credential, tool: ToolSpec, arguments: BaseModel, token: Optional[str]) -> bool:
        return await ledger.use_approval(credential.workspace_id, tool.name, fingerprint(tool.name, arguments.model_dump(mode="json")), token)

    async def held_by_safety(self, credential: Credential, tool: ToolSpec, arguments: BaseModel) -> bool:
        """A post the safety check held back is never released from outside, whatever the credential."""
        return await any_held_by_safety(credential.workspace_id, arguments)

    async def request_approval(self, credential: Credential, tool: ToolSpec, arguments: BaseModel) -> Optional[str]:
        from app.tools import approvals

        return await approvals.open_request(credential, tool, arguments)

    async def earlier_answer(self, credential: Credential, tool: str, key: str, fingerprint_: str) -> tuple[str, Optional[dict[str, Any]]]:
        return await ledger.earlier_answer(credential.id, tool, key, fingerprint_)

    async def record(self, entry: dict[str, Any]) -> Optional[str]:
        return await ledger.record(entry)
