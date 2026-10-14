"""The audio director: one goal for a recording becomes a validated plan, run through the audio routes' own handlers.

The assets, the database, the handlers, the plan executor, the budget and the decision log are real. Only the calls that would
spend provider quota (speech, transcription, the writing model, storage) are replaced, as the other audio tests do."""
import copy
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.agents.audio import director
from app.agents.runtime import PlanError
from app.api.v1 import audio_assets as audio_module
from app.db.mongo import audio_assets, get_db
from app.shared import jobs as jobs_layer
from tests.test_audio_assets import _generate, _setup, _wav, stubs  # noqa: F401 - fixture reuse


class _Steps:
    """What a job reporter records: each step label, in order."""

    def __init__(self):
        self.labels: list[str] = []

    async def step(self, label: str) -> None:
        self.labels.append(label)


def _payload(asset_id: str, **settings):
    from pydantic import BaseModel

    class P(BaseModel):
        audio_asset_id: str | None = None
        settings: director.DirectorSettings

    return P(audio_asset_id=asset_id, settings=director.DirectorSettings(**settings))


async def _ctx(ws_id: str, user_id: str):
    return await jobs_layer.build_context(ws_id, user_id)


async def _recording(signup_user, stubs, *, words: int = 0, seconds: float = 130.0):
    client, profile, ws_id, brand_id = await _setup(signup_user)
    res = await _generate(client, ws_id, brand_id)
    assert res.status_code == 201, res.text
    asset = res.json()
    if words:
        text = ("focus habits and routines for a calmer working week " * 30).split()[:words]
        step = seconds / max(1, len(text))
        transcript = [{"word": w, "start_s": round(i * step, 2), "end_s": round(i * step + step * 0.8, 2)} for i, w in enumerate(text)]
        await audio_assets.update_one({"id": asset["id"]}, {"$set": {"transcript": transcript}})
    return client, profile, ws_id, asset


@pytest.fixture
def writing(monkeypatch):
    """The writing model: chapters and the written versions. Records what it was asked."""
    seen = {"chapters": 0, "derivatives": []}

    async def structured(prompt, *a, **k):
        seen["chapters"] += 1
        return {"chapters": [{"start_s": 0, "title": "Opening"}, {"start_s": 60, "title": "The main idea"}]}

    async def analysis(**kwargs):
        seen["derivatives"].append(kwargs["raw_input"]["output_type"])
        return {"content": "Key points: focus, habits and routines for a calmer working week.", "quality_scores": {"completeness": 0.9}}

    async def stored_audio(url: str) -> bytes:
        return _wav(70.0)             # real audio bytes stand in for the stored file's download

    async def moments(doc, workspace_id, asset_id):
        from app.models.audio_asset import SuggestedClip

        return [SuggestedClip(start_s=5.0, end_s=25.0, quote="focus and habits", reason="A clear idea in one breath"),
                SuggestedClip(start_s=30.0, end_s=50.0, quote="a calmer working week", reason="A strong closing line")]

    monkeypatch.setattr(audio_module, "call_llm_structured", structured)
    monkeypatch.setattr(audio_module, "_download_media_bytes", stored_audio)
    monkeypatch.setattr(audio_module, "_generate_clip_suggestions", moments)
    monkeypatch.setattr("app.pipelines.audio.orchestrator.run_audio_analysis", analysis)
    return seen


# ── the plan ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_plan_only_names_registered_tools_and_the_same_goal_gives_the_same_plan():
    for goal in director.GOALS:
        steps = director.build_plan(director.DirectorSettings(goal=goal, cleanup={"normalize_loudness": True}))
        assert director.REGISTRY.validate_plan([s.tool for s in steps])
        assert [s.tool for s in steps] == [s.tool for s in director.build_plan(director.DirectorSettings(goal=goal, cleanup={"normalize_loudness": True}))]
    with pytest.raises(PlanError):
        director.REGISTRY.validate_plan(["transcribe", "publish_to_moon"])


def test_each_goal_has_the_steps_it_promises():
    names = lambda **kw: [s.tool for s in director.build_plan(director.DirectorSettings(**kw))]  # noqa: E731
    assert names(goal="show_notes") == ["transcribe", "chapters", "derivative"]
    assert names(goal="publish_ready", outputs=["show_notes", "captions"]) == [
        "transcribe", "chapters", "soundbites", "derivative", "derivative", "sound_checks"]
    assert names(goal="clean_and_check", cleanup={"normalize_loudness": True}) == ["cleanup", "sound_checks"]
    assert "cleanup" in names(goal="publish_ready", cleanup={"normalize_loudness": True})


