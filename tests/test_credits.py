"""Credits: prices, reserving and charging, limits, notices, housekeeping, the routes, and that every AI call has a charge point.

Real Mongo throughout; no model or provider is ever called (model calls are stubbed by `mock_llm`)."""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.billing import costs as C
from app.billing import credits
from app.core.config import settings
from app.db.mongo import brand_profiles, get_campaigns_collection, get_db, users, workspace_ai_budgets
from app.shared import entitlements
from tests.conftest import create_workspace


async def _ws(plan: str = "free") -> str:
    ws = f"ws-{uuid4()}"
    await get_db()["workspaces"].insert_one({"id": ws, "tier": "solo", "plan": plan, "name": "Credit test"})
    return ws


async def _limit(ws: str, n) -> None:
    await workspace_ai_budgets.update_one(
        {"workspace_id": ws}, {"$set": {"monthly_credit_limit": n}, "$setOnInsert": {"id": ws, "workspace_id": ws}}, upsert=True,
    )
    credits._limit_cache.pop(ws, None)
    credits.forget(ws)


async def _balance(ws: str) -> dict:
    credits.forget()
    return await credits.summary(ws)


@pytest.fixture
def enforced(monkeypatch):
    monkeypatch.setattr(settings, "BILLING_ENFORCED", True)
    entitlements.forget()
    credits._ensured.clear()
    yield
    entitlements.forget()
    credits._ensured.clear()


@pytest.fixture(autouse=True)
def _no_bound_run():
    credits.bind_run(None)
    yield
    credits.bind_run(None)


# ── prices and words ─────────────────────────────────────────────────────

def test_a_cost_per_unit_rounds_up_to_whole_credits_and_real_work_is_never_free():
    t = C.DEFAULT_COSTS
    assert C.price_with(t, "text_post") == 1 and C.price_with(t, "long_form") == 3 and C.price_with(t, "picture") == 2
    assert C.price_with(t, "narration", 1.2) == 4          # 3 a minute x 1.2 = 3.6
    assert C.price_with(t, "narration", 0.05) == 1         # a few seconds still costs something
    assert C.price_with(t, "translate", 2500) == 3         # 1 for each 1,000 words
    assert C.price_with(t, "video_analysis", 25) == 3      # 1 for each 10 minutes
    assert C.price_with(t, "render", 0) == 0
    assert C.price_with(t, "content_guard") == 0 and C.price_with(t, "mascot_first") == 0


def test_every_system_action_is_free_and_every_cost_says_its_unit():
    for key, cost in C.DEFAULT_COSTS.items():
        assert cost["module"] in C.MODULES, key
        assert cost["kind"] in ("active", "passive"), key
        if cost["kind"] == "passive":
            assert cost["credits"] == 0, key
    assert C.unit_label(C.DEFAULT_COSTS["narration"]) == "per minute"
    assert C.unit_label(C.DEFAULT_COSTS["translate"]) == "per 1,000 words"
    assert C.unit_label(C.DEFAULT_COSTS["video_analysis"]) == "per 10 minutes"
    assert C.unit_label(C.DEFAULT_COSTS["picture"]) is None


def test_the_limit_is_the_plan_once_plans_are_on_and_never_above_the_owners():
    assert credits.effective_limit(enforced=False, allowance=100, granted=0, cap=None) is None
    assert credits.effective_limit(enforced=False, allowance=100, granted=0, cap=40) == 40
    assert credits.effective_limit(enforced=True, allowance=100, granted=50, cap=None) == 150
    assert credits.effective_limit(enforced=True, allowance=100, granted=50, cap=120) == 120


def test_refusals_and_notices_are_plain_words():
    period = "2026-11"
    assert credits.refusal_message(need=40, left=12, limit=1200, by_owner=False, period=period) == "This needs about 40 credits. You have 12 left."
    assert credits.refusal_message(need=1, left=0, limit=1200, by_owner=False, period=period) == \
        "You've used all 1,200 credits this month. They start again on 1 Dec."
    assert "Odette under Quotas" in credits.refusal_message(need=1, left=0, limit=500, by_owner=True, period=period)
    title, body = credits.notice_words(80, used=80, limit=100, period=period, by_owner=False)
    assert title == "80% of this month's credits are used" and "1 Dec" in body
    title, body = credits.notice_words(100, used=100, limit=100, period=period, by_owner=False)
    assert "used up" in title and "publishing what is ready keep working" in body
    for text in (title, body):
        assert "—" not in text


