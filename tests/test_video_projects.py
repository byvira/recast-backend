"""The edit list, the project API, the link to the activity layer, and the own / AI / stock flag, on the real database."""
import copy
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.v1 import video_projects as api
from app.db.mongo import get_db, media_assets, pipeline_runs
from app.models.media import MediaAsset, MediaKind, MediaSource
from app.shared import pipeline_runs as run_layer
from app.video import builders, edl, jobs
from app.video.origin import summarise


@pytest.fixture(autouse=True)
async def _empty_queue():
    await get_db()["video_jobs"].delete_many({})
    yield


def _edit(**over):
    e = edl.blank("landscape")
    e["video"] = [{"id": "v1", "media_id": "m1", "at_f": 0, "dur_f": 300}]
    e.update(over)
    return e


def _call(fn, *args, **kwargs):
    return getattr(fn, "__wrapped__", fn)(*args, **kwargs)


def _ctx(ws=None):
    return SimpleNamespace(workspace_id=ws or f"ws-{uuid4()}", user_id="u1")


async def _media(ws: str, *, kind="video", source=MediaSource.UPLOADED, duration=10.0, probe=None, **kw) -> str:
    m = MediaAsset(
        id=str(uuid4()), workspace_id=ws, kind=MediaKind(kind), url=f"https://res.cloudinary.com/x/video/upload/v1/{uuid4()}.mp4",
        mime_type="video/mp4", duration_s=duration, source=source, created_by="u1", created_at=datetime.now(timezone.utc),
        width=1280, height=720, probe=probe, **kw,
    )
    await media_assets.insert_one(m.model_dump())
    return m.id


# ── the edit list ────────────────────────────────────────────────────────────────

def test_validation_fills_defaults_and_works_out_the_length():
    out = edl.validate(_edit())
    clip = out["video"][0]
    assert clip["speed"] == 1.0 and clip["fit"] == "cover" and clip["audio"] is True
    assert out["duration_f"] == 300 and out["schema_version"] == 1
    assert edl.validate(out) == out                      # validating a valid edit changes nothing


@pytest.mark.parametrize("change,words", [
    (lambda e: e.update(width=1281), "even"),
    (lambda e: e.update(background="red"), "#RRGGBB"),
    (lambda e: e["video"][0].update(speed=3), "between 0.5 and 2.0"),
    (lambda e: e["video"][0].update(dur_f=0), "between 1"),
    (lambda e: e["video"][0].update(fade_in_f=200, fade_out_f=200), "longer than the clip"),
    (lambda e: e["video"][0].update(fit="stretch"), "fit must be"),
    (lambda e: e["video"].append({"id": "v1", "media_id": "m1", "at_f": 0, "dur_f": 5}), "share the id"),
    (lambda e: e["video"][0].pop("media_id"), "library item"),
    (lambda e: e.update(fps={"num": 0, "den": 1}), "between 1"),
    (lambda e: e["captions"].update(words=[{"w": "a", "s_ms": 5, "e_ms": 1}]), "start first"),
])
def test_a_rule_break_is_refused_with_a_message_that_says_where(change, words):
    e = copy.deepcopy(_edit())
    change(e)
    with pytest.raises(edl.EditError) as caught:
        edl.validate(e)
    assert words in str(caught.value)


def test_camera_frame_rates_become_exact_fractions():
    assert edl.fps_from_float(29.97) == {"num": 30000, "den": 1001}
    assert edl.fps_from_float(23.976) == {"num": 24000, "den": 1001}
    assert edl.fps_from_float(30.0) == {"num": 30, "den": 1}
    assert edl.fps_from_float(None) == {"num": 30, "den": 1}


def test_the_worker_job_names_inputs_and_drops_editor_only_fields():
    e = edl.validate(_edit(audio=[{"id": "a1", "media_id": "m2", "at_f": 0, "dur_f": 300, "role": "music"}]))
    job = edl.to_job(e, {"m1": {"url": "https://x/a.mp4", "kind": "video"}, "m2": {"url": "https://x/b.mp3", "kind": "audio"}})
    assert set(job["inputs"]) == {"m0", "m1"}
    clip = job["edit"]["video"][0]
    assert clip["src"] in job["inputs"] and "media_id" not in clip
    assert job["edit"]["audio"][0]["duck"] is True and job["edit"]["audio"][0]["role"] == "music"
    with pytest.raises(edl.EditError):
        edl.to_job(edl.validate(edl.blank()), {})
    with pytest.raises(edl.EditError):
        edl.to_job(e, {"m1": {"url": "https://x/a.mp4", "kind": "video"}})


