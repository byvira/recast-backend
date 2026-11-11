"""The gatekeeper: plain code, no AI, that every tool call passes before anything is done.

A call from a key, the CLI, an assistant or Ask Ekavo is checked in a fixed order. The first check that fails refuses the call with a stable
code and a plain message; nothing after it runs. A call that passes every check is admitted, with its typed input, its credit estimate and
the credits set aside. Doing the work is not here: the gatekeeper only decides.

The order: sign-in (not revoked or expired), the person (still an active member), the switches (all off, workspace off, tool off), scope
and role (the tool's scope intersected with the person's current role, so a demotion acts at once), the key's capabilities (delete, brand
identity), the rate limit, typed validation, the plan limits, the credit estimate and the key's monthly cap, approval and safety, and
last, idempotency. Credits are only checked here (they must fit); the work the tool runs sets them aside and charges them itself, so a
call is never held twice. Every outcome, allowed or refused, is written to the ledger by the caller of `admit`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Protocol

from pydantic import BaseModel, ValidationError

from app.tools.contract import Risk, Scope, ToolSpec
from app.tools.registry import ToolRegistry

#: a run that would cost more than this many credits asks first
CONFIRM_ABOVE = 20

#: tools that change the brand's identity: open to people, off for keys until the owner switches on the "brand identity" capability
BRAND_IDENTITY_TOOLS = frozenset({
    "brand.set_logo", "brand.set_watermark", "brand.make_watermark_mark", "brand.set_visual_identity", "brand.regenerate_mascot",
})
CAP_DELETE = "delete"
CAP_BRAND_IDENTITY = "brand_identity"


class Refusal(Exception):
    """A call that was stopped, with a code that never changes and a message in plain words."""

    def __init__(self, code: str, message: str, *, status: int = 403, extra: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.code, self.message, self.status, self.extra = code, message, status, extra or {}

    def body(self) -> dict[str, Any]:
        from app.core import failures

        return {"code": self.code, "message": self.message, **failures.for_refusal(self.code), **self.extra}


@dataclass(frozen=True)
class Credential:
    """What the gatekeeper knows about whoever is calling, read fresh on every call."""
    id: str
    surface: str                      # "api", "cli", "mcp" or "ask"
    workspace_id: str
    user_id: Optional[str]            # the member it acts as (a key acts as the member who made it)
    scopes: frozenset[Scope]
    capabilities: frozenset[str] = frozenset()
    credit_cap: Optional[int] = None  # a key's credits a month; None means no key cap
    revoked: bool = False
    expires_at: Optional[datetime] = None


@dataclass(frozen=True)
class Estimate:
    credits: int
    #: False when the price depends on what is made (minutes of narration, say) and the number is the cost of one unit
    exact: bool = True
    note: str = ""
    #: the price row and the amount of it the credits were worked out from
    cost_key: Optional[str] = None
    units: float = 1.0


@dataclass(frozen=True)
class Call:
    credential_id: str
    tool: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    idempotency_key: Optional[str] = None
    #: the amount the person saw and agreed to; required above CONFIRM_ABOVE
    confirmed_credits: Optional[int] = None
    #: a person's answer to "do you want this to go out?", for publish tools
    approval_token: Optional[str] = None
    dry_run: bool = False


@dataclass(frozen=True)
class Admission:
    tool: ToolSpec
    credential: Credential
    arguments: BaseModel
    estimate: Estimate
    #: an earlier answer to the same call (same idempotency key and same input); nothing is done or charged again
    replay: Optional[dict[str, Any]] = None
    dry_run: bool = False
    #: the ledger row for this call, so how it ended can be written on it
    call_id: Optional[str] = None


class Services(Protocol):
    """Everything the gatekeeper has to ask the rest of the app. Kept apart so the checks can be tested without a database."""

    async def credential(self, credential_id: str) -> Optional[Credential]: ...
    async def member_permissions(self, workspace_id: str, user_id: Optional[str]) -> Optional[frozenset[str]]: ...
    async def switched_off(self, workspace_id: str, tool: str) -> Optional[str]: ...
    async def within_rate(self, credential: Credential) -> bool: ...
    async def plan_limit_message(self, workspace_id: str, limit_key: str) -> Optional[str]: ...
    async def price(self, credential: Credential, tool: ToolSpec, arguments: BaseModel) -> Estimate: ...
    async def spent_this_month(self, credential_id: str) -> int: ...
    async def ensure_room(self, credential: Credential, tool: ToolSpec, estimate: Estimate) -> None:
        """Refuse when the credits would not fit. Nothing is set aside: the work the tool runs sets aside and charges its own credits."""
    async def approved(self, credential: Credential, tool: ToolSpec, arguments: BaseModel, token: Optional[str]) -> bool: ...
    async def held_by_safety(self, credential: Credential, tool: ToolSpec, arguments: BaseModel) -> bool: ...
    async def earlier_answer(self, credential: Credential, tool: str, key: str, fingerprint: str) -> tuple[str, Optional[dict[str, Any]]]:
        """("none", None) when the key is new (and now claimed), ("replay", answer), ("in_progress", None) or ("conflict", None)."""
    async def request_approval(self, credential: Credential, tool: ToolSpec, arguments: BaseModel) -> Optional[str]:
        """Put the call in front of a person who may answer it, and return the request's id. The same call asked again finds the same request."""
    async def record(self, entry: dict[str, Any]) -> Optional[str]: ...


