"""Tests for Module 2 Stage 5 (Batch Mode) — run_batch_pipeline streamed
through a shared EventEmitter, as the SSE route now does.

Batch mode used to silently generate one ordinary post instead of the
promised N days, because the SSE route only ever called run_text_pipeline
and never imported run_batch_pipeline at all. This covers the actual
orchestration fix: every day shares one emitter and streams over what
would be one SSE connection, each day still gets its own real persisted
session/piece, and — the part that's easy to get wrong — only the very
last day may fire pipeline_complete, since that event is what closes the
SSE stream on the frontend; firing it after day one would cut the other
N-1 days off entirely.

Runs the real pipeline for each day with the LLM mocked — never a real
Groq call. batch_angles' own LLM call isn't mocked with a specific
response on purpose: call_llm_structured's default mock_llm response is
{}, and score_hook-style callers already fall back sanely on an empty
dict — here angle_result.get("angles", [topic_cluster] * days) exercises
that exact fallback, which is itself real batch_angles.py behaviour worth
confirming rather than working around.
"""

from uuid import uuid4

from app.agents.text.event_emitter import EventEmitter
from app.db.mongo import brand_profiles, content_pieces, content_sessions
from app.models.text import Platform
from app.pipelines.text.orchestrator import run_batch_pipeline


async def _drain(emitter: EventEmitter) -> list[dict]:
    events = []
    while True:
        event = await emitter.queue.get()
        if event is EventEmitter.DONE:
            break
        events.append(event)
    return events


async def test_batch_pipeline_streams_all_days_over_one_emitter(mock_llm):
    workspace_id = str(uuid4())
    brand_id = str(uuid4())
    user_id = str(uuid4())
    outer_session_id = str(uuid4())

    await brand_profiles.insert_one({
        "id": brand_id,
        "workspace_id": workspace_id,
        "brand_type": "Person",
        "is_complete": True,
        "identity": {"name": "Test", "role": "Founder"},
        "voice_tone": {"tones": ["direct"], "humor": "Subtle", "emoji": "Sometimes", "style": "punchy"},
        "manual_data": {"openers": [], "closers": [], "phrases": [], "banned_words": []},
        "audience": {"reading_level": "General", "knowledge_base": "Beginner", "primary_pain_point": "time"},
    })

    class Extras:
        hook_variations = False
        hashtags = False
        auto_cta = False
        seo_meta = False
        grammar_check = False
        plagiarism_check = False
        avoid_blacklist = False
        pdf_export = False

    mock_llm.set_plain("Real batch-day content, distinct per day in principle.")

    emitter = EventEmitter()
    days = 3
    results = await run_batch_pipeline(
        topic_cluster="Async teams ship faster",
        platforms=[Platform.LINKEDIN],
        brand_id=brand_id,
        workspace_id=workspace_id,
        user_id=user_id,
        extras=Extras(),
        days=days,
        emitter=emitter,
        outer_session_id=outer_session_id,
    )
    events = await _drain(emitter)

    assert len(results) == days

    output_complete_events = [e for e in events if e["type"] == "output_complete"]
    assert len(output_complete_events) == days

    # Every day's batch_day_index is present, correct, and distinct.
    day_indexes = sorted(e["data"]["batch_day_index"] for e in output_complete_events)
    assert day_indexes == list(range(days))

    # Only one pipeline_complete for the whole batch, not one per day —
    # confirms emit_completion=False actually suppressed the per-day ones.
    complete_events = [e for e in events if e["type"] == "pipeline_complete"]
    assert len(complete_events) == 1
    assert complete_events[0]["data"]["session_id"] == outer_session_id
    assert complete_events[0]["data"]["total_pieces"] == days

    # Every day's piece_id is real and distinct — persisted independently,
    # not just present in the response.
    piece_ids = [e["data"]["piece_id"] for e in output_complete_events]
    assert all(piece_ids)
    assert len(set(piece_ids)) == days

    for piece_id in piece_ids:
        piece = await content_pieces.find_one({"piece_id": piece_id})
        assert piece is not None
        assert piece["workspace_id"] == workspace_id

    # Each day got its own session document too — a real, independent
    # generation run, not one shared/conflated session across all 3 days.
    session_ids = {
        (await content_pieces.find_one({"piece_id": pid}))["session_id"]
        for pid in piece_ids
    }
    assert len(session_ids) == days
    for sid in session_ids:
        session_doc = await content_sessions.find_one({"session_id": sid})
        assert session_doc is not None
        assert session_doc["workspace_id"] == workspace_id


async def _minimal_batch_kwargs(mock_llm, workspace_id: str, brand_id: str, user_id: str) -> dict:
    await brand_profiles.insert_one({
        "id": brand_id,
        "workspace_id": workspace_id,
        "brand_type": "Person",
        "is_complete": True,
        "identity": {"name": "Test", "role": "Founder"},
        "voice_tone": {"tones": ["direct"], "humor": "Subtle", "emoji": "Sometimes", "style": "punchy"},
        "manual_data": {"openers": [], "closers": [], "phrases": [], "banned_words": []},
        "audience": {"reading_level": "General", "knowledge_base": "Beginner", "primary_pain_point": "time"},
    })
    mock_llm.set_plain("Real batch-day content.")

    class Extras:
        hook_variations = False
        hashtags = False
        auto_cta = False
        seo_meta = False
        grammar_check = False
        plagiarism_check = False
        avoid_blacklist = False
        pdf_export = False

    return {"brand_id": brand_id, "workspace_id": workspace_id, "user_id": user_id, "extras": Extras()}


async def test_batch_pipeline_varies_platforms_per_day(mock_llm):
    workspace_id, brand_id, user_id = str(uuid4()), str(uuid4()), str(uuid4())
    kwargs = await _minimal_batch_kwargs(mock_llm, workspace_id, brand_id, user_id)

    mock_llm.set_structured({"angles": ["Day one angle", "Day two angle"]})

    results = await run_batch_pipeline(
        topic_cluster="Async teams ship faster",
        platforms=[Platform.LINKEDIN],
        platforms_by_day=[[Platform.LINKEDIN], [Platform.LINKEDIN, Platform.TWITTER]],
        days=2,
        **kwargs,
    )

    assert len(results) == 2
    assert len(results[0].pieces) == 1
    assert len(results[1].pieces) == 2


async def test_batch_pipeline_on_day_complete_callback_fires_per_day(mock_llm):
    workspace_id, brand_id, user_id = str(uuid4()), str(uuid4()), str(uuid4())
    kwargs = await _minimal_batch_kwargs(mock_llm, workspace_id, brand_id, user_id)

    mock_llm.set_structured({"angles": ["Angle A", "Angle B", "Angle C"]})

    seen: list[tuple[int, object]] = []

    async def on_day_complete(day_index, result):
        seen.append((day_index, result))

    days = 3
    results = await run_batch_pipeline(
        topic_cluster="Async teams ship faster",
        platforms=[Platform.LINKEDIN],
        days=days,
        on_day_complete=on_day_complete,
        **kwargs,
    )

    assert len(seen) == days
    assert [i for i, _ in seen] == list(range(days))
    assert [r for _, r in seen] == results