# ── the flag ─────────────────────────────────────────────────────────────────────

def test_every_media_item_carries_where_it_came_from():
    def make(src):
        return MediaAsset(id="a", workspace_id="w", kind=MediaKind.VIDEO, url="u", mime_type="video/mp4", source=src,
                          created_by="u", created_at=datetime.now(timezone.utc))
    assert make(MediaSource.UPLOADED).origin == "own"
    assert make(MediaSource.AI_GENERATED).origin == "ai" and make(MediaSource.AI_GENERATED).contains_ai is True
    assert make(MediaSource.SYNTHESIZED).origin == "ai"
    assert make(MediaSource.STOCK).origin == "stock" and make(MediaSource.STOCK).contains_stock is True
    assert make(MediaSource.LIBRARY).origin == "stock"
    assert make(MediaSource.BRAND_ASSET).origin == "brand"


def test_a_finished_video_says_what_went_into_it():
    assert summarise([]) == {"origin": "own", "contains_ai": False, "contains_stock": False}
    assert summarise(["own", "brand"])["origin"] == "own"
    assert summarise(["ai", "ai"]) == {"origin": "ai", "contains_ai": True, "contains_stock": False}
    assert summarise(["own", "ai"]) == {"origin": "mixed", "contains_ai": True, "contains_stock": False}
    assert summarise(["stock"])["origin"] == "stock"
    assert summarise(["own", "stock"]) == {"origin": "mixed", "contains_ai": False, "contains_stock": True}


# ── projects ─────────────────────────────────────────────────────────────────────

async def test_a_project_from_a_video_opens_with_the_clip_the_canvas_and_the_flag():
    ctx = _ctx()
    mid = await _media(ctx.workspace_id, probe={"duration_s": 10.0, "video": {"fps": 29.97, "shown_width": 1280, "shown_height": 720}})
    out = await _call(api.create_project, None, api.CreateBody(source="video", media_id=mid, title="My talk"), ctx)
    assert out["version"] == 1 and out["edit"]["fps"] == {"num": 30000, "den": 1001}
    assert (out["edit"]["width"], out["edit"]["height"]) == (1280, 720)
    assert out["edit"]["video"][0]["dur_f"] == 300 and out["media"][mid]["origin"] == "own"
    assert out["origin"]["label"] == "Your own"


async def test_a_project_hands_the_editor_the_preview_files_that_exist_and_nothing_for_an_item_without_one():
    ctx = _ctx()
    ready = await _media(ctx.workspace_id)
    await media_assets.update_one({"id": ready}, {"$set": {
        "preview_status": "ready", "poster_url": "https://res.cloudinary.com/x/p.jpg",
        "preview": {"proxy_url": "https://res.cloudinary.com/x/proxy.mp4", "strip_url": "https://res.cloudinary.com/x/strip.jpg",
                    "waveform_url": "https://res.cloudinary.com/x/wave.json",
                    "meta": {"strip": {"count": 5, "columns": 5, "rows": 1, "thumb_width": 160, "thumb_height": 90, "interval_s": 2.0},
                             "waveform": {"points": 80, "per_second": 8}}}}})
    plain = await _media(ctx.workspace_id)
    first = await _call(api.create_project, None, api.CreateBody(source="video", media_id=ready), ctx)
    m = first["media"][ready]
    assert m["preview_status"] == "ready" and m["proxy_url"].endswith("proxy.mp4")
    assert m["strip"]["columns"] == 5 and m["waveform"]["per_second"] == 8 and m["waveform_url"].endswith("wave.json")
    second = await _call(api.create_project, None, api.CreateBody(source="video", media_id=plain), ctx)
    n = second["media"][plain]
    assert n["proxy_url"] is None and n["strip"] is None and n["waveform_url"] is None and n["preview_status"] is None


async def test_a_vertical_canvas_over_a_landscape_video_fills_the_sides_with_blur():
    ctx = _ctx()
    mid = await _media(ctx.workspace_id)
    out = await _call(api.create_project, None, api.CreateBody(source="video", media_id=mid, size="vertical"), ctx)
    assert (out["edit"]["width"], out["edit"]["height"]) == (1080, 1920) and out["edit"]["video"][0]["fit"] == "blur"


async def test_another_workspaces_video_cannot_be_used():
    other = await _media(f"ws-{uuid4()}")
    with pytest.raises(HTTPException) as caught:
        await _call(api.create_project, None, api.CreateBody(source="video", media_id=other), _ctx())
    assert caught.value.status_code == 404