def fingerprint(tool: str, arguments: Mapping[str, Any]) -> str:
    """The same call always gives the same fingerprint, whatever order its fields were sent in."""
    return hashlib.sha256(json.dumps([tool, arguments], sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def visible_tools(registry: ToolRegistry, credential: Credential, permissions: frozenset[str]) -> list[ToolSpec]:
    """The tools this credential could use today: its scopes meet the person's current role, a key's capabilities are counted, and a decision
    is never offered to Ask."""
    out = []
    for tool in registry.visible_to(credential.scopes, permissions):
        if tool.human_only and credential.surface == "ask":
            continue
        if credential.surface == "api":
            if tool.risk is Risk.DESTRUCTIVE and CAP_DELETE not in credential.capabilities:
                continue
            if tool.name in BRAND_IDENTITY_TOOLS and CAP_BRAND_IDENTITY not in credential.capabilities:
                continue
        out.append(tool)
    return out


class Gatekeeper:
    def __init__(self, registry: ToolRegistry, services: Services):
        self.registry, self.services = registry, services

    async def admit_and_record(self, call: Call) -> Admission:
        """`admit`, and either way a row in the ledger: a refusal says why, an admission says what it was admitted for."""
        started = _now()
        try:
            admission = await self.admit(call)
        except Refusal as refusal:
            await self._write(call, started, outcome="refused", code=refusal.code, credential=getattr(refusal, "credential", None))
            raise
        call_id = await self._write(call, started, outcome="replayed" if admission.replay is not None else "dry_run" if admission.dry_run else "admitted",
                                    admission=admission)
        return replace(admission, call_id=call_id)

    async def _write(self, call: Call, started: datetime, *, outcome: str, code: Optional[str] = None, admission: Optional[Admission] = None,
                     credential: Optional[Credential] = None) -> Optional[str]:
        credential = admission.credential if admission else credential
        tool = admission.tool if admission else self.registry.get(call.tool)
        try:
            return await self.services.record({
                "at": started, "credential_id": call.credential_id, "workspace_id": credential.workspace_id if credential else None,
                "surface": credential.surface if credential else None, "user_id": credential.user_id if credential else None,
                "tool": call.tool, "risk": tool.risk.value if tool else None, "outcome": outcome, "code": code,
                "estimate_credits": admission.estimate.credits if admission else None, "estimate_exact": admission.estimate.exact if admission else None,
                "dry_run": call.dry_run, "idempotency_key": call.idempotency_key, "fingerprint": fingerprint(call.tool, call.arguments),
                # which fields were sent, never what they held: private text does not belong in a log
                "fields": sorted(call.arguments.keys()),
                "duration_ms": int((_now() - started).total_seconds() * 1000),
            })
        except Exception:  # noqa: BLE001 - a failed log write never changes the answer the caller gets
            import logging

            logging.getLogger(__name__).exception("tool call not written to the ledger")
            return None

    async def admit(self, call: Call) -> Admission:
        credential = await self._sign_in(call)
        try:
            return await self._admit(call, credential)
        except Refusal as refusal:
            refusal.credential = credential          # who was refused, so the ledger and Ops can say whose call it was
            raise

    async def _admit(self, call: Call, credential: Credential) -> Admission:
        tool = self._tool(call)
        permissions = await self._person(credential)
        await self._switches(credential, tool)
        self._scope_and_role(credential, tool, permissions)
        self._capabilities(credential, tool)
        await self._rate(credential)
        await self._ai_cooldown(tool)
        arguments = self._validate(tool, call)
        await self._plan_limits(credential, tool)
        estimate = await self._estimate(credential, tool, arguments, call)
        await self._approval_and_safety(credential, tool, arguments, call)
        replay = await self._idempotency(credential, tool, call)
        if replay is not None:
            return Admission(tool, credential, arguments, estimate, replay=replay, dry_run=call.dry_run)
        if call.dry_run:
            return Admission(tool, credential, arguments, estimate, dry_run=True)
        if estimate.credits > 0:
            await self.services.ensure_room(credential, tool, estimate)
        return Admission(tool, credential, arguments, estimate)

    # 1. sign-in
    async def _sign_in(self, call: Call) -> Credential:
        credential = await self.services.credential(call.credential_id)
        if credential is None:
            raise Refusal("auth_invalid", "That sign-in is not valid. Sign in again.", status=401)
        if credential.revoked:
            raise Refusal("auth_revoked", "That key or session was switched off. Make a new one in Ekavo.", status=401)
        if credential.expires_at is not None and credential.expires_at <= _now():
            raise Refusal("auth_expired", "That key or session has expired. Make a new one in Ekavo.", status=401)
        return credential

    def _tool(self, call: Call) -> ToolSpec:
        tool = self.registry.get(call.tool)
        if tool is None:
            raise Refusal("tool_unknown", f"There is no tool called {call.tool!r}. Ask for the tool list to see what is available.", status=404)
        return tool

    # 2. the person
    async def _person(self, credential: Credential) -> frozenset[str]:
        permissions = await self.services.member_permissions(credential.workspace_id, credential.user_id)
        if permissions is None:
            raise Refusal("member_inactive", "The person behind this sign-in is no longer an active member of the workspace.", status=401)
        return permissions

    # 3. switches
    async def _switches(self, credential: Credential, tool: ToolSpec) -> None:
        if not tool.enabled:
            raise Refusal("tool_off", "That tool is switched off.", status=403)
        reason = await self.services.switched_off(credential.workspace_id, tool.name)
        if reason:
            raise Refusal("tool_off", reason, status=403)

    # 4. scope and role
    def _scope_and_role(self, credential: Credential, tool: ToolSpec, permissions: frozenset[str]) -> None:
        if tool.scope not in credential.scopes:
            raise Refusal("scope_missing", f"This sign-in is not allowed to {tool.scope.value}. Give it that permission in Ekavo first.")
        if tool.human_only and credential.surface == "ask":
            raise Refusal("human_only", "That is a decision for a person. Do it yourself in Ekavo.")
        if tool.permission is not None and tool.permission not in permissions:
            raise Refusal("role_missing", "The person this acts for does not have the right to do that in this workspace.")

    # 5. what a key may do beyond its scope
    def _capabilities(self, credential: Credential, tool: ToolSpec) -> None:
        if credential.surface != "api":
            return
        if tool.risk is Risk.DESTRUCTIVE and CAP_DELETE not in credential.capabilities:
            raise Refusal("capability_missing", "Keys cannot delete unless the owner switches that on for this key.")
        if tool.name in BRAND_IDENTITY_TOOLS and CAP_BRAND_IDENTITY not in credential.capabilities:
            raise Refusal("capability_missing", "Keys cannot change the brand's look unless the owner switches that on for this key.")

    # 6. rate
    async def _rate(self, credential: Credential) -> None:
        if not await self.services.within_rate(credential):
            raise Refusal("rate_limited", "Too many requests. Wait a moment and try again.", status=429)

    # 6b. a writing service that keeps failing: new calls wait a minute instead of piling on
    async def _ai_cooldown(self, tool: ToolSpec) -> None:
        from app.core import failures
        from app.tools.executor import service_of

        kind = service_of(tool)
        if kind is None:
            return
        check = getattr(self.services, "ai_cooldown_left", None)
        left = await check(kind) if check else 0
        if left > 0:
            code, words = failures.SERVICES[kind]
            raise Refusal(code, f"{words} So this was not started and nothing was charged. Try again shortly.", status=503, extra={"retry_after_seconds": left})

    # 7. typed validation
    def _validate(self, tool: ToolSpec, call: Call) -> BaseModel:
        for name in tool.human_only_fields:
            if call.arguments.get(name):
                raise Refusal("human_only", f"{name} can only be set by a person in Ekavo.")
        try:
            return tool.input_model.model_validate(dict(call.arguments))
        except ValidationError as exc:
            problems = [{"field": ".".join(str(p) for p in e["loc"]), "problem": e["msg"]} for e in exc.errors()][:10]
            raise Refusal("input_invalid", "Some of what was sent is not valid.", status=422, extra={"problems": problems})

    # 8. plan limits
    async def _plan_limits(self, credential: Credential, tool: ToolSpec) -> None:
        for key in tool.limit_keys:
            message = await self.services.plan_limit_message(credential.workspace_id, key)
            if message:
                raise Refusal("plan_limit", message, status=402)

    # 9. credits
    async def _estimate(self, credential: Credential, tool: ToolSpec, arguments: BaseModel, call: Call) -> Estimate:
        estimate = await self.services.price(credential, tool, arguments)
        if credential.credit_cap is not None and estimate.credits > 0:
            spent = await self.services.spent_this_month(credential.id)
            if spent + estimate.credits > credential.credit_cap:
                raise Refusal("key_cap", f"This key has used {spent} of its {credential.credit_cap} credits this month.", status=402,
                              extra={"estimate": estimate.credits, "left": max(0, credential.credit_cap - spent)})
        if estimate.credits > CONFIRM_ABOVE and not call.dry_run:
            if call.confirmed_credits is None:
                raise Refusal("confirm_required", f"This would cost about {estimate.credits} credits. Confirm that amount to go ahead.", status=409,
                              extra={"estimate": estimate.credits})
            if call.confirmed_credits != estimate.credits:
                raise Refusal("estimate_changed", f"The cost is now {estimate.credits} credits, not {call.confirmed_credits}. Confirm the new amount.",
                              status=409, extra={"estimate": estimate.credits})
        return estimate

    # 10. approval and safety
    async def _approval_and_safety(self, credential: Credential, tool: ToolSpec, arguments: BaseModel, call: Call) -> None:
        if tool.risk is Risk.PUBLISH:
            if await self.services.held_by_safety(credential, tool, arguments):
                raise Refusal("safety_held", "The safety check is holding this back. A person has to review it in Ekavo.", status=409)
            if tool.approval == "person" and not call.dry_run and not await self.services.approved(credential, tool, arguments, call.approval_token):
                request_id = await self.services.request_approval(credential, tool, arguments)
                raise Refusal("approval_required", "A person has to approve this before it goes out. It is waiting for them in Ekavo.", status=409,
                              extra={"approval_request_id": request_id} if request_id else None)

    # 11. idempotency
    async def _idempotency(self, credential: Credential, tool: ToolSpec, call: Call) -> Optional[dict[str, Any]]:
        if not call.idempotency_key or call.dry_run:
            return None
        state, stored = await self.services.earlier_answer(credential, tool.name, call.idempotency_key, fingerprint(tool.name, call.arguments))
        if state == "conflict":
            raise Refusal("idempotency_conflict", "That idempotency key was already used for a different request.", status=409)
        if state == "in_progress":
            raise Refusal("request_in_progress", "The same request is still running. Wait for it to finish, then ask again.", status=409)
        return stored if state == "replay" else None