def test_the_month_starts_again_on_the_first_in_utc():
    assert credits.period_of(datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc)) == "2026-12"
    assert credits.resets_at("2026-12") == datetime(2027, 1, 1, tzinfo=timezone.utc)


# ── reserve, settle, release ─────────────────────────────────────────────

async def test_a_hold_is_set_aside_then_charged_or_given_back():
    ws = await _ws()
    hold = await credits.reserve(ws, "u1", "picture")
    assert (await _balance(ws))["reserved"] == 2
    await credits.settle(hold)
    b = await _balance(ws)
    assert b["used"] == 2 and b["reserved"] == 0
    assert b["members"][0]["credits"] == 2 and b["actions"][0]["id"] == "picture"

    other = await credits.reserve(ws, "u1", "picture")
    await credits.release(other)
    await credits.settle(other)                        # a released hold is never charged after all
    b = await _balance(ws)
    assert b["used"] == 2 and b["reserved"] == 0
    rows = (await credits.ledger(ws))["items"]
    assert [r["kind"] for r in rows] == ["charge"] and rows[0]["credits"] == 2


async def test_the_real_amount_is_charged_not_the_estimate():
    ws = await _ws()
    async with credits.spend(ws, "u1", "narration", 2.0) as hold:   # 6 set aside
        hold.actual_units = 0.5                                     # 30 seconds were spoken
    assert (await _balance(ws))["used"] == 2


async def test_work_that_fails_or_makes_nothing_costs_nothing():
    ws = await _ws()
    with pytest.raises(RuntimeError):
        async with credits.spend(ws, "u1", "picture"):
            raise RuntimeError("provider down")
    async with credits.spend(ws, "u1", "picture") as hold:
        hold.nothing_made = True
    b = await _balance(ws)
    assert b["used"] == 0 and b["reserved"] == 0


async def test_a_retried_job_is_never_charged_twice_for_the_same_step():
    ws = await _ws()
    credits.bind_run("run-1")
    await credits.charge(ws, "u1", "text_post", step="text:0:LinkedIn")
    await credits.charge(ws, "u1", "text_post", step="text:0:X")
    credits.bind_run("run-1")                          # the job starts again from the top
    again = await credits.charge(ws, "u1", "text_post", step="text:0:LinkedIn")
    assert again.duplicate
    assert (await _balance(ws))["used"] == 2


async def test_spending_at_the_same_time_can_never_go_past_the_limit():
    ws = await _ws()
    await _limit(ws, 10)

    async def one():
        try:
            await credits.charge(ws, "u1", "text_post")
            return True
        except credits.CreditsExhausted:
            return False

    results = await asyncio.gather(*(one() for _ in range(30)))
    assert sum(results) == 10
    b = await _balance(ws)
    assert b["used"] == 10 and b["reserved"] == 0


async def test_the_owners_limit_applies_before_launch_and_the_plan_only_after(enforced):
    ws = await _ws("free")                               # Free gives 100 a month
    for _ in range(12):
        await credits.charge(ws, "u1", "picture_hd")     # 8 each, 96 in all
    with pytest.raises(credits.CreditsExhausted) as refused:
        await credits.charge(ws, "u1", "picture_hd")
    detail = refused.value.detail
    assert refused.value.status_code == 402 and detail["code"] == "limit_reached" and detail["limit"] == "credits_month"
    assert detail["message"] == "This needs about 8 credits. You have 4 left."


async def test_before_launch_nothing_stops_without_an_owner_limit():
    ws = await _ws("free")
    for _ in range(14):
        await credits.charge(ws, "u1", "picture_hd")     # 112, past Free's 100
    b = await _balance(ws)
    assert b["used"] == 112 and b["enforced"] is False and b["limit"] is None and b["plan_total"] == 100
    assert b["percent"] == 100


