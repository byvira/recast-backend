"""The gatekeeper's checks, one at a time and in order, with stand-in services (no database, no credits, no providers)."""

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest
from pydantic import BaseModel, Field

from app.tools.contract import Mode, Risk, Scope, ToolSpec
from app.tools.gatekeeper import (
    CAP_BRAND_IDENTITY, CAP_DELETE, Call, Credential, Estimate, Gatekeeper, Refusal, fingerprint,
)
from app.tools.registry import ToolRegistry

WS, USER = "ws1", "u1"
EDITOR = frozenset({"create_content", "edit_content", "publish_content"})


class _Make(BaseModel):
    topic: str = Field(min_length=1, max_length=50)


class _Del(BaseModel):
    item_id: str
    confirm: bool


class _Publish(BaseModel):
    piece_id: str
    confirm_publish_anyway: bool = False


class _Nothing(BaseModel):
    pass


def _tool(name, risk, scope, model, **over) -> ToolSpec:
    base = dict(name=name, summary="Do a thing.", risk=risk, scope=scope, mode=Mode.INSTANT, input_model=model, cost_note="Free.")
    base.update(over)
    return ToolSpec(**base)


def _registry() -> ToolRegistry:
    r = ToolRegistry()
    r.register(_tool("demo.read", Risk.READ, Scope.READ, _Nothing, cost_note=""))
    r.register(_tool("demo.make", Risk.WRITE, Scope.CREATE, _Make, permission="create_content", cost_key="text_post", limit_keys=("pictures_day",)))
    r.register(_tool("demo.delete", Risk.DESTRUCTIVE, Scope.CREATE, _Del, permission="create_content", undo="Cannot be undone."))
    r.register(_tool("demo.publish", Risk.PUBLISH, Scope.PUBLISH, _Publish, permission="publish_content", approval="person",
                     human_only_fields=("confirm_publish_anyway",)))
    r.register(_tool("demo.approve", Risk.DECISION, Scope.APPROVE, _Nothing, permission="approve_content", human_only=True))
    r.register(_tool("brand.set_logo", Risk.WRITE, Scope.CREATE, _Nothing, permission="create_content"))
    r.register(_tool("demo.off", Risk.READ, Scope.READ, _Nothing, cost_note="", enabled=False))
    return r


class Fake:
    """Stand-in services; each answer can be changed by a test."""

    def __init__(self):
        self.cred: Optional[Credential] = Credential(
            "k1", "api", WS, USER, frozenset({Scope.READ, Scope.CREATE, Scope.PUBLISH}), credit_cap=None,
        )
        self.permissions: Optional[frozenset[str]] = EDITOR
        self.off: Optional[str] = None
        self.in_rate = True
        self.plan: dict[str, str] = {}
        self.credits = 1
        self.exact = True
        self.spent = 0
        self.room_checked: list = []
        self.approved_ok = True
        self.held = False
        self.idem: tuple[str, Optional[dict]] = ("none", None)
        self.ledger: list[dict] = []
        self.calls: list[str] = []

    async def credential(self, credential_id):
        self.calls.append("credential")
        return self.cred

    async def member_permissions(self, workspace_id, user_id):
        self.calls.append("person")
        return self.permissions

    async def switched_off(self, workspace_id, tool):
        self.calls.append("switch")
        return self.off

    async def within_rate(self, credential):
        self.calls.append("rate")
        return self.in_rate

    async def plan_limit_message(self, workspace_id, limit_key):
        self.calls.append("plan")
        return self.plan.get(limit_key)

    async def price(self, credential, tool, arguments):
        self.calls.append("price")
        return Estimate(self.credits if tool.cost_key else 0, self.exact, "", tool.cost_key, 1)

    async def spent_this_month(self, credential_id):
        return self.spent

    async def ensure_room(self, credential, tool, estimate):
        self.calls.append("room")
        self.room_checked.append((tool.name, estimate.credits))

    async def approved(self, credential, tool, arguments, token):
        self.calls.append("approval")
        return self.approved_ok

    async def held_by_safety(self, credential, tool, arguments):
        self.calls.append("safety")
        return self.held

    async def earlier_answer(self, credential, tool, key, fingerprint_):
        self.calls.append("idempotency")
        return self.idem

    async def request_approval(self, credential, tool, arguments):
        self.calls.append("request")
        return "req-1"

    async def record(self, entry):
        self.ledger.append(entry)


@pytest.fixture
def fake():
    return Fake()


@pytest.fixture
def gate(fake):
    return Gatekeeper(_registry(), fake)


async def _code(gate, **call) -> str:
    with pytest.raises(Refusal) as caught:
        await gate.admit(Call(credential_id="k1", **call))
    return caught.value.code


async def test_a_good_call_is_admitted_with_its_typed_input_and_estimate(gate, fake):
    admission = await gate.admit(Call("k1", "demo.make", {"topic": "launch"}, idempotency_key="a"))
    assert admission.arguments.topic == "launch" and admission.estimate.credits == 1
    assert fake.room_checked == [("demo.make", 1)]          # checked, never held: the work holds its own credits


