"""The decision harness and shadow mode: a candidate is judged on labelled requests per language, a failing decider shows as flaky, a shadow candidate is recorded and
never acted on, Ops can switch it and sees its agreement, and a request that is confirmed or cancelled is labelled so. No model is called."""
from pathlib import Path
from uuid import uuid4

import pytest

from app.ask import decisions, router, runs, shortlist, threads
from app.db.mongo import get_db, users
from app.core import staff
from app.tools.registry import get_registry
from tests.conftest import create_workspace, signup_new_user

CASES = Path(__file__).parent / "data" / "ask_decision_cases.jsonl"


def specs():
    return [s for s in get_registry().all() if not s.name.startswith(("compose.", "changes."))]


async def keyword_shortlister(text, all_specs):
    async def no_embed(space, texts):
        return []

    names, _ = await shortlist.shortlist(text, all_specs, embed=no_embed, space="")
    by = {s.name: s for s in all_specs}
    return [by[n] for n in names if n in by]


def test_the_seed_cases_load_name_real_tools_and_say_which_are_checked_by_a_native_speaker():
    cases = decisions.load_cases(CASES)
    names = {s.name for s in get_registry().all()}
    assert len(cases) >= 12 and {c.language for c in cases} >= {"en", "hi", "ta", "ta-latn", "hi-latn"}
    assert all(c.tool in names for c in cases if c.tool) and any(c.tool is None for c in cases)
    assert all(not c.reviewed for c in cases if c.language != "en")                          # nothing non-English is claimed as checked yet


def test_a_bad_line_in_a_cases_file_is_skipped(tmp_path):
    f = tmp_path / "c.jsonl"
    f.write_text('# a comment\n{"text": "ok", "language": "en", "tool": "plan.limits"}\nnot json\n{"language": "en"}\n\n', encoding="utf-8")
    assert [c.text for c in decisions.load_cases(f)] == ["ok"]


async def test_a_decider_is_scored_overall_and_per_language_with_every_miss_listed():
    cases = [decisions.Case("a", "en", "plan.limits"), decisions.Case("b", "en", "credits.summary"), decisions.Case("c", "hi", "plan.limits"), decisions.Case("d", "hi", None)]

    async def decider(text, candidates, language):
        return {"a": decisions.Decision("tool", "plan.limits"), "b": decisions.Decision("tool", "wrong.tool"), "c": decisions.Decision("tool", "plan.limits"), "d": decisions.Decision("none")}[text]

    async def all_specs(text, s):
        return s

    report = await decisions.evaluate("stub", decider, cases, specs(), all_specs)
    assert report.total == 4 and report.correct == 3 and report.accuracy == 0.75 and report.language_accuracy() == {"en": 0.5, "hi": 1.0}
    assert report.misses == [{"text": "b", "language": "en", "expected": "credits.summary", "got": "wrong.tool"}]


async def test_a_decider_that_fails_counts_as_a_miss_and_shows_in_errors_and_the_best_is_listed_first():
    cases = [decisions.Case("a", "en", "plan.limits"), decisions.Case("b", "en", "plan.limits")]

    async def flaky(text, candidates, language):
        raise RuntimeError("model down")

    async def good(text, candidates, language):
        return decisions.Decision("tool", "plan.limits")

    async def all_specs(text, s):
        return s

    ranked = await decisions.compare({"flaky": flaky, "good": good}, cases, specs(), all_specs)
    assert [r.name for r in ranked] == ["good", "flaky"] and ranked[1].errors == 2 and ranked[1].accuracy == 0.0 and ranked[0].accuracy == 1.0
    assert ranked[0].summary()["by_language"] == {"en": 1.0}


async def test_the_no_model_baseline_runs_on_the_seed_cases_so_a_candidate_has_something_to_beat():
    report = await decisions.evaluate("shortlist_top", decisions.shortlist_top, decisions.load_cases(CASES), specs(), keyword_shortlister)
    assert report.total >= 12 and 0.0 <= report.accuracy <= 1.0 and report.errors == 0
    assert report.language_accuracy()["en"] >= 0.5                                      # English words match tool names and summaries


# ── shadow mode, with a real workspace ───────────────────────────────────────

@pytest.fixture
async def world(signup_user, monkeypatch):
    client, profile = await signup_user()
    ws = await create_workspace(client, f"Shadow {uuid4().hex[:6]}")
    await runs.create_indexes()
    await threads.create_indexes()
    await get_db()["ask_router"].delete_many({"_id": "shadow"})

    async def no_space():
        return ""

    monkeypatch.setattr(shortlist, "pick_space", no_space)
    return client, profile["id"], ws, {"X-Workspace-Id": ws}