async def test_an_owner_cannot_set_a_limit_above_the_plan_and_a_grant_raises_room(enforced):
    from fastapi import HTTPException

    ws = await _ws("free")
    with pytest.raises(HTTPException):
        await credits.set_owner_limit(ws, 150)
    await credits.charge(ws, "u1", "picture_hd")
    await credits.grant(ws, 50, by="staff", note="beta tester")
    await credits.set_owner_limit(ws, 150)
    b = await _balance(ws)
    assert b["granted"] == 50 and b["owner_limit"] == 150 and b["limit"] == 150
    await credits.refund(ws, 8, by="staff", note="a render failed on our side")
    assert (await _balance(ws))["used"] == 0
    kinds = [r["kind"] for r in (await credits.ledger(ws))["items"]]
    assert kinds == ["refund", "grant", "charge"]


async def test_linked_workspaces_share_one_pool_once_plans_are_on(enforced):
    payer = await _ws("single")
    linked = f"ws-{uuid4()}"
    await get_db()["workspaces"].insert_one({"id": linked, "tier": "solo", "billing_group_id": payer})
    entitlements.forget()
    await credits.charge(payer, "u1", "picture")
    await credits.charge(linked, "u2", "picture")
    b = await _balance(linked)
    assert b["pool_id"] == payer and b["used"] == 4 and b["shared"] is True and b["allowance"] == 1200


async def test_a_new_month_starts_from_zero(monkeypatch):
    ws = await _ws()
    await credits.charge(ws, "u1", "picture")
    later = datetime.now(timezone.utc).replace(day=1) + timedelta(days=40)
    monkeypatch.setattr(credits, "_now", lambda: later)
    b = await _balance(ws)
    assert b["used"] == 0 and b["period"] == credits.period_of(later)


async def test_a_plan_change_mid_month_never_lowers_the_allowance(enforced):
    ws = await _ws("single")
    await credits.charge(ws, "u1", "picture")
    await get_db()["workspaces"].update_one({"id": ws}, {"$set": {"plan": "free"}})
    entitlements.forget()
    credits._ensured.clear()
    await credits.charge(ws, "u1", "picture")
    assert (await _balance(ws))["allowance"] == 1200


# ── notices and housekeeping ─────────────────────────────────────────────

async def test_members_are_told_once_at_eighty_and_once_at_a_hundred_percent(monkeypatch):
    ws = await _ws()
    await _limit(ws, 10)
    sent: list[int] = []

    async def capture(pool, period, threshold, **kw):
        sent.append(threshold)

    monkeypatch.setattr(credits, "_send_notice", capture)

    async def charge(n):
        for _ in range(n):
            await credits.charge(ws, "u1", "text_post")
        await asyncio.gather(*list(credits._tasks))

    await charge(7)
    assert sent == []
    await charge(1)
    assert sent == [80]
    await charge(1)
    assert sent == [80]
    await charge(1)
    assert sent == [80, 100]


async def test_a_hold_left_by_a_crash_is_given_back_and_still_charged_if_the_work_ends():
    ws = await _ws()
    hold = await credits.reserve(ws, "u1", "render", 2, hold_seconds=-1)
    assert await credits.sweep() >= 1
    assert (await _balance(ws))["reserved"] == 0
    await credits.settle(hold)                         # the render finished after all
    b = await _balance(ws)
    assert b["used"] == 10 and b["reserved"] == 0


async def test_the_nightly_check_finds_a_balance_that_differs_from_its_ledger():
    ws = await _ws()
    await credits.charge(ws, "u1", "picture")
    await credits.balances_col().update_one({"pool_id": ws}, {"$inc": {"used": 5}})
    found = [d for d in await credits.reconcile() if d["pool_id"] == ws]
    assert found and found[0]["used"] == 7 and found[0]["ledger_used"] == 2


async def test_a_render_sets_its_credits_aside_and_gives_them_back_when_it_ends_without_a_video():
    from app.video import jobs
    from tests.test_video_worker_routes import INPUTS, _edit

    ws = await _ws("single")
    job = await jobs.enqueue(workspace_id=ws, user_id="u1", inputs=INPUTS, edit=_edit(seconds=60))
    assert (await _balance(ws))["reserved"] == 5
    doc = await get_db()["video_jobs"].find_one({"id": job["id"]})
    assert doc["credit_hold"]["action"] == "render"
    await jobs._close_job(doc, "failed", code="worker_lost", message="lost")
    b = await _balance(ws)
    assert b["reserved"] == 0 and b["used"] == 0