async def test_checks_run_in_the_fixed_order_and_stop_at_the_first_refusal(gate, fake):
    await gate.admit(Call("k1", "demo.make", {"topic": "launch"}, idempotency_key="a"))
    assert fake.calls == ["credential", "person", "switch", "switch", "rate", "plan", "price", "idempotency", "room"]
    fake.calls.clear()
    fake.cred = None
    assert await _code(gate, tool="demo.make", arguments={"topic": "x"}) == "auth_invalid"
    assert fake.calls == ["credential"]


async def test_a_revoked_or_expired_key_is_refused_before_anything_else(gate, fake):
    fake.cred = Credential("k1", "api", WS, USER, frozenset({Scope.READ}), revoked=True)
    assert await _code(gate, tool="demo.read") == "auth_revoked"
    fake.cred = Credential("k1", "api", WS, USER, frozenset({Scope.READ}), expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    assert await _code(gate, tool="demo.read") == "auth_expired"


async def test_a_tool_that_does_not_exist_cannot_be_named(gate):
    assert await _code(gate, tool="demo.invented") == "tool_unknown"


async def test_a_person_who_left_the_workspace_stops_their_key_at_once(gate, fake):
    fake.permissions = None
    assert await _code(gate, tool="demo.read") == "member_inactive"


async def test_switches_stop_a_tool(gate, fake):
    assert await _code(gate, tool="demo.off") == "tool_off"
    fake.off = "Tools are switched off for now."
    assert await _code(gate, tool="demo.read") == "tool_off"


async def test_scope_is_the_keys_and_role_is_the_persons_current_role(gate, fake):
    fake.cred = Credential("k1", "api", WS, USER, frozenset({Scope.READ}))
    assert await _code(gate, tool="demo.make", arguments={"topic": "x"}) == "scope_missing"
    fake.cred = Credential("k1", "api", WS, USER, frozenset({Scope.READ, Scope.CREATE}))
    fake.permissions = frozenset({"approve_content"})                      # demoted to Reviewer since the key was made
    assert await _code(gate, tool="demo.make", arguments={"topic": "x"}) == "role_missing"


async def test_a_decision_needs_the_approve_scope_and_never_comes_from_ask(gate, fake):
    fake.permissions = frozenset({"approve_content"})
    assert await _code(gate, tool="demo.approve") == "scope_missing"
    fake.cred = Credential("k1", "api", WS, USER, frozenset({Scope.APPROVE}))
    assert (await gate.admit(Call("k1", "demo.approve"))).tool.name == "demo.approve"      # an owner named this key for it
    fake.cred = Credential("k1", "ask", WS, USER, frozenset({Scope.APPROVE}))
    assert await _code(gate, tool="demo.approve") == "human_only"


async def test_a_key_cannot_delete_or_change_the_brands_look_unless_the_owner_allows_it(gate, fake):
    assert await _code(gate, tool="demo.delete", arguments={"item_id": "a", "confirm": True}) == "capability_missing"
    assert await _code(gate, tool="brand.set_logo") == "capability_missing"
    fake.cred = Credential("k1", "api", WS, USER, frozenset({Scope.CREATE}), capabilities=frozenset({CAP_DELETE, CAP_BRAND_IDENTITY}))
    assert (await gate.admit(Call("k1", "demo.delete", {"item_id": "a", "confirm": True}))).tool.name == "demo.delete"
    assert (await gate.admit(Call("k1", "brand.set_logo"))).tool.name == "brand.set_logo"
    # a person using the CLI is not a key: the same tools are open to them
    fake.cred = Credential("s1", "cli", WS, USER, frozenset({Scope.CREATE}))
    assert (await gate.admit(Call("s1", "brand.set_logo"))).tool.name == "brand.set_logo"


async def test_the_rate_limit_refuses_with_429(gate, fake):
    fake.in_rate = False
    with pytest.raises(Refusal) as caught:
        await gate.admit(Call("k1", "demo.read"))
    assert caught.value.code == "rate_limited" and caught.value.status == 429


async def test_bad_input_is_refused_with_the_fields_named_and_nothing_is_charged(gate, fake):
    with pytest.raises(Refusal) as caught:
        await gate.admit(Call("k1", "demo.make", {"topic": ""}))
    assert caught.value.code == "input_invalid" and caught.value.extra["problems"][0]["field"] == "topic"
    assert fake.room_checked == []
    fake.cred = Credential("k1", "api", WS, USER, frozenset({Scope.CREATE}), capabilities=frozenset({CAP_DELETE}))
    assert await _code(gate, tool="demo.delete", arguments={"item_id": "a"}) == "input_invalid"      # no confirm


async def test_a_field_only_a_person_may_set_is_refused(gate):
    assert await _code(gate, tool="demo.publish", arguments={"piece_id": "p", "confirm_publish_anyway": True}) == "human_only"


async def test_a_reached_plan_limit_refuses_with_402_and_its_own_words(gate, fake):
    fake.plan = {"pictures_day": "The Free plan includes 5 AI pictures a day. None are left."}
    with pytest.raises(Refusal) as caught:
        await gate.admit(Call("k1", "demo.make", {"topic": "x"}))
    assert caught.value.code == "plan_limit" and caught.value.status == 402 and "5 AI pictures" in caught.value.message


async def test_a_key_stops_at_its_monthly_credit_cap(gate, fake):
    fake.cred = Credential("k1", "api", WS, USER, frozenset({Scope.CREATE}), credit_cap=100)
    fake.spent = 100
    with pytest.raises(Refusal) as caught:
        await gate.admit(Call("k1", "demo.make", {"topic": "x"}))
    assert caught.value.code == "key_cap" and caught.value.extra["left"] == 0
    fake.spent = 98
    fake.credits = 3
    assert await _code(gate, tool="demo.make", arguments={"topic": "x"}) == "key_cap"
    fake.credits = 2
    assert (await gate.admit(Call("k1", "demo.make", {"topic": "x"}))).estimate.credits == 2


async def test_a_run_over_twenty_credits_asks_first_and_the_confirmed_amount_must_match(gate, fake):
    fake.credits = 35
    with pytest.raises(Refusal) as first:
        await gate.admit(Call("k1", "demo.make", {"topic": "x"}))
    assert first.value.code == "confirm_required" and first.value.extra["estimate"] == 35
    with pytest.raises(Refusal) as changed:
        await gate.admit(Call("k1", "demo.make", {"topic": "x"}, confirmed_credits=30))
    assert changed.value.code == "estimate_changed"
    assert (await gate.admit(Call("k1", "demo.make", {"topic": "x"}, confirmed_credits=35))).estimate.credits == 35


async def test_a_dry_run_shows_the_estimate_and_reserves_nothing(gate, fake):
    fake.credits = 35
    admission = await gate.admit(Call("k1", "demo.make", {"topic": "x"}, dry_run=True))
    assert admission.dry_run and admission.estimate.credits == 35 and fake.room_checked == []


async def test_publishing_needs_a_persons_yes_and_a_held_post_is_never_released(gate, fake):
    fake.approved_ok = False
    with pytest.raises(Refusal) as asked:
        await gate.admit(Call("k1", "demo.publish", {"piece_id": "p"}))
    assert asked.value.code == "approval_required" and asked.value.extra == {"approval_request_id": "req-1"}
    fake.approved_ok = True
    assert (await gate.admit(Call("k1", "demo.publish", {"piece_id": "p"}, approval_token="t"))).tool.name == "demo.publish"
    fake.held = True
    assert await _code(gate, tool="demo.publish", arguments={"piece_id": "p"}, approval_token="t") == "safety_held"


async def test_a_repeated_request_is_answered_from_the_first_and_charges_nothing_more(gate, fake):
    fake.idem = ("replay", {"piece_id": "p9"})
    admission = await gate.admit(Call("k1", "demo.make", {"topic": "x"}, idempotency_key="same"))
    assert admission.replay == {"piece_id": "p9"} and fake.room_checked == []
    fake.idem = ("conflict", None)
    assert await _code(gate, tool="demo.make", arguments={"topic": "y"}, idempotency_key="same") == "idempotency_conflict"
    fake.idem = ("in_progress", None)
    assert await _code(gate, tool="demo.make", arguments={"topic": "x"}, idempotency_key="same") == "request_in_progress"


async def test_every_call_reaches_the_ledger_refused_or_admitted_and_it_holds_field_names_never_values(gate, fake):
    await gate.admit_and_record(Call("k1", "demo.make", {"topic": "my secret launch plan"}, idempotency_key="a"))
    with pytest.raises(Refusal):
        await gate.admit_and_record(Call("k1", "demo.make", {"topic": ""}))
    assert [row["outcome"] for row in fake.ledger] == ["admitted", "refused"]
    assert fake.ledger[1]["code"] == "input_invalid" and fake.ledger[0]["fields"] == ["topic"]
    assert "secret" not in str(fake.ledger)


async def test_the_same_call_always_has_the_same_fingerprint():
    assert fingerprint("a.b", {"x": 1, "y": 2}) == fingerprint("a.b", {"y": 2, "x": 1})
    assert fingerprint("a.b", {"x": 1}) != fingerprint("a.c", {"x": 1})


async def test_a_ledger_that_fails_to_write_never_changes_the_answer(gate, fake):
    async def broken(entry: dict[str, Any]) -> None:
        raise RuntimeError("database down")

    fake.record = broken
    assert (await gate.admit_and_record(Call("k1", "demo.read"))).tool.name == "demo.read"
