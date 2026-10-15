"""Video events on the workspace bus, and Remy's note to the member when a video or a recipe stops on a check."""
from uuid import uuid4

from app.agents.personal import video_handlers
from app.db.mongo import activity_entries, personal_signals, workspace_events
from app.models.agent_events import EventType, PAYLOAD_MODELS, PIPELINE_SCOPED_EVENTS
from app.video import activity


def test_every_video_event_is_pipeline_scoped_and_has_a_payload_model():
    names = {"analysis_done", "script_ready", "render_done", "qc_failed", "cover_ready", "automation_paused"}
    for name in names:
        et = EventType(f"video.{name}")
        assert et in PIPELINE_SCOPED_EVENTS and et in PAYLOAD_MODELS


async def test_a_video_event_is_stored_once_with_its_pipeline_and_a_redelivery_is_ignored():
    ws = f"ws-{uuid4()}"

    await activity.emit("render_done", ws, "u1", "job-1", project_id="p1", job_id="job-1", duration_s=12.5, origin="own")
    await activity.emit("render_done", ws, "u1", "job-1", project_id="p1", job_id="job-1", duration_s=12.5, origin="own")

    rows = await workspace_events.find({"workspace_id": ws}).to_list(length=5)
    assert len(rows) == 1 and rows[0]["event_type"] == "video.render_done" and rows[0]["pipeline_type"] == "video"
    assert rows[0]["payload"]["project_id"] == "p1" and rows[0]["payload"]["duration_s"] == 12.5


async def test_a_bad_payload_is_dropped_quietly_and_never_raises():
    ws = f"ws-{uuid4()}"
    await activity.emit("qc_failed", ws, "u1", "job-9", project_id="p1", duration_s="not a number")
    assert await workspace_events.count_documents({"workspace_id": ws}) == 0


async def test_a_failed_render_check_tells_the_member_once_in_plain_words():
    ws, user = f"ws-{uuid4()}", f"u-{uuid4()}"
    event = {"event_id": "e1", "event_type": "video.qc_failed", "workspace_id": ws, "actor_user_id": user,
             "payload": {"job_id": "job-7", "project_id": "p1", "detail": "The sound was too quiet."}}

    assert await video_handlers.handle(event) is True
    assert await video_handlers.handle(event) is False             # redelivered: no second note

    [signal] = await personal_signals.find({"workspace_id": ws}).to_list(length=5)
    assert signal["signal_type"] == "video_check_failed" and signal["user_id"] == user
    assert "The sound was too quiet." in signal["member_message"] and "Nothing was published" in signal["member_message"]
    [row] = await activity_entries.find({"workspace_id": ws, "member_user_id": user}).to_list(length=5)
    assert row["lane"] == "active" and row["title"] == "A video stopped on a check"


async def test_a_stopped_recipe_tells_the_member_and_unrelated_events_are_left_alone():
    ws, user = f"ws-{uuid4()}", f"u-{uuid4()}"
    stopped = {"event_id": "e2", "event_type": "video.automation_paused", "workspace_id": ws, "actor_user_id": user,
               "payload": {"recipe_id": "r1", "title": "Weekly clips", "detail": "the captions do not sound like your voice"}}

    assert await video_handlers.handle(stopped) is True
    assert await video_handlers.handle({"event_type": "video.render_done", "workspace_id": ws, "actor_user_id": user, "payload": {"job_id": "j"}}) is False
    assert await video_handlers.handle({**stopped, "actor_user_id": "system", "event_id": "e3"}) is False      # no person to tell

    [signal] = await personal_signals.find({"workspace_id": ws}).to_list(length=5)
    assert "Weekly clips stopped on its own" in signal["member_message"]