# ── through the real routes ──────────────────────────────────────────────

async def _brand(client, ws_id: str) -> str:
    res = await client.post("/api/v1/brand/", json={"brand_type": "Person"}, headers={"X-Workspace-Id": ws_id})
    assert res.status_code in (200, 201), res.text
    brand_id = res.json()["brand_profile_id"]
    await brand_profiles.update_one({"id": brand_id}, {"$set": {"is_complete": True}})
    return brand_id


async def test_a_post_costs_one_credit_and_is_refused_once_the_limit_is_used(signup_user, mock_llm):
    client, _ = await signup_user()
    ws = await create_workspace(client, "Credits Text")
    brand_id = await _brand(client, ws)
    headers = {"X-Workspace-Id": ws}
    mock_llm.set_plain("A post written inside the credits.")
    mock_llm.set_structured({})

    async def regenerate():
        return await client.post("/api/v1/text/regenerate", json={"platform": "LinkedIn", "brand_id": brand_id, "content": "Some source."},
                                 headers=headers)

    assert (await regenerate()).status_code == 200
    assert (await _balance(ws))["used"] == 1

    res = await client.put("/api/v1/ops/ai/budget", json={"monthly_credit_limit": 1}, headers=headers)
    assert res.status_code == 200 and res.json()["monthly_credit_limit"] == 1
    blocked = await regenerate()
    assert blocked.status_code == 402, blocked.text
    assert blocked.json()["detail"]["code"] == "limit_reached" and "Quotas" in blocked.json()["detail"]["message"]

    await client.put("/api/v1/ops/ai/budget", json={"monthly_credit_limit": None}, headers=headers)
    assert (await regenerate()).status_code == 200


async def test_every_member_sees_the_balance_history_and_costs(signup_user):
    client, _ = await signup_user()
    ws = await create_workspace(client, "Credits Panel")
    headers = {"X-Workspace-Id": ws}
    await credits.charge(ws, None, "picture")
    panel = (await client.get("/api/v1/ops/ai/credits", headers=headers)).json()
    assert panel["used"] == 2 and panel["plan_total"] == 100 and panel["resets_on"] and panel["insight"]["text"]
    assert {m["key"] for m in panel["modules"]} == set(C.MODULES) - {"system"}
    history = (await client.get("/api/v1/ops/ai/credits/history", headers=headers)).json()
    assert history["items"][0]["label"] == C.DEFAULT_COSTS["picture"]["label"] and history["items"][0]["member"] == "Automations"
    table = (await client.get("/api/v1/ops/ai/credit-costs", headers=headers)).json()
    assert {r["id"] for r in table["costs"]} == set(C.DEFAULT_COSTS)
    assert (await client.put("/api/v1/ops/ai/budget", json={"monthly_credit_limit": 500}, headers=headers)).status_code == 400


async def test_only_the_ops_owner_grants_credits_or_changes_a_cost(signup_user):
    staff, staff_profile = await signup_user()
    owner, owner_profile = await signup_user()
    await users.update_one({"id": staff_profile["id"]}, {"$set": {"is_platform_staff": True}})
    await users.update_one({"id": owner_profile["id"]}, {"$set": {"is_master_admin": True}})
    ws = await _ws()
    try:
        assert (await staff.post(f"/api/v1/ops/plans/credits/workspaces/{ws}/grant", json={"credits": 50, "reason": "test"})).status_code == 403
        granted = await owner.post(f"/api/v1/ops/plans/credits/workspaces/{ws}/grant", json={"credits": 50, "reason": "beta tester"})
        assert granted.status_code == 200 and granted.json()["balance"]["granted"] == 50
        seen = (await staff.get(f"/api/v1/ops/plans/credits/workspaces/{ws}")).json()
        assert seen["ledger"]["items"][0]["kind"] == "grant"

        assert (await owner.put("/api/v1/ops/plans/credits/costs/content_guard", json={"credits": 1, "reason": "make it paid"})).status_code == 400
        changed = await owner.put("/api/v1/ops/plans/credits/costs/picture", json={"credits": 3, "reason": "pictures cost more"})
        assert changed.status_code == 200 and changed.json()["credits"] == 3 and changed.json()["default_credits"] == 2
        assert await C.price("picture") == 3
        back = await owner.put("/api/v1/ops/plans/credits/costs/picture", json={"credits": 2, "reason": "back"})
        assert back.json()["credits"] == 2

        report = (await staff.get("/api/v1/ops/plans/credits/report")).json()
        assert report["target_usd_per_credit"] == 0.0035 and "credits_charged" in report
    finally:
        await get_db()["billing_credit_costs"].delete_many({})
        C.forget()
        await users.update_many({"id": {"$in": [staff_profile["id"], owner_profile["id"]]}},
                                {"$unset": {"is_platform_staff": "", "is_master_admin": ""}})