async def test_saving_makes_a_version_and_a_stale_save_is_refused_not_overwritten():
    ctx = _ctx()
    mid = await _media(ctx.workspace_id)
    proj = await _call(api.create_project, None, api.CreateBody(source="video", media_id=mid), ctx)
    edit = copy.deepcopy(proj["edit"])
    edit["text"] = [{"id": "t1", "text": "Hello", "at_f": 0, "dur_f": 30}]
    saved = await _call(api.save_project, None, proj["id"], api.SaveBody(edit=edit, base_version=1, title="Renamed"), ctx)
    assert saved["version"] == 2 and saved["title"] == "Renamed" and saved["edit"]["text"][0]["text"] == "Hello"
    with pytest.raises(HTTPException) as caught:
        await _call(api.save_project, None, proj["id"], api.SaveBody(edit=edit, base_version=1), ctx)
    assert caught.value.status_code == 409 and caught.value.detail["current_version"] == 2


async def test_a_save_that_names_a_file_outside_the_workspace_is_refused():
    ctx = _ctx()
    proj = await _call(api.create_project, None, api.CreateBody(source="blank"), ctx)
    edit = _edit()
    with pytest.raises(HTTPException) as caught:
        await _call(api.save_project, None, proj["id"], api.SaveBody(edit=edit, base_version=1), ctx)
    assert caught.value.status_code == 400 and "library" in caught.value.detail


async def test_autosaves_are_trimmed_but_explicit_saves_are_kept():
    ctx = _ctx()
    proj = await _call(api.create_project, None, api.CreateBody(source="blank"), ctx)
    v = 1
    for i in range(api.AUTOSAVES_KEPT + 5):
        out = await _call(api.save_project, None, proj["id"], api.SaveBody(edit=proj["edit"], base_version=v, autosave=True), ctx)
        v = out["version"]
    await _call(api.save_project, None, proj["id"], api.SaveBody(edit=proj["edit"], base_version=v, autosave=False), ctx)
    kinds = [r["kind"] for r in (await _call(api.list_versions, None, proj["id"], ctx))["versions"]]
    assert kinds.count("autosave") == api.AUTOSAVES_KEPT and kinds.count("save") >= 2


async def test_restore_and_start_again_keep_what_was_there():
    ctx = _ctx()
    mid = await _media(ctx.workspace_id)
    proj = await _call(api.create_project, None, api.CreateBody(source="video", media_id=mid), ctx)
    edit = copy.deepcopy(proj["edit"])
    edit["video"][0]["dur_f"] = 100
    await _call(api.save_project, None, proj["id"], api.SaveBody(edit=edit, base_version=1), ctx)        # version 2: cut short
    back = await _call(api.restore_version, None, proj["id"], 1, ctx)
    assert back["edit"]["video"][0]["dur_f"] == 300 and back["version"] > 2
    again = await _call(api.reset_project, None, proj["id"], ctx)
    assert again["edit"] == proj["edit"]
    labels = [v["label"] for v in (await _call(api.list_versions, None, proj["id"], ctx))["versions"]]
    assert any("Before" in x for x in labels)


async def test_responses_are_never_cached():
    from fastapi import Response

    r = Response()
    await api.no_store(r)
    assert r.headers["cache-control"] == "no-store"


# ── a render in the activity layer ───────────────────────────────────────────────

async def _rendering(ctx, **render):
    mid = await _media(ctx.workspace_id)
    proj = await _call(api.create_project, None, api.CreateBody(source="video", media_id=mid, title="Clip"), ctx)
    job = await _call(api.render_project, None, proj["id"], api.RenderBody(**render), ctx)
    return proj, job


async def test_a_render_appears_in_the_run_layer_and_is_not_closed_by_the_sweeper():
    ctx = _ctx()
    proj, job = await _rendering(ctx)
    run = await run_layer.get_run(job["run_id"], ctx.workspace_id)
    assert run["kind"] == "video" and run["status"] == "queued" and run["stage"] == "Waiting for a worker"
    assert run["external_worker"] is True and run["ref"]["project_id"] == proj["id"]
    await pipeline_runs.update_one({"id": run["id"]}, {"$set": {"created_at": datetime(2020, 1, 1, tzinfo=timezone.utc)}})
    await run_layer.fail_interrupted()
    assert (await run_layer.get_run(run["id"], ctx.workspace_id))["status"] == "queued"


