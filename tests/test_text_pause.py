"""Pause, resume and cancel of a text run by the member. No network and no database."""
import asyncio

import pytest

from app.agents.text.event_emitter import EventEmitter
from app.api.v1 import text_stream


def _types(emitter: EventEmitter) -> list[str]:
    out = []
    while not emitter.queue.empty():
        item = emitter.queue.get_nowait()
        out.append("DONE" if item is EventEmitter.DONE else item["type"])
    return out


async def test_a_run_that_was_not_paused_goes_straight_through():
    emitter = EventEmitter()
    await asyncio.wait_for(emitter.pause_point(), timeout=1)
    assert _types(emitter) == []


async def test_a_paused_run_waits_at_the_next_step_and_goes_on_when_resumed():
    emitter = EventEmitter()
    emitter.request_pause()
    assert emitter.is_paused
    waiting = asyncio.create_task(emitter.pause_point())
    await asyncio.sleep(0.05)
    assert not waiting.done()
    emitter.request_resume()
    await asyncio.wait_for(waiting, timeout=1)
    assert _types(emitter) == ["pipeline_paused", "pipeline_resumed"] and not emitter.is_paused


async def test_cancel_ends_the_run_at_the_next_step_tells_the_client_once_and_keeps_finished_posts():
    emitter = EventEmitter()
    emitter.completed_platforms.append("LinkedIn")
    emitter.request_cancel()
    with pytest.raises(asyncio.CancelledError):
        await emitter.pause_point()
    with pytest.raises(asyncio.CancelledError):  # another platform's step reaches the same point
        await emitter.pause_point()
    events = []
    while not emitter.queue.empty():
        events.append(emitter.queue.get_nowait())
    types = ["DONE" if e is EventEmitter.DONE else e["type"] for e in events]
    assert types == ["pipeline_cancelled", "DONE"]
    assert events[0]["data"]["completed"] == ["LinkedIn"]


async def test_a_cancel_while_paused_wakes_the_wait_and_ends_the_run():
    emitter = EventEmitter()
    emitter.request_pause()
    waiting = asyncio.create_task(emitter.pause_point())
    await asyncio.sleep(0.05)
    emitter.request_cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(waiting, timeout=1)
    assert "pipeline_cancelled" in _types(emitter)


async def test_a_pause_after_a_cancel_does_nothing():
    emitter = EventEmitter()
    emitter.request_cancel()
    emitter.request_pause()
    assert not emitter.is_paused


async def test_controls_sent_from_another_server_are_applied_by_the_one_that_owns_the_run(monkeypatch):
    emitter = EventEmitter()
    monkeypatch.setitem(text_stream._active_sessions, "s1", (emitter, "w1"))
    assert await text_stream._apply_local_resume("s1", "w1", "__pause__") is True and emitter.is_paused
    assert await text_stream._apply_local_resume("s1", "w1", "__resume__") is True and not emitter.is_paused
    assert await text_stream._apply_local_resume("s1", "w1", "__cancel__") is True
    assert await text_stream._apply_local_resume("s1", "other-workspace", "__pause__") is False