async def test_a_scheduled_campaign_waits_for_credits_without_counting_a_failure(signup_user, monkeypatch):
    from app.workers import campaign_scheduler
    from app.shared import pipeline_runs as runs

    client, user = await signup_user()
    ws = await create_workspace(client, "Credits Campaign")
    await get_campaigns_collection().insert_one({
        "id": f"cc-{uuid4().hex[:8]}", "workspace_id": ws, "created_by": user["id"], "brand_id": "b", "name": "Weekly",
        "topic_cluster": "topic", "status": "active", "deleted": False,
        "cadence": {"frequency": "daily", "next_run_at": datetime.now(timezone.utc) - timedelta(minutes=5), "failures": 0},
    })

    async def out_of_credits(*args, **kwargs):
        raise credits.CreditsExhausted({"code": "limit_reached", "limit": "credits_month", "message": "No credits left."})

    paused: list[str] = []

    async def note(workspace_id, what, **kw):
        paused.append(what)

    monkeypatch.setattr(campaign_scheduler, "generate_campaign_batch", out_of_credits)
    monkeypatch.setattr(credits, "pause_notice", note)
    await campaign_scheduler.run_due_campaign_batches()
    await asyncio.gather(*list(runs._tasks.values()), return_exceptions=True)

    doc = await get_campaigns_collection().find_one({"workspace_id": ws})
    assert doc["cadence"]["failures"] == 0 and doc["cadence"]["last_error"] == "Waiting for credits."
    assert doc["cadence"]["next_run_at"].replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)
    assert paused == ['Campaign "Weekly"']


# ── every AI call has a charge point ─────────────────────────────────────

APP = Path(__file__).resolve().parents[1] / "app"
AI_CALL = re.compile(
    r"\b(call_llm|call_llm_structured|call_llm_fallback|call_vision|synthesize_speech|synthesize_speech_timed|transcribe_audio_bytes|"
    r"transcribe_audio_detailed|transcribe_audio|generate_image_from_prompt|generate_brand_image|generate_brand_mascot|embed_text|"
    r"reduce_echo|make_clip|run_image_agent)\("
)
CHARGES = re.compile(r"credits\.(spend|reserve|charge)\(|charges\.voiced\(")