def free_read():
    from app.tools.contract import Risk

    return next(s for s in get_registry().all() if s.risk is Risk.READ and not s.cost_key and not (s.input_schema() or {}).get("required")
                and not s.name.startswith(("compose.", "changes.")) and not s.human_only)


async def test_a_shadow_candidate_is_recorded_beside_the_live_choice_and_never_acted_on(world, monkeypatch):
    client, user, ws, h = world
    tool = free_read()

    async def think(role, prompt, system):
        return {"action": "none"}                                                       # the live step finds nothing to do

    async def eager(text, candidates, language):
        return decisions.Decision("tool", tool.name)                                    # the candidate would have run a tool

    monkeypatch.setattr(router, "think", think)
    monkeypatch.setitem(decisions.CANDIDATES, "eager", eager)
    await decisions.set_shadow("eager")
    run = await runs.create(ws, user, "show me something")
    assert run["status"] == "none"                                                      # only the live decision was acted on
    saved = await runs.runs().find_one({"id": run["id"]})
    assert saved["shadow"]["name"] == "eager" and saved["shadow"]["tool"] == tool.name and saved["shadow"]["agrees"] is False and saved.get("compose_run_id") is None
    assert "shadow" not in run                                                          # never shown to the person


async def test_a_failing_shadow_candidate_never_stops_the_request(world, monkeypatch):
    client, user, ws, h = world

    async def think(role, prompt, system):
        return {"action": "none"}

    async def broken(text, candidates, language):
        raise RuntimeError("candidate down")

    monkeypatch.setattr(router, "think", think)
    monkeypatch.setitem(decisions.CANDIDATES, "broken", broken)
    await decisions.set_shadow("broken")
    run = await runs.create(ws, user, "hello")
    assert run["status"] == "none" and (await runs.runs().find_one({"id": run["id"]}))["shadow"]["error"] == "RuntimeError"


async def test_a_confirmed_request_is_labelled_accepted_and_a_cancelled_one_is_not(world, monkeypatch):
    client, user, ws, h = world
    from app.billing import costs as credit_costs
    from app.tools.contract import Risk

    table = await credit_costs.costs()
    priced = next(s for s in get_registry().all() if s.cost_key and credit_costs.price_with(table, s.cost_key, 1.0) > 0 and s.risk is not Risk.PUBLISH and not s.human_only
                  and s.scope.value in ("create",) and not s.name.startswith(("compose.", "changes.")) and not (s.input_schema() or {}).get("required"))

    async def think(role, prompt, system):
        return {"action": "tool", "tool": priced.name}

    async def no_problems(gate, cred, steps):
        return []

    async def fake_execute(workspace_id, user_id, run_id, steps, services):
        await runs.runs().update_one({"id": run_id}, {"$set": {"status": "done", "result": {"status": "done", "steps": []}}})

    monkeypatch.setattr(router, "think", think)
    monkeypatch.setattr(runs, "_check_steps", no_problems)
    monkeypatch.setattr(runs, "_execute", fake_execute)
    yes = await runs.create(ws, user, priced.summary)
    await runs.confirm(ws, user, yes["id"], wait=True)
    no = await runs.create(ws, user, priced.summary)
    await runs.stop(ws, user, no["id"])
    assert (await runs.runs().find_one({"id": yes["id"]}))["accepted"] is True and (await runs.runs().find_one({"id": no["id"]}))["accepted"] is False


async def test_ops_can_switch_a_candidate_on_and_off_and_sees_how_often_it_agreed(world, make_client):
    client, user, ws, h = world
    admin = make_client()
    staff_user = await signup_new_user(admin, name="Staff")
    await users.update_one({"id": staff_user["id"]}, {"$set": {"is_platform_staff": True, "is_master_admin": True, "staff": {"level": "admin"}}})
    staff.forget(staff_user["id"])
    O = "/api/v1/ops/tools/ask-shadow"
    assert (await client.get(O)).status_code in (401, 403)
    assert (await admin.get(O)).json()["current"] is None and "shortlist_top" in (await admin.get(O)).json()["candidates"]
    assert (await admin.put(O, json={"name": "no-such", "reason": "test"})).status_code == 422
    on = await admin.put(O, json={"name": "shortlist_top", "reason": "measure the baseline"})
    assert on.json()["current"] == "shortlist_top"

    async def think(role, prompt, system):
        return {"action": "none"}

    monkeypatch_think = router.think
    router.think = think
    try:
        await runs.create(ws, user, "show my credits")
    finally:
        router.think = monkeypatch_think
    agreed = (await admin.get(O)).json()["agreement"]
    row = next(a for a in agreed if a["name"] == "shortlist_top")
    assert row["requests"] >= 1
    assert (await admin.put(O, json={"name": None, "reason": "done"})).json()["current"] is None