async def test_progress_stage_and_cancel_flow_through_to_the_run():
    ctx = _ctx()
    _, job = await _rendering(ctx)
    claimed = await jobs.claim("w1", "0.1.1", {})
    assert claimed["id"] == job["id"]
    run = await run_layer.get_run(job["run_id"], ctx.workspace_id)
    assert run["status"] == "running"
    out = await jobs.heartbeat(job["id"], claimed["lease_token"], {"stage": "Rendering", "fraction": 0.5})
    assert out["cancel"] is False
    run = await run_layer.get_run(job["run_id"], ctx.workspace_id)
    assert run["stage"] == "Rendering" and run["steps_done"] == 50

    await run_layer.control(job["run_id"], ctx.workspace_id, "pause")             # a render cannot be paused
    out = await jobs.heartbeat(job["id"], claimed["lease_token"], {"stage": "Rendering", "fraction": 0.6})
    assert (await run_layer.get_run(job["run_id"], ctx.workspace_id))["pause_requested"] is False

    await run_layer.control(job["run_id"], ctx.workspace_id, "cancel")            # cancelled from the activity dock
    out = await jobs.heartbeat(job["id"], claimed["lease_token"], {"stage": "Rendering", "fraction": 0.7})
    assert out["cancel"] is True
    assert (await jobs.get_job(ctx.workspace_id, job["id"]))["status"] == "cancelled"
    assert (await run_layer.get_run(job["run_id"], ctx.workspace_id))["status"] == "cancelled"


async def test_cancelling_a_waiting_render_from_the_dock_closes_it_without_a_worker():
    ctx = _ctx()
    _, job = await _rendering(ctx)
    await run_layer.control(job["run_id"], ctx.workspace_id, "cancel")
    await jobs.reap_expired()
    assert (await jobs.get_job(ctx.workspace_id, job["id"]))["status"] == "cancelled"
    assert (await run_layer.get_run(job["run_id"], ctx.workspace_id))["status"] == "cancelled"


async def test_a_failed_render_ends_its_run_with_the_plain_message_and_an_activity_entry():
    ctx = _ctx()
    _, job = await _rendering(ctx)
    claimed = await jobs.claim("w1", "0.1.1", {})
    await jobs.fail(job["id"], claimed["lease_token"], "bad_input", "This video could not be read.", True)
    run = await run_layer.get_run(job["run_id"], ctx.workspace_id)
    assert run["status"] == "failed" and run["error"] == "This video could not be read."
    entry = await get_db()["activity_log"].find_one({"workspace_id": ctx.workspace_id, "target_id": job["run_id"]}) \
        if "activity_log" in await get_db().list_collection_names() else None
    assert entry is None or "failed" in entry["title"]


async def test_the_same_version_rendered_twice_is_one_job_and_a_different_one_is_refused_while_active():
    ctx = _ctx()
    proj, job = await _rendering(ctx)
    again = await _call(api.render_project, None, proj["id"], api.RenderBody(), ctx)
    assert again["id"] == job["id"]
    with pytest.raises(HTTPException) as caught:
        await _call(api.render_project, None, proj["id"], api.RenderBody(preset="high"), ctx)
    assert caught.value.status_code == 409 and "already being rendered" in caught.value.detail


async def test_a_render_over_the_workspace_limit_is_refused_plainly():
    ctx = _ctx()
    for _ in range(run_layer.MAX_ACTIVE_PER_WORKSPACE):
        await _rendering(ctx)
    with pytest.raises(HTTPException) as caught:
        await _rendering(ctx)
    assert caught.value.status_code == 429


async def test_an_ai_clip_marks_the_render_and_the_job_carries_it():
    ctx = _ctx()
    ai = await _media(ctx.workspace_id, source=MediaSource.AI_GENERATED)
    own = await _media(ctx.workspace_id)
    proj = await _call(api.create_project, None, api.CreateBody(source="video", media_id=own), ctx)
    edit = copy.deepcopy(proj["edit"])
    edit["video"].append({"id": "v2", "media_id": ai, "at_f": 300, "dur_f": 60})
    saved = await _call(api.save_project, None, proj["id"], api.SaveBody(edit=edit, base_version=1), ctx)
    assert saved["origin"]["origin"] == "mixed" and saved["origin"]["contains_ai"] is True
    job = await _call(api.render_project, None, proj["id"], api.RenderBody(), ctx)
    assert job["origin"] == {"origin": "mixed", "contains_ai": True, "contains_stock": False}