#: Every file that asks a model or a provider for something, and where its credits are charged. "here" means the file itself must
#: charge; anything else names the charge point that covers it or says why it is free. A new file making AI calls fails this test
#: until it is added here with an answer.
AI_FILES: dict[str, str] = {
    "api/v1/audio_assets.py": "here",
    "api/v1/brand.py": "here",
    "api/v1/content.py": "generate_brand_image charges the picture",
    "api/v1/image_assets.py": "here; the upload description is free system work",
    "api/v1/search.py": "free: search is always free (plan 9.1)",
    "api/v1/suggest.py": "free: typing suggestions are always free, with their own daily cap",
    "api/v1/thumbnails.py": "generate_brand_image charges the picture",
    "api/v1/video_narrate.py": "here",
    "api/v1/video_prompt.py": "here; AI clips are off on this server (GPU work is parked) and unpriced",
    "agents/analytics/nodes.py": "free: the analytics answer is system work, cached on the dashboard",
    "agents/audio/nodes.py": "charged by the audio routes that run it (show notes and the rest)",
    "agents/content_guard/agent.py": "free: safety checks never cost credits",
    "agents/content_guard/media.py": "free: safety checks never cost credits",
    "agents/personal/assist.py": "charged by /assistant/assist (draft feedback); free as an agent's own check",
    "agents/personal/nodes.py": "free: Remy's background voice check",
    "agents/runtime/toolkit.py": "each tool runs a charged entry point",
    "agents/supervisor/nodes.py": "free: Odette",
    "pipelines/audio/transcriber.py": "charged by the audio upload, import and transcribe routes",
    "pipelines/brand/trait_extraction.py": "charged by the training-samples route (voice analysis)",
    "pipelines/brand/voice_playground.py": "charged by the preview-rewrite route",
    "pipelines/brand/voice_suggestions.py": "charged by the suggest-voice-patterns route",
    "pipelines/campaigns/suggest.py": "free: campaign topic ideas are bundled",
    "pipelines/image/orchestrator.py": "generate_brand_image charges the picture",
    "pipelines/media/default_image.py": "generate_brand_image charges the default picture",
    "pipelines/media/echo_reduction.py": "charged by the cleanup route",
    "pipelines/media/headline.py": "bundled with the campaign picture it is drawn on",
    "pipelines/media/image_generation.py": "here",
    "pipelines/media/tts_generation.py": "charged by every route that voices (charges.voiced)",
    "pipelines/media/video_analysis.py": "here",
    "pipelines/presets_suggest.py": "free: preset suggestions are bundled",
    "pipelines/publish/youtube/metadata.py": "free: details made at publish are system work",
    "pipelines/text/angles.py": "free: hooks and angles are bundled",
    "pipelines/brand/watermark_tools.py": "here; the text suggestions are free like hooks",
    "pipelines/text/media_angles.py": "free: the 3 ideas are bundled like the written angles; each piece made is charged by its own pipeline",
    "pipelines/text/chips.py": "charged by /text/refine and /text/refine-chat",
    "pipelines/text/critique.py": "part of the post, charged by run_text_pipeline",
    "pipelines/text/generator.py": "part of the post, charged by run_text_pipeline",
    "pipelines/text/hook_agent.py": "part of the post, charged by run_text_pipeline",
    "pipelines/text/normalizer.py": "part of the post, charged by run_text_pipeline",
    "pipelines/text/orchestrator.py": "here",
    "pipelines/text/reader_review.py": "charged by /text/reader-review",
    "pipelines/text/repurpose.py": "part of the post, charged by run_text_pipeline",
    "pipelines/text/repurpose_suggest.py": "free: bundled",
    "pipelines/text/scorer.py": "free: scoring is bundled",
    "pipelines/text/seo.py": "part of the long-form post, charged by run_text_pipeline",
    "shared/localized_strings.py": "free: translated labels are system work, cached",
    "shared/support_ai.py": "free: support is always free",
    "video/analysis/analyze.py": "charged by the video analysis route and the flows",
    "video/clipgen.py": "off on this server (GPU work is parked) and unpriced",
    "video/create/generate.py": "charged by the video create, prompt and flow routes",
    "video/transcribe.py": "here",
}


def test_every_file_that_makes_ai_calls_has_a_charge_point_or_a_reason():
    found = set()
    for path in APP.rglob("*.py"):
        rel = path.relative_to(APP).as_posix()
        if rel in ("shared/llm.py", "billing/report.py") or rel.startswith("shared/llm_health/"):
            continue
        text = path.read_text(encoding="utf-8")
        if AI_CALL.search(text):
            found.add(rel)
    missing = sorted(found - set(AI_FILES))
    assert not missing, f"AI calls with no charge point listed: {missing}. Charge them, or add them to AI_FILES with the reason."
    for rel, how in AI_FILES.items():
        if how.startswith("here"):
            assert CHARGES.search((APP / rel).read_text(encoding="utf-8")), f"{rel} is listed as charging but has no charge point"


def test_every_charged_action_in_the_code_is_in_the_costs_table():
    """Each charge call names an action from the costs table (or passes one along in a variable), so a typo cannot charge nothing."""
    call = re.compile(r"(?:credits\.(?:spend|reserve|charge)|charges\.voiced|_charged)\(")
    named = 0
    for path in APP.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for m in call.finditer(text):
            if text[max(0, m.start() - 4): m.start()] == "def ":
                continue
            args = text[m.end(): m.end() + 260]
            words = re.findall(r'"([a-z_]+)"', args)
            if any(w in C.DEFAULT_COSTS for w in words):
                named += 1
                continue
            assert re.match(r"\s*(?:[\w.]+,\s*){0,3}(?:credit_action|action|_post_action\(platform\))\W", args), f"{path.relative_to(APP)}: a charge with no known action: {args[:90]!r}"
    assert named > 40


