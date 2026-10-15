"""Automatic recipes: autonomy levels that cannot be bypassed, every run ending in a decision for a person, a failed check always
stopping the recipe, and the kill switch holding it. Only the recipe's own work and the model check are replaced."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.db.mongo import activity_entries, get_db, workspace_events
from app.video import automation
from app.workers import video_automation as worker


async def _recipe(ws: str, **auto) -> dict:
    doc = {"id": uuid4().hex, "workspace_id": ws, "name": "Weekly clips", "steps": [{"flow": "x", "params": {}}], "runs": 0,
           "created_at": datetime.now(timezone.utc)}
    await get_db()["video_recipes"].insert_one(dict(doc))
    if auto:
        await automation.configure(doc["id"], ws, "u1", enabled=True, **auto)
    return await get_db()["video_recipes"].find_one({"id": doc["id"]})


def _good(project_ids=("p1", "p2")):
    async def run(recipe, render):
        run.render = render
        return {"ok": True, "videos": len(project_ids), "steps": [{"ok": True, "projects": [{"project_id": p} for p in project_ids]}]}

    run.render = None
    return run


async def test_the_default_level_waits_and_an_unknown_level_or_frequency_is_refused():
    ws = f"ws-{uuid4()}"
    recipe = await _recipe(ws)

    state = await automation.configure(recipe["id"], ws, "u1", enabled=True)
    assert state["autonomy"] == "draft_and_wait" and state["paused"] is False and state["publish"] is None

    with pytest.raises(automation.AutomationError):
        await automation.configure(recipe["id"], ws, "u1", enabled=True, autonomy="publish")
    with pytest.raises(automation.AutomationError):
        await automation.configure(recipe["id"], ws, "u1", enabled=True, frequency="hourly")
    with pytest.raises(automation.AutomationError):
        await automation.configure("missing", ws, "u1", enabled=True)


async def test_a_good_run_leaves_a_decision_for_a_person_and_sets_the_next_run():
    ws = f"ws-{uuid4()}"
    recipe = await _recipe(ws, frequency="daily", autonomy="render_and_wait")
    run = _good()
    now = datetime.now(timezone.utc)

    out = await automation.run_one(recipe, run=run, now=now)

    assert out == {"status": "ready", "projects": ["p1", "p2"]} and run.render is True
    [row] = await activity_entries.find({"workspace_id": ws}).to_list(length=5)
    assert row["lane"] == "active" and row["title"] == "Weekly clips made 2 videos" and "Nothing was published" in row["description"]
    fresh = await get_db()["video_recipes"].find_one({"id": recipe["id"]})
    assert abs((fresh["automation"]["next_run_at"].replace(tzinfo=timezone.utc) - (now + timedelta(days=1))).total_seconds()) < 5
    steps = [d["step"] async for d in get_db()["agent_decisions"].find({"workspace_id": ws})]
    assert steps == ["automation.ready"]


async def test_a_draft_level_recipe_does_not_render():
    ws = f"ws-{uuid4()}"
    recipe = await _recipe(ws, autonomy="draft_and_wait")
    run = _good()

    await automation.run_one(recipe, run=run)

    assert run.render is False
    [row] = await activity_entries.find({"workspace_id": ws}).to_list(length=5)
    assert "They are drafts" in row["description"]


async def test_a_failed_check_stops_the_recipe_asks_a_person_tells_the_bus_and_keeps_it_stopped():
    ws = f"ws-{uuid4()}"
    recipe = await _recipe(ws, autonomy="render_and_wait")

    async def failing(r, result):
        return ["the captions do not sound like your voice"]

    out = await automation.run_one(recipe, run=_good(), checks=failing)

    assert out["status"] == "stopped"
    fresh = await get_db()["video_recipes"].find_one({"id": recipe["id"]})
    assert fresh["automation"]["paused"] is True and "do not sound like your voice" in fresh["automation"]["paused_reason"]
    [row] = await activity_entries.find({"workspace_id": ws}).to_list(length=5)
    assert row["lane"] == "active" and row["title"] == "Weekly clips stopped" and "Nothing was published" in row["description"]
    assert await workspace_events.count_documents({"workspace_id": ws, "event_type": "video.automation_paused"}) == 1
    # a stopped recipe is not picked up again by the scheduler
    due = await get_db()["video_recipes"].count_documents({"id": recipe["id"], "automation.enabled": True, "automation.paused": {"$ne": True}})
    assert due == 0


async def test_a_run_that_raises_is_a_stop_not_a_silent_failure():
    ws = f"ws-{uuid4()}"
    recipe = await _recipe(ws, frequency="weekly")

    async def broken(r, render):
        raise RuntimeError("boom")

    out = await automation.run_one(recipe, run=broken)

    assert out["status"] == "stopped"
    assert (await get_db()["video_recipes"].find_one({"id": recipe["id"]}))["automation"]["paused"] is True


async def test_the_default_checks_name_what_went_wrong_and_a_run_that_made_nothing_is_not_a_pass():
    ok = {"videos": 1, "steps": [{"ok": True, "projects": [{"project_id": "p"}]}]}
    assert await automation.default_checks({}, ok) == []
    bad = {"videos": 1, "steps": [{"ok": False, "error": "That video is not in your library.", "projects": []}]}
    assert await automation.default_checks({}, bad) == ["That video is not in your library."]
    assert await automation.default_checks({}, {"videos": 0, "steps": []}) == ["it made no video"]
    broken_render = {"videos": 1, "steps": [{"ok": True, "projects": [{"project_id": "p", "error": "The render failed."}]}]}
    assert await automation.default_checks({}, broken_render) == ["The render failed."]


async def test_accepting_a_stopped_recipes_decision_switches_it_back_on_and_dismissing_leaves_it_stopped():
    ws = f"ws-{uuid4()}"
    stopped = await _recipe(ws, frequency="weekly")
    await get_db()["video_recipes"].update_one({"id": stopped["id"]}, {"$set": {"automation.paused": True, "automation.paused_reason": "x"}})

    await automation.apply_decision(stopped["id"], ws, "dismiss")
    assert (await get_db()["video_recipes"].find_one({"id": stopped["id"]}))["automation"]["paused"] is True

    await automation.apply_decision(stopped["id"], ws, "accept")
    fresh = await get_db()["video_recipes"].find_one({"id": stopped["id"]})
    assert fresh["automation"]["paused"] is False and fresh["automation"]["paused_reason"] is None


async def test_the_scheduler_holds_a_recipe_while_the_kill_switch_is_on_and_runs_it_after(monkeypatch):
    ws = f"ws-{uuid4()}"
    recipe = await _recipe(ws, frequency="daily")
    await get_db()["video_recipes"].update_one({"id": recipe["id"]}, {"$set": {"automation.next_run_at": datetime.now(timezone.utc) - timedelta(minutes=1)}})
    await get_db()["workspaces"].update_one({"id": ws}, {"$set": {"generation_halted": True}}, upsert=True)
    ran = []

    async def fake_once(r, **kw):
        ran.append(r["id"])

    monkeypatch.setattr(worker, "run_recipe_once", fake_once)

    await worker.run_due_video_recipes()
    assert recipe["id"] not in ran                                       # held, still due
    held = await activity_entries.find({"workspace_id": ws}).to_list(length=5)
    assert held and "is waiting" in held[0]["title"]

    await get_db()["workspaces"].update_one({"id": ws}, {"$set": {"generation_halted": False}})
    await worker.run_due_video_recipes()
    assert recipe["id"] in ran


async def test_the_voice_check_verdict_decides_whether_a_made_video_passes(monkeypatch):
    ws = f"ws-{uuid4()}"
    recipe = await _recipe(ws, frequency="weekly")
    await get_db()["video_projects"].insert_one({"id": "pv1", "workspace_id": ws, "edit": {"captions": {"words": [{"w": "Hello"}, {"w": "there"}]}}})
    lines = await worker._caption_lines(["pv1"], ws)
    assert lines == ["Hello there"]
    assert await worker._caption_lines(["missing"], ws) == []


# ── publishing by itself ─────────────────────────────────────────────────────────

async def _brand(ws: str) -> str:
    bid = uuid4().hex
    await get_db()["brand_profiles"].insert_one({"id": bid, "workspace_id": ws, "is_complete": True})
    return bid


async def _member(ws: str, role: str) -> str:
    uid = uuid4().hex
    await get_db()["workspace_members"].insert_one({"workspace_id": ws, "user_id": uid, "role": role, "status": "active"})
    return uid


async def test_only_an_owner_or_admin_can_choose_auto_publish_and_it_needs_a_brand_and_platforms():
    ws = f"ws-{uuid4()}"
    recipe = await _recipe(ws)
    brand = await _brand(ws)
    ok = {"brand_id": brand, "platforms": ["LinkedIn"]}

    for role in ("editor", "viewer", None):
        with pytest.raises(automation.AutomationError, match="owner or admin"):
            await automation.configure(recipe["id"], ws, "u1", enabled=True, autonomy="auto_publish", role=role, publish=ok)
    with pytest.raises(automation.AutomationError, match="brand"):
        await automation.configure(recipe["id"], ws, "u1", enabled=True, autonomy="auto_publish", role="owner", publish={"platforms": ["LinkedIn"]})
    with pytest.raises(automation.AutomationError, match="platform"):
        await automation.configure(recipe["id"], ws, "u1", enabled=True, autonomy="auto_publish", role="admin", publish={"brand_id": brand, "platforms": ["Nowhere"]})
    with pytest.raises(automation.AutomationError, match="brand was not found"):
        await automation.configure(recipe["id"], ws, "u1", enabled=True, autonomy="auto_publish", role="owner", publish={"brand_id": "other", "platforms": ["LinkedIn"]})

    state = await automation.configure(recipe["id"], ws, "u1", enabled=True, autonomy="auto_publish", role="admin",
                                       publish={**ok, "daily_cap": 99})
    assert state["publish"] == {"brand_id": brand, "platforms": ["LinkedIn"], "daily_cap": 3}      # the cap can be lowered, never raised past 3


async def test_a_passing_run_schedules_posts_and_leaves_a_decision_that_can_pull_them():
    ws = f"ws-{uuid4()}"
    owner = await _member(ws, "owner")
    recipe = await _recipe(ws)
    await automation.configure(recipe["id"], ws, owner, enabled=True, autonomy="auto_publish", role="owner",
                               publish={"brand_id": await _brand(ws), "platforms": ["LinkedIn"]})
    recipe = await get_db()["video_recipes"].find_one({"id": recipe["id"]})
    piece = f"p-{uuid4()}"
    await get_db()["content_pieces"].insert_one({"piece_id": piece, "workspace_id": ws, "publish_status": "queued"})
    seen = {}

    async def publisher(r, projects, when):
        seen["projects"] = projects
        return {"scheduled": [piece], "held": ["YouTube: connect the account first"]}

    out = await automation.run_one(recipe, run=_good(), publisher=publisher)

    assert out["status"] == "published" and seen["projects"] == ["p1", "p2"]
    [row] = await activity_entries.find({"workspace_id": ws}).to_list(length=5)
    assert row["lane"] == "active" and row["title"] == "Weekly clips scheduled 1 post"
    assert "Pull them" in row["description"] and "YouTube: connect the account first" in row["description"]
    assert row["metadata"]["pieceIds"] == [piece]

    await automation.apply_decision(recipe["id"], ws, "accept", row["metadata"])
    assert (await get_db()["content_pieces"].find_one({"piece_id": piece}))["publish_status"] == "queued"      # keeping them changes nothing
    await automation.apply_decision(recipe["id"], ws, "dismiss", row["metadata"])
    pulled = await get_db()["content_pieces"].find_one({"piece_id": piece})
    assert pulled["publish_status"] == "pending" and pulled["publish_scheduled_at"] == ""


async def test_a_failed_check_publishes_nothing_even_at_auto_publish():
    ws = f"ws-{uuid4()}"
    owner = await _member(ws, "owner")
    recipe = await _recipe(ws)
    await automation.configure(recipe["id"], ws, owner, enabled=True, autonomy="auto_publish", role="owner",
                               publish={"brand_id": await _brand(ws), "platforms": ["LinkedIn"]})
    recipe = await get_db()["video_recipes"].find_one({"id": recipe["id"]})
    called = []

    async def publisher(r, projects, when):
        called.append(1)
        return {"scheduled": [], "held": []}

    async def failing(r, result):
        return ["the captions do not sound like your voice"]

    out = await automation.run_one(recipe, run=_good(), checks=failing, publisher=publisher)

    assert out["status"] == "stopped" and called == []


async def test_when_the_person_who_allowed_it_is_no_longer_an_owner_or_admin_it_falls_back_to_waiting():
    ws = f"ws-{uuid4()}"
    admin = await _member(ws, "admin")
    recipe = await _recipe(ws)
    await automation.configure(recipe["id"], ws, admin, enabled=True, autonomy="auto_publish", role="admin",
                               publish={"brand_id": await _brand(ws), "platforms": ["LinkedIn"]})
    await get_db()["workspace_members"].update_one({"workspace_id": ws, "user_id": admin}, {"$set": {"role": "editor"}})
    recipe = await get_db()["video_recipes"].find_one({"id": recipe["id"]})
    called = []

    async def publisher(r, projects, when):
        called.append(1)
        return {"scheduled": [], "held": []}

    out = await automation.run_one(recipe, run=_good(), publisher=publisher)

    assert called == [] and out["status"] == "ready"
    fresh = await get_db()["video_recipes"].find_one({"id": recipe["id"]})
    assert fresh["automation"]["autonomy"] == "render_and_wait" and fresh["automation"]["publish"] is None
    titles = sorted(r["title"] for r in await activity_entries.find({"workspace_id": ws}).to_list(length=5))
    assert titles == ["Weekly clips made 2 videos", "Weekly clips no longer publishes by itself"]


async def test_the_post_text_is_the_title_then_the_start_of_what_is_said_cut_at_a_sentence():
    from app.video import auto_publish

    long = ["Word."] * 400
    text = auto_publish.post_text("  Launch  ", long)
    assert text.startswith("Launch\n\nWord. Word.") and len(text) <= auto_publish.MAX_TEXT + 12
    assert auto_publish.post_text("Short", ["a", "", "b"]) == "Short\n\na b"