# ── running it ───────────────────────────────────────────────────────────────────────────────────────────────────────────

async def test_show_notes_runs_every_step_in_order_and_writes_the_result(signup_user, stubs, writing):
    client, profile, ws_id, asset = await _recording(signup_user, stubs, words=240)
    reporter = _Steps()
    out = await director.run_director(await _ctx(ws_id, profile["id"]), _payload(asset["id"], goal="show_notes"), reporter)

    assert [s["tool"] for s in out["steps"]] == ["transcribe", "chapters", "derivative"]
    assert [s["status"] for s in out["steps"]] == ["skipped", "done", "done"]     # it was already transcribed
    assert out["steps"][0]["detail"] == "Already transcribed."
    assert reporter.labels[0] == "Writing out what was said" and "Writing the show notes" in reporter.labels
    saved = await audio_assets.find_one({"id": asset["id"]})
    assert len(saved["chapters"]) == 2 and saved["derivatives"]["show_notes"]["content"].startswith("Key points")
    assert writing["derivatives"] == ["show_notes"]


async def test_a_second_run_does_not_repeat_what_is_already_there(signup_user, stubs, writing):
    client, profile, ws_id, asset = await _recording(signup_user, stubs, words=240)
    ctx = await _ctx(ws_id, profile["id"])
    await director.run_director(ctx, _payload(asset["id"], goal="show_notes"), _Steps())
    again = await director.run_director(ctx, _payload(asset["id"], goal="show_notes"), _Steps())
    assert [s["status"] for s in again["steps"]] == ["skipped", "skipped", "skipped"]
    assert writing["chapters"] == 1 and writing["derivatives"] == ["show_notes"]       # no second model call


async def test_a_recording_without_a_transcript_is_transcribed_first(signup_user, stubs, writing):
    client, profile, ws_id, asset = await _recording(signup_user, stubs, words=0)
    out = await director.run_director(await _ctx(ws_id, profile["id"]), _payload(asset["id"], goal="show_notes"), _Steps())
    assert out["steps"][0]["tool"] == "transcribe" and out["steps"][0]["status"] == "done"
    saved = await audio_assets.find_one({"id": asset["id"]})
    assert [w["word"] for w in saved["transcript"]] == ["hello", "world"]
    # two words are too few to write from: that step says so plainly and the run still finishes
    notes = out["steps"][-1]
    assert notes["tool"] == "derivative" and notes["status"] in ("skipped", "failed")


async def test_a_step_that_fails_is_reported_and_the_rest_carry_on(signup_user, stubs, writing, monkeypatch):
    client, profile, ws_id, asset = await _recording(signup_user, stubs, words=240)

    async def broken(prompt, *a, **k):
        raise RuntimeError("the writing model is busy")
    monkeypatch.setattr(audio_module, "call_llm_structured", broken)
    out = await director.run_director(await _ctx(ws_id, profile["id"]), _payload(asset["id"], goal="show_notes"), _Steps())
    by_tool = {s["tool"]: s for s in out["steps"]}
    assert by_tool["chapters"]["status"] == "failed" and "right now" in by_tool["chapters"]["detail"]
    assert by_tool["derivative"]["status"] == "done"          # an optional step failing does not stop the next one
    assert out["failed"] == 1


async def test_the_budget_stops_a_run_that_would_spend_too_much(signup_user, stubs, writing, monkeypatch):
    client, profile, ws_id, asset = await _recording(signup_user, stubs, words=240)
    from app.agents.runtime import Budget

    original = director.Budget
    monkeypatch.setattr(director, "Budget", lambda **kw: Budget(llm_calls=1, provider_calls=0, worker_jobs=0))
    out = await director.run_director(
        await _ctx(ws_id, profile["id"]), _payload(asset["id"], goal="show_notes", outputs=["show_notes", "captions", "summary"]), _Steps())
    statuses = [s["status"] for s in out["steps"]]
    assert statuses.count("stopped") >= 1 and "limit" in next(s["detail"] for s in out["steps"] if s["status"] == "stopped")
    assert director.Budget is not original