# ── bugs found in the 2026-10-29 review, kept from coming back ──────────

async def test_a_run_that_exactly_fits_is_not_refused_by_its_own_hold():
    """The quick check inside a run must not count the run's own reservation (limit 10, 9 used, a 1-credit post fits)."""
    from app.agents.supervisor.service import assert_ai_budget_available

    ws = await _ws()
    await _limit(ws, 10)
    for _ in range(9):
        await credits.charge(ws, "u1", "text_post")
    hold = await credits.reserve(ws, "u1", "text_post")
    await assert_ai_budget_available(ws)               # must not raise
    await credits.settle(hold)
    assert (await _balance(ws))["used"] == 10


async def test_a_retry_after_a_failed_step_is_charged_once():
    ws = await _ws()
    credits.bind_run("run-retry")
    with pytest.raises(RuntimeError):
        async with credits.spend(ws, "u1", "picture", step="picture"):
            raise RuntimeError("provider down")
    credits.bind_run("run-retry")                       # the job is restarted and the step now works
    async with credits.spend(ws, "u1", "picture", step="picture") as hold:
        assert not hold.duplicate
    credits.bind_run("run-retry")                       # restarted again after it was charged: not charged twice
    again = await credits.charge(ws, "u1", "picture", step="picture")
    assert again.duplicate
    assert (await _balance(ws))["used"] == 2


async def test_a_hold_left_half_made_by_a_crash_never_returns_credits_that_were_not_taken():
    ws = await _ws()
    await credits.charge(ws, "u1", "picture")           # makes the month's record
    await credits.holds_col().insert_one({
        "_id": f"{ws}:crash", "pool_id": ws, "period": credits.period_of(), "workspace_id": ws, "user_id": "u1", "action": "picture",
        "units": 1, "credits": 2, "run_id": None, "status": "pending", "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) - timedelta(minutes=1),
    })
    await credits.sweep()
    b = await _balance(ws)
    assert b["reserved"] == 0 and b["used"] == 2
    assert (await credits.holds_col().find_one({"_id": f"{ws}:crash"}))["status"] == "abandoned"


async def test_a_limit_of_zero_reads_as_full():
    ws = await _ws()
    await _limit(ws, 0)
    b = await _balance(ws)
    assert b["percent"] == 100 and b["left"] == 0 and b["insight"]["kind"] == "warn"


def test_a_run_stopped_for_credits_says_why_in_plain_words():
    from app.shared.pipeline_runs import _plain

    exc = credits.CreditsExhausted({"code": "limit_reached", "message": "You've used all 100 credits this month."})
    assert _plain(exc) == "You've used all 100 credits this month."


async def test_members_never_see_a_staff_note_in_the_history():
    ws = await _ws()
    await credits.grant(ws, 10, by="staff", note="private reason")
    assert (await credits.ledger(ws))["items"][0]["note"] is None
    assert (await credits.ledger(ws, for_ops=True))["items"][0]["note"] == "private reason"


async def test_search_and_typing_help_stay_on_at_zero_credits(signup_user, mock_llm):
    mock_llm.set_structured({"matches": []})
    client, _ = await signup_user()
    ws = await create_workspace(client, "Credits Free Work")
    await _limit(ws, 0)
    res = await client.post("/api/v1/search/agentic", json={"query": "hello"}, headers={"X-Workspace-Id": ws})
    assert res.status_code == 200, res.text


async def test_a_bound_label_names_every_charge_made_under_it():
    ws = await _ws()
    credits.bind_label("Fresh Angles picture")
    try:
        await credits.charge(ws, "u1", "picture", label="Picture")
    finally:
        credits.bind_label(None)
    await credits.charge(ws, "u1", "picture")
    rows = (await credits.ledger(ws))["items"]
    assert [r["label"] for r in rows] == [C.DEFAULT_COSTS["picture"]["label"], "Fresh Angles picture"]
