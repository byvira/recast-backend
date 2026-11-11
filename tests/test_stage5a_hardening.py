"""Hardening and language fixes for Remy, Odette and the Activity layer: a language change cannot use up the model's allowance, the daily note
is built from fixed sentences, Remy's drift notes read in any language, Odette's events are only acknowledged once handled and its lock is
taken in one write, Remy's weekly check skips people who left and does not repeat, and translated free text is checked and expires.
No model is called: the translator is a stand-in."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.agents.personal import signals as remy_signals
from app.agents.personal import nodes as remy_nodes
from app.agents.supervisor import personas, ticks
from app.db.mongo import activity_entries, agent_worker_state, get_db, member_personas, personal_signals, workspace_members, workspaces
from app.shared import localized_strings as LS
from app.shared.activity import i18n
from tests.conftest import create_workspace


class Translator:
    """Stands in for the model: counts how many translations were asked for."""

    def __init__(self):
        self.calls = []

    async def __call__(self, key, language, english):
        if language == "en":
            return english, True
        self.calls.append(english)
        return f"[{language}] {english}", True


@pytest.fixture
def translator(monkeypatch):
    t = Translator()
    monkeypatch.setattr(LS, "_translate", t)
    return t


def _unique(text: str) -> str:
    return f"{text} {uuid4().hex[:8]}"


# ---- a cap on new translations -----------------------------------------------------------------------------------------------------------
async def test_a_piece_of_work_can_be_given_a_share_of_new_translations_and_cached_ones_are_free(translator):
    words = [_unique("Sentence") for _ in range(4)]
    with LS.translation_budget(2) as budget:
        out = [await LS.get_localized_string(f"test.budget.{w}", "hi", w) for w in words]
        assert out[0].startswith("[hi]") and out[1].startswith("[hi]")
        assert out[2] == words[2] and out[3] == words[3], "past the share the English stands in"
        assert budget.refused is True and len(translator.calls) == 2
        # a translation already kept costs nothing and is not counted
        assert (await LS.get_localized_string(f"test.budget.{words[0]}", "hi", words[0])).startswith("[hi]")
        assert len(translator.calls) == 2
    # the English that stood in was not kept: with room again it is translated
    assert (await LS.get_localized_string(f"test.budget.{words[2]}", "hi", words[2])).startswith("[hi]")
    # English is never counted
    with LS.translation_budget(0) as budget:
        assert await LS.get_localized_string("test.budget.en", "en", "Hello") == "Hello"
        assert budget.refused is False


async def test_a_language_change_translates_the_newest_rows_first_and_stops_at_its_share(signup_user, translator, monkeypatch):
    client, _ = await signup_user()
    ws = await create_workspace(client, f"Relocalize {uuid4().hex[:6]}")
    now = datetime.now(timezone.utc)
    title_template = _unique("Finished {n}")
    for i in range(5):
        await activity_entries.insert_one({
            "_id": f"relocalize:{ws}:{i}", "workspace_id": ws, "visibility": "workspace", "lane": "passive", "category": "content_generated",
            "occurred_at": now - timedelta(minutes=i), "title": "x", "description": "y", "status": "success",
            "i18n": {"language": "en", "sources": {"title": {"$t": title_template, "$c": {"n": str(i)}}, "description": {"$s": _unique("A one-off reason")}}},
        })
    await workspaces.update_one({"id": ws}, {"$set": {"language": "hi"}})
    monkeypatch.setattr(i18n, "RELOCALIZE_NEW_TRANSLATIONS", 3)

    changed = await i18n.relocalize(ws)
    rows = {r["_id"]: r async for r in activity_entries.find({"workspace_id": ws})}
    assert changed == 2, "the template is translated once (1) and two one-off reasons (2) use the share of 3"
    done = [rid for rid, r in rows.items() if r["i18n"]["language"] == "hi"]
    assert sorted(done) == [f"relocalize:{ws}:0", f"relocalize:{ws}:1"], "the newest rows come first"
    assert all(rows[f"relocalize:{ws}:{i}"]["i18n"]["language"] == "en" for i in (2, 3, 4)), "the rest keep their earlier language, not half in English"
    assert len(translator.calls) == 3


# ---- translated free text is checked and expires -----------------------------------------------------------------------------------------
def test_an_answer_far_longer_than_its_source_or_with_a_new_link_is_not_a_translation():
    source = "Your file could not be read."
    assert LS._reject_reason("फ़ाइल पढ़ी नहीं जा सकी।", source, set(), False) is None
    assert "longer" in (LS._reject_reason("x" * 400, source, set(), False) or "")
    assert "link" in (LS._reject_reason("Open https://evil.example now", source, set(), False) or "")
    assert LS._reject_reason("See https://ekavo.example/help", "See https://ekavo.example/help", set(), False) is None, "a link already in the text is fine"


async def test_free_text_translations_are_marked_to_expire_and_fixed_sentences_are_not(translator):
    free_key, fixed_key = f"activity.s.{uuid4().hex}", f"activity.t.{uuid4().hex}"
    await LS.get_localized_string(free_key, "ta", _unique("One-off reason"))
    await LS.get_localized_string(fixed_key, "ta", _unique("A fixed sentence"))
    coll = get_db()["localized_strings"]
    assert (await coll.find_one({"key": free_key, "language": "ta"}))["expires_free"] is True
    assert (await coll.find_one({"key": fixed_key, "language": "ta"}))["expires_free"] is False


# ---- Remy's drift note in any language ---------------------------------------------------------------------------------------------------
async def test_a_drift_note_phrase_reads_in_the_members_language_whatever_it_is(translator):
    assert await remy_nodes._hint_phrase("longer", "en") == "much longer sentences"
    assert await remy_nodes._hint_phrase("longer", "ta") == remy_nodes._REGISTER_HINT_PHRASES["ta"]["longer"], "hand-written languages need no model"
    assert translator.calls == []
    assert (await remy_nodes._hint_phrase("longer", "fr")).startswith("[fr] much longer sentences")
    assert (await remy_nodes._hint_phrase("shorter", "ta+en")).startswith("[ta+en]"), "a mix such as Tanglish is translated too"
    assert (await remy_nodes._hint_phrase("fallback", "ko")).startswith("[ko] reads differently"), "the fallback line is translated even where the table has no entry"


async def test_a_phrase_that_cannot_be_translated_falls_back_to_english(monkeypatch):
    monkeypatch.setattr(LS, "_translate", AsyncMock(side_effect=RuntimeError("model down")))
    assert await remy_nodes._hint_phrase("more_formal", f"zz{uuid4().hex[:4]}") == "more formal"


# ---- Odette's events are acknowledged only once handled ----------------------------------------------------------------------------------
def _id(ms_ago: int, n: int) -> str:
    return f"{int((datetime.now(timezone.utc).timestamp() * 1000) - ms_ago)}-{n}"


class FakeStream:
    """The part of a Redis stream with a consumer group that the ticks use: new events, events delivered but not acknowledged, and acks."""

    def __init__(self, events):
        self.events = events
        self.delivered: set[str] = set()
        self.acked: set[str] = set()

    async def xgroup_create(self, *a, **k):
        return None

    @staticmethod
    def _key(entry_id: str):
        ms, seq = entry_id.split("-")
        return int(ms), int(seq)

    async def xreadgroup(self, group, consumer, streams, count=None, block=None):
        cursor = next(iter(streams.values()))
        if cursor == ">":
            fresh = [e for e in self.events if e[0] not in self.delivered][:count]
            self.delivered.update(e[0] for e in fresh)
            return [("events", fresh)] if fresh else []
        floor = self._key("0-0") if cursor == "0" else self._key(cursor)
        pending = [e for e in self.events if e[0] in self.delivered and e[0] not in self.acked and self._key(e[0]) > floor][:count]
        return [("events", pending)] if pending else []

    async def xack(self, stream, group, *ids):
        self.acked.update(ids)


def _event(entry_id, ws, kind="content.published"):
    return (entry_id, {"workspace_id": ws, "event_type": kind})


async def test_events_are_not_acknowledged_until_the_workspace_was_handled():
    a1, a2, sig, b1 = _id(1000, 1), _id(900, 2), _id(800, 3), _id(700, 4)
    stream = FakeStream([_event(a1, "ws-a"), _event(a2, "ws-a"), _event(sig, "ws-a", "assistant.signal"), _event(b1, "ws-b")])
    touched = await ticks._drain_group(stream, "supervisor_rules")
    assert touched == {"ws-a": [a1, a2], "ws-b": [b1]}
    assert stream.acked == {sig}, "an event nothing is done for is acknowledged at once; the rest wait"
    # nobody acknowledged them (the check failed): the next read brings the same events back
    again = await ticks._drain_group(stream, "supervisor_rules")
    assert again == {"ws-a": [a1, a2], "ws-b": [b1]}
    await ticks._ack(stream, "supervisor_rules", [a1, a2])
    assert await ticks._drain_group(stream, "supervisor_rules") == {"ws-b": [b1]}


async def test_an_event_nobody_could_handle_for_an_hour_is_given_up_on():
    old, fresh = _id(2 * 60 * 60 * 1000, 1), _id(1000, 2)
    stream = FakeStream([_event(old, "ws-a"), _event(fresh, "ws-b")])
    assert await ticks._drain_group(stream, "supervisor_rules") == {"ws-b": [fresh]}
    assert old in stream.acked


async def test_the_rules_tick_acknowledges_what_worked_and_tries_what_failed_again(monkeypatch):
    a, b = _id(500, 1), _id(400, 2)
    stream = FakeStream([_event(a, "ws-a"), _event(b, "ws-b")])
    monkeypatch.setattr(ticks, "get_redis", AsyncMock(return_value=stream))
    handled = []
    broken = {"ws-a"}

    async def rules(ws):
        if ws in broken:
            raise RuntimeError("a database blip")
        handled.append(ws)
        return []

    monkeypatch.setattr(ticks, "run_rules_for_workspace", rules)
    await ticks.supervisor_rules_tick({})
    assert handled == ["ws-b"] and stream.acked == {b}, "the failed workspace's event stays unacknowledged"
    broken.clear()
    await ticks.supervisor_rules_tick({})
    assert handled == ["ws-b", "ws-a"] and stream.acked == {a, b}, "the next tick tried it again, and now it is done"


# ---- Odette's lock ----------------------------------------------------------------------------------------------------------------------
async def test_two_reasoning_passes_for_one_workspace_cannot_both_start(monkeypatch):
    ws = f"ws-lock-{uuid4().hex[:6]}"
    await agent_worker_state.delete_many({"_id": f"supervisor:{ws}"})
    ran = []
    release = asyncio.Event()

    async def slow(workspace_id, trigger, **inputs):
        ran.append(trigger)
        await release.wait()  # the pass that got the lock holds it until the others have been turned away
        return {"persisted": {"insight_ids": [], "flag_ids": [], "notification_ids": []}}

    monkeypatch.setattr(ticks, "_gather_inputs", AsyncMock(return_value={}))
    monkeypatch.setattr(ticks, "run_supervisor", slow)
    tasks = [asyncio.create_task(ticks._run_reasoning_pass(ws, f"try-{i}")) for i in range(5)]

    async def four_turned_away():
        while sum(1 for t in tasks if t.done()) < 4:
            await asyncio.sleep(0.05)

    await asyncio.wait_for(four_turned_away(), timeout=30)
    release.set()
    answers = await asyncio.gather(*tasks)
    assert len(ran) == 1, f"only one pass started: {ran} {answers}"
    assert sum(1 for a in answers if a == {"skipped": "locked"}) == 4
    # afterwards the lock is clear and a later pass runs
    assert (await ticks._run_reasoning_pass(ws, "later")).get("skipped") is None and ran[-1] == "later"
    await agent_worker_state.delete_many({"_id": f"supervisor:{ws}"})


async def test_a_lock_that_has_run_out_does_not_block_the_next_pass(monkeypatch):
    ws = f"ws-stale-{uuid4().hex[:6]}"
    await agent_worker_state.update_one({"_id": f"supervisor:{ws}"}, {"$set": {"lock_until": datetime.now(timezone.utc) - timedelta(seconds=5)}}, upsert=True)
    monkeypatch.setattr(ticks, "_gather_inputs", AsyncMock(return_value={}))
    monkeypatch.setattr(ticks, "run_supervisor", AsyncMock(return_value={"persisted": {"insight_ids": [], "flag_ids": [], "notification_ids": []}}))
    assert (await ticks._run_reasoning_pass(ws, "tick")).get("skipped") is None
    await agent_worker_state.delete_many({"_id": f"supervisor:{ws}"})


# ---- Remy's weekly check ----------------------------------------------------------------------------------------------------------------
async def test_the_volume_check_skips_people_who_left_and_asks_for_one_signal_a_day(monkeypatch):
    ws = f"ws-vol-{uuid4().hex[:6]}"
    stayed, left, low = f"u-stayed-{uuid4().hex[:4]}", f"u-left-{uuid4().hex[:4]}", f"u-low-{uuid4().hex[:4]}"
    for user, mean in ((stayed, 50.0), (left, 50.0), (low, 0.0)):
        await member_personas.insert_one({"workspace_id": ws, "user_id": user, "volume_stats": {"mean": mean, "daily_counts": {}}})
    await workspace_members.insert_one({"workspace_id": ws, "user_id": stayed, "status": "active", "role": "editor"})
    await workspace_members.insert_one({"workspace_id": ws, "user_id": left, "status": "removed", "role": "editor"})
    sent = []

    async def emit(**kwargs):
        sent.append(kwargs)
        return "id"

    monkeypatch.setattr(ticks, "emit_signal", emit)
    monkeypatch.setattr(ticks, "remy_message", AsyncMock(return_value="Remy says"))
    monkeypatch.setattr(ticks, "user_language", AsyncMock(return_value="en"))
    monkeypatch.setattr(ticks, "workspace_language", AsyncMock(return_value="en"))
    monkeypatch.setattr(ticks, "_platform_volume_sweep", AsyncMock(return_value=0))

    await ticks.personal_volume_sweep({})
    mine = [s for s in sent if s["workspace_id"] == ws]
    assert [s["user_id"] for s in mine] == [stayed], "the person who left and the person who rarely posted are not told"
    assert mine[0]["dedupe_key"] == f"volume_drop:{ws}:{stayed}:{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
    await member_personas.delete_many({"workspace_id": ws})
    await workspace_members.delete_many({"workspace_id": ws})


async def test_a_signal_with_the_same_key_is_made_once_even_when_asked_twice_at_once(monkeypatch):
    shown = []

    async def project(doc):
        shown.append(doc["_id"])

    monkeypatch.setattr("app.shared.activity.project_remy_signal", project)
    monkeypatch.setattr(remy_signals, "emit_event", AsyncMock())
    ws, user = f"ws-sig-{uuid4().hex[:6]}", "u1"
    key = f"volume_drop:{ws}:{user}:2026-11-11"
    args = dict(workspace_id=ws, user_id=user, pipeline_type=None, signal_type="volume_drop", severity="medium",
                metric={"name": "zero_days", "value": 3.0, "baseline": 2.0, "threshold": 3.0},
                window={"kind": "rolling", "n": 14}, evidence_refs=[], member_message="m", supervisor_note="n")
    ids = await asyncio.gather(*[remy_signals.emit_signal(**args, dedupe_key=key) for _ in range(5)])
    assert await personal_signals.count_documents({"dedupe_key": key}) == 1
    assert len(set(ids)) == 1 and len(shown) == 1, "one signal, shown once"
    # signals without a key behave as before
    await remy_signals.emit_signal(**args)
    await remy_signals.emit_signal(**args)
    assert await personal_signals.count_documents({"workspace_id": ws}) == 3
    await personal_signals.delete_many({"workspace_id": ws})


# ---- Odette's flag headline ------------------------------------------------------------------------------------------------------------
def test_every_kind_of_flag_has_a_plain_headline_and_an_unknown_one_never_shows_the_rule_name():
    kinds = ["assistant_signal_storm", "brand_voice_instability", "daily_publish_cap", "member_churn", "platform_capability_drift", "rbac_violation",
             "tier_seat_exceeded", "video_renders_failing", "video_minutes_high", "video_storage_high", "video_automation_stopped",
             "video_auto_publish_on", "video_connection_expiring", "video_voice_changed", "llm_anomaly"]
    for kind in kinds:
        title = personas.flag_title(kind)
        assert "_" not in title and "flagged" not in title.lower() and title[0].isupper(), kind
    assert personas.flag_title("some_new_rule") == "Odette flagged something to look at"