async def test_every_step_writes_a_decision_and_publish_ready_hands_the_signoff_back(signup_user, stubs, writing):
    client, profile, ws_id, asset = await _recording(signup_user, stubs, words=240)
    out = await director.run_director(await _ctx(ws_id, profile["id"]), _payload(asset["id"], goal="publish_ready"), _Steps())
    assert out["needs_signoff"] is True
    rows = await get_db()["agent_decisions"].find({"workspace_id": ws_id, "agent": "audio"}).to_list(50)
    steps = {r["step"] for r in rows}
    assert {"transcribe", "chapters", "derivative", "sound_checks", "signoff"} <= steps
    signoff = next(r for r in rows if r["step"] == "signoff")
    assert signoff["outcome"] == "needs_decision" and signoff["evidence"]["audio_asset_id"] == asset["id"]


async def test_a_recording_that_is_not_in_the_workspace_is_refused(signup_user, stubs, writing):
    client, profile, ws_id, asset = await _recording(signup_user, stubs, words=240)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as caught:
        await director.run_director(await _ctx(ws_id, profile["id"]), _payload(str(uuid4()), goal="show_notes"), _Steps())
    assert caught.value.status_code == 404


def test_the_director_is_a_registered_job_action_with_the_audio_permission_and_page_link():
    from app.shared.job_actions import register_all

    register_all()
    action = jobs_layer.ACTIONS["audio.director"]
    assert action.kind == "audio" and action.permission == "edit_content" and action.gated is True
    assert action.result_href({"asset_id": "abc"}) == "/dashboard/pipelines/audio?asset=abc"
    payload = action.payload_model(audio_asset_id="a1")
    assert payload.settings.goal == "publish_ready" and payload.settings.outputs == ["show_notes"]


# ── audio generation: a script becomes a recording, then the rest of the plan runs on it ─────────────────────────────────

async def test_make_episode_voices_the_script_and_carries_on_with_the_new_recording(signup_user, stubs, writing):
    from app.api.v1.audio_assets import GenerateAudioAssetRequest  # noqa: F401 - the shape the narration request takes

    from app.models.audio_asset import TranscriptWord

    # the narration comes back with word timing (as the real speech service gives), so there is a transcript to write from
    spoken = ("welcome to the show where we talk about focus habits and the routines that make a working week calmer " * 2).split()
    stubs["timed_words"] = [TranscriptWord(word=w, start_s=i * 0.2, end_s=i * 0.2 + 0.15) for i, w in enumerate(spoken)]
    client, profile, ws_id, brand_id = await _setup(signup_user)
    out = await director.run_director(
        await _ctx(ws_id, profile["id"]),
        _payload(None, goal="make_episode", narration={"title": "Episode 7", "brand_id": brand_id, "script": "Welcome to the show."}),
        _Steps(),
    )
    assert out["steps"][0]["tool"] == "narrate" and out["steps"][0]["status"] == "done"
    assert out["asset_id"]                                             # the recording the narration made
    saved = await audio_assets.find_one({"id": out["asset_id"]})
    assert saved["source_type"] == "script_tts" and saved["script"] == "Welcome to the show."
    assert stubs["synth"][0]["text"] == "Welcome to the show."          # the real narration path ran
    assert out["needs_signoff"] is True


async def test_make_episode_without_a_script_is_refused_plainly(signup_user, stubs, writing):
    from fastapi import HTTPException

    client, profile, ws_id, brand_id = await _setup(signup_user)
    with pytest.raises(HTTPException) as caught:
        await director.run_director(await _ctx(ws_id, profile["id"]), _payload(None, goal="make_episode"), _Steps())
    assert caught.value.status_code == 400 and "script" in caught.value.detail


async def test_a_goal_that_works_on_a_recording_needs_one(signup_user, stubs, writing):
    from fastapi import HTTPException

    client, profile, ws_id, brand_id = await _setup(signup_user)
    with pytest.raises(HTTPException) as caught:
        await director.run_director(await _ctx(ws_id, profile["id"]), _payload(None, goal="show_notes"), _Steps())
    assert caught.value.status_code == 400


def test_the_plan_for_make_episode_starts_with_narration():
    steps = director.build_plan(director.DirectorSettings(goal="make_episode", narration={"script": "x"}))
    assert steps[0].tool == "narrate" and steps[0].required is True
