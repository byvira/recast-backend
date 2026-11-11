"""Keys for the tools, the ledger and the live services behind the gatekeeper, against the test database (no credits, no providers)."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.db.mongo import get_db
from app.tools import credentials, ledger, services
from app.tools.contract import Scope
from app.tools.gatekeeper import Call, Gatekeeper, Refusal, fingerprint
from app.tools.registry import get_registry
from app.video import api_keys


@pytest.fixture
async def world():
    ws, user = uuid4().hex, uuid4().hex
    db = get_db()
    await db["workspaces"].insert_one({"id": ws, "name": "Tools", "owner_id": user})
    await db["workspace_members"].insert_one({"workspace_id": ws, "user_id": user, "role": "editor", "status": "active"})
    await ledger.create_indexes()
    await ledger.settings().delete_many({})                     # switches are shared by the whole database; each test starts with none
    return ws, user


async def _gate() -> Gatekeeper:
    return Gatekeeper(get_registry(), services.LiveServices())


# ── keys ─────────────────────────────────────────────────────────────────

async def test_a_tool_key_is_shown_once_stored_hashed_and_expires_in_ninety_days(world):
    ws, user = world
    record, secret = await credentials.create_key(ws, user, "Zapier", ["read", "create"])
    stored = await get_db()["api_keys"].find_one({"id": record["id"]})
    assert secret.startswith("ek_live_") and stored["hash"] != secret and secret not in str(stored)
    assert stored["tool_scopes"] == ["read", "create"] and stored["credit_cap"] == credentials.DEFAULT_CREDIT_CAP
    days = (stored["expires_at"].replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).days
    assert 88 <= days <= 90
    assert "secret" not in record and "hash" not in record


async def test_a_key_can_never_be_admin_and_approving_needs_the_owner_to_name_it(world):
    ws, user = world
    with pytest.raises(credentials.CredentialError):
        await credentials.create_key(ws, user, "Bad", ["admin"])
    with pytest.raises(credentials.CredentialError):
        await credentials.create_key(ws, user, "Bad", ["read", "approve"])
    record, _ = await credentials.create_key(ws, user, "Named for approvals", ["read", "approve"], allow_approve=True)
    assert "approve" in record["tool_scopes"]


async def test_a_key_needs_a_name_known_scopes_a_sane_cap_and_a_sane_life(world):
    ws, user = world
    for bad in (dict(name="  ", scopes=["read"]), dict(name="x", scopes=[]), dict(name="x", scopes=["everything"]),
                dict(name="x", scopes=["read"], capabilities=["root"]), dict(name="x", scopes=["read"], credit_cap=0),
                dict(name="x", scopes=["read"], days=0), dict(name="x", scopes=["read"], days=4000)):
        with pytest.raises(credentials.CredentialError):
            await credentials.create_key(ws, user, bad.pop("name"), bad.pop("scopes"), **bad)


async def test_a_key_made_before_tools_cannot_call_a_tool(world):
    ws, user = world
    record, _ = await api_keys.create(ws, user, "Old video key", ["video:read"])
    credential = await credentials.load(record["id"])
    assert credential.scopes == frozenset()


async def test_an_expired_or_revoked_key_stops_working_at_once(world):
    ws, user = world
    record, secret = await credentials.create_key(ws, user, "Zapier", ["read"])
    assert await credentials.credential_for_secret(secret) == record["id"]
    await get_db()["api_keys"].update_one({"id": record["id"]}, {"$set": {"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)}})
    assert await credentials.credential_for_secret(secret) is None
    other, other_secret = await credentials.create_key(ws, user, "Second", ["read"])
    await api_keys.revoke(ws, other["id"])
    assert await credentials.credential_for_secret(other_secret) is None
    assert (await credentials.load(other["id"])).revoked is True


# ── the live gatekeeper ──────────────────────────────────────────────────

async def test_a_read_call_goes_through_and_is_written_to_the_ledger(world):
    ws, user = world
    record, _ = await credentials.create_key(ws, user, "Zapier", ["read"])
    admission = await (await _gate()).admit_and_record(Call(record["id"], "credits.summary"))
    assert admission.estimate.credits == 0
    row = await ledger.calls().find_one({"credential_id": record["id"]})
    assert row["outcome"] == "admitted" and row["tool"] == "credits.summary" and row["surface"] == "api" and row["workspace_id"] == ws


async def test_removing_or_demoting_the_person_acts_on_the_next_call(world):
    ws, user = world
    record, _ = await credentials.create_key(ws, user, "Zapier", ["read", "create"])
    gate = await _gate()
    await gate.admit(Call(record["id"], "runs.retry", {"run_id": "r1"}, dry_run=True))
    await get_db()["workspace_members"].update_one({"workspace_id": ws, "user_id": user}, {"$set": {"role": "viewer"}})
    with pytest.raises(Refusal) as demoted:
        await gate.admit(Call(record["id"], "runs.retry", {"run_id": "r1"}, dry_run=True))
    assert demoted.value.code == "role_missing"
    await get_db()["workspace_members"].update_one({"workspace_id": ws, "user_id": user}, {"$set": {"status": "removed"}})
    with pytest.raises(Refusal) as removed:
        await gate.admit(Call(record["id"], "credits.summary"))
    assert removed.value.code == "member_inactive"


async def test_the_ops_switches_stop_all_tools_one_tool_or_a_workspace(world):
    ws, user = world
    record, _ = await credentials.create_key(ws, user, "Zapier", ["read"])
    gate = await _gate()
    await ledger.settings().insert_one({"scope": "tool", "tool": "credits.summary", "off": True})
    with pytest.raises(Refusal) as one:
        await gate.admit(Call(record["id"], "credits.summary"))
    assert one.value.code == "tool_off"
    assert (await gate.admit(Call(record["id"], "plan.limits"))).tool.name == "plan.limits"
    await ledger.settings().insert_one({"scope": "global", "off": True})
    with pytest.raises(Refusal) as everything:
        await gate.admit(Call(record["id"], "plan.limits"))
    assert everything.value.code == "tool_off"


async def test_the_rate_limit_is_per_key_and_refuses_with_429(world, monkeypatch):
    ws, user = world
    monkeypatch.setattr(services, "DEFAULT_CALLS_PER_MINUTE", 2)
    one, _ = await credentials.create_key(ws, user, "One", ["read"])
    two, _ = await credentials.create_key(ws, user, "Two", ["read"])
    live = services.LiveServices()
    ent_limit = (await __import__("app.shared.entitlements", fromlist=["x"]).entitlements_for(ws)).limits.get("api_calls_per_minute")
    limit = int(ent_limit or 2)
    gate = await _gate()
    for _ in range(limit):
        await gate.admit(Call(one["id"], "plan.limits"))
    with pytest.raises(Refusal) as over:
        await gate.admit(Call(one["id"], "plan.limits"))
    assert over.value.status == 429
    assert (await gate.admit(Call(two["id"], "plan.limits"))).tool.name == "plan.limits"
    assert live is not None


async def test_a_held_post_is_never_published_from_outside_and_a_yes_works_once(world):
    ws, user = world
    record, _ = await credentials.create_key(ws, user, "Zapier", ["read", "create", "publish"])
    gate = await _gate()
    ok, held = uuid4().hex, uuid4().hex
    await get_db()["content_pieces"].insert_many([
        {"id": ok, "piece_id": ok, "workspace_id": ws, "flagged_for_review": False},
        {"id": held, "piece_id": held, "workspace_id": ws, "flagged_for_review": True},
    ])
    with pytest.raises(Refusal) as no_yes:
        await gate.admit(Call(record["id"], "publish.now", {"piece_id": ok}))
    assert no_yes.value.code == "approval_required"

    arguments = get_registry().get("publish.now").input_model.model_validate({"piece_id": ok})
    token = await ledger.issue_approval(ws, user, "publish.now", fingerprint("publish.now", arguments.model_dump(mode="json")))
    assert (await gate.admit(Call(record["id"], "publish.now", {"piece_id": ok}, approval_token=token))).tool.name == "publish.now"
    with pytest.raises(Refusal) as used:
        await gate.admit(Call(record["id"], "publish.now", {"piece_id": ok}, approval_token=token))
    assert used.value.code == "approval_required"                                  # a yes is used once

    with pytest.raises(Refusal) as safety:
        await gate.admit(Call(record["id"], "publish.now", {"piece_id": held}, approval_token="anything"))
    assert safety.value.code == "safety_held"


async def test_a_yes_for_one_post_does_not_work_for_another_or_after_it_expires(world):
    ws, user = world
    token = await ledger.issue_approval(ws, user, "publish.now", "fp-a")
    assert await ledger.use_approval(ws, "publish.now", "fp-b", token) is False
    assert await ledger.use_approval("another-workspace", "publish.now", "fp-a", token) is False
    await get_db()["tool_approvals"].update_one({"id": token}, {"$set": {"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)}})
    assert await ledger.use_approval(ws, "publish.now", "fp-a", token) is False


# ── repeated requests, the ledger and the month's spend ──────────────────

async def test_a_repeated_request_is_claimed_once_answered_the_same_and_released_when_it_fails(world):
    assert await ledger.earlier_answer("k", "text.generate", "a", "fp") == ("none", None)
    assert await ledger.earlier_answer("k", "text.generate", "a", "fp") == ("in_progress", None)
    assert (await ledger.earlier_answer("k", "text.generate", "a", "other"))[0] == "conflict"
    await ledger.complete("k", "text.generate", "a", {"piece_ids": ["p1"]})
    assert await ledger.earlier_answer("k", "text.generate", "a", "fp") == ("replay", {"piece_ids": ["p1"]})
    assert await ledger.earlier_answer("k", "text.generate", "b", "fp") == ("none", None)
    await ledger.abandon("k", "text.generate", "b")
    assert await ledger.earlier_answer("k", "text.generate", "b", "fp") == ("none", None)      # a failed call can be tried again


async def test_the_months_spend_counts_what_calls_really_cost_and_what_admitted_calls_hold(world):
    ws, user = world
    now = datetime.now(timezone.utc)
    await ledger.record({"credential_id": "k9", "outcome": "done", "charged_credits": 7, "estimate_credits": 9, "at": now})
    await ledger.record({"credential_id": "k9", "outcome": "admitted", "estimate_credits": 5, "at": now})
    await ledger.record({"credential_id": "k9", "outcome": "refused", "estimate_credits": 50, "at": now})
    await ledger.record({"credential_id": "k9", "outcome": "done", "charged_credits": 40, "at": now - timedelta(days=40)})
    await ledger.record({"credential_id": "other", "outcome": "done", "charged_credits": 99, "at": now})
    assert await ledger.spent_this_month("k9") == 12


async def test_finishing_a_call_records_what_it_really_cost(world):
    call_id = await ledger.record({"credential_id": "k8", "outcome": "admitted", "estimate_credits": 9})
    await ledger.finish(call_id, outcome="done", charged_credits=4)
    assert (await ledger.calls().find_one({"id": call_id}))["charged_credits"] == 4 and await ledger.spent_this_month("k8") == 4


async def test_the_ledger_and_the_other_records_clean_themselves_up(world):
    info = await ledger.calls().index_information()
    assert any(v.get("expireAfterSeconds") == ledger.LEDGER_DAYS * 24 * 3600 for v in info.values())
    assert any("expireAfterSeconds" in v for v in (await ledger.idempotency().index_information()).values())
    assert any("expireAfterSeconds" in v for v in (await ledger.rates().index_information()).values())


async def test_the_price_of_a_tool_comes_from_the_credit_table_and_says_when_it_is_only_per_unit(world):
    live = services.LiveServices()
    registry = get_registry()
    flat = registry.get("text.refine")
    cred = await credentials.load((await credentials.create_key(*world, 'Price', ['read'])) [0]['id'])
    assert (await live.price(cred, flat, flat.input_model.model_construct())).exact is True
    free = registry.get("credits.summary")
    assert (await live.price(cred, free, free.input_model.model_construct())).credits == 0
    by_length = registry.get("videovoice.narrate")
    services.ESTIMATORS.pop('videovoice.narrate', None)
    estimate = await live.price(cred, by_length, by_length.input_model.model_construct())
    assert estimate.exact is False and estimate.cost_key == "narration"
    services.register_estimator("videovoice.narrate", lambda args, ws: 2.5)
    try:
        estimate = await live.price(cred, by_length, by_length.input_model.model_construct())
        assert estimate.exact is True and estimate.units == 2.5 and estimate.credits == 8      # 3 a minute, rounded up
    finally:
        services.ESTIMATORS.pop("videovoice.narrate", None)


async def test_length_priced_tools_are_estimated_from_the_real_length_so_a_dry_run_equals_the_charge(world):
    from datetime import datetime, timezone

    from app.tools import estimators

    estimators.register_all()
    ws, user = world
    record, _ = await credentials.create_key(ws, user, "Estimator", ["read", "create"])
    cred = await credentials.load(record["id"])
    live, registry = services.LiveServices(), get_registry()
    db = get_db()

    await db["media_assets"].insert_one({"id": "m-long", "workspace_id": ws, "duration_s": 600.0, "kind": "audio"})
    await db["audio_assets"].insert_one({"id": "a-long", "workspace_id": ws, "media_id": "m-long"})
    await db["video_projects"].insert_one({"id": "p-long", "workspace_id": ws, "edit": {"duration_f": 30 * 150, "fps": {"num": 30, "den": 1}}, "version": 1,
                                           "updated_at": datetime.now(timezone.utc)})

    def estimate(tool, **arguments):
        spec = registry.get(tool)
        return live.price(cred, spec, spec.input_model.model_construct(**arguments))

    cleanup = await estimate("audio.cleanup", audio_asset_id="a-long")
    assert cleanup.exact and cleanup.units == 10.0 and cleanup.credits == 10                      # 10 minutes at 1 a minute
    video = await estimate("video.render", project_id="p-long")
    assert video.exact and video.units == 2.5 and video.credits == 13                              # 2.5 minutes at 5, rounded up
    analysis = await estimate("videoanalysis.run", media_id="m-long")
    assert analysis.exact and analysis.credits == 1                                                  # 1 credit for each 10 minutes
    narration = await estimate("audio.generate", script=" ".join(["word"] * 300), words_per_minute=150)
    assert narration.exact and narration.units == 2.0 and narration.credits == 6                   # 300 words at 150 a minute is 2 minutes at 3
    unknown = await estimate("audio.cleanup", audio_asset_id="missing")
    assert unknown.units == 1.0                                                                      # a one-minute floor when the length is not known
    assert (await estimate("audio.dialogue")).exact is False                                         # honest: dialogue is not estimated yet
