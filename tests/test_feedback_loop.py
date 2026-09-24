"""The post-publish feedback loop (app.agents.feedback).

Findings must come from real 24h engagement, meet the evidence bar (≥3 posts
a side, ≥1.5×), land in the right lane for the right audience, and respect
what people did with earlier suggestions (two dismissals silence a variant;
no repeats within 30 days; at most one per week).
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.agents.feedback import sweep
from app.agents.feedback.patterns import find_patterns
from app.db.mongo import activity_entries, content_pieces, personal_signals, post_metric_checkpoints, workspace_insights
from tests.conftest import create_workspace, signup_new_user


@pytest.fixture(autouse=True)
def _no_translation_llm(monkeypatch):
    import app.shared.localized_strings as ls

    async def _identity(key, language, english_template):
        return english_template, False

    monkeypatch.setattr(ls, "_translate", _identity)


def _sample(platform="linkedin", engagement=1.0, words=120, question=False, hour=9):
    return {
        "platform": platform, "engagement": engagement, "words": words,
        "question_opener": question,
        "published_at": datetime(2026, 9, 1, hour, 0, tzinfo=timezone.utc),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Pure analysis
# ─────────────────────────────────────────────────────────────────────────────

def test_platform_finding_needs_enough_posts_and_a_real_gap():
    strong = [_sample("threads", 6.0) for _ in range(3)] + [_sample("linkedin", 2.0) for _ in range(3)]
    [finding] = [f for f in find_patterns(strong) if f.variant == "platform"]
    assert finding.params == {"winner": "Threads"}
    assert finding.ratio == 3.0

    too_few = [_sample("threads", 6.0) for _ in range(2)] + [_sample("linkedin", 2.0) for _ in range(5)]
    assert not [f for f in find_patterns(too_few) if f.variant == "platform"]

    too_close = [_sample("threads", 2.8) for _ in range(3)] + [_sample("linkedin", 2.0) for _ in range(3)]
    assert not [f for f in find_patterns(too_close) if f.variant == "platform"]


def test_opener_and_length_findings_go_whichever_way_the_data_says():
    samples = (
        [_sample(engagement=5.0, question=True, words=40) for _ in range(4)]
        + [_sample(engagement=1.0, question=False, words=200) for _ in range(4)]
    )
    variants = {f.variant for f in find_patterns(samples)}
    assert {"opener_question", "length_short"} <= variants
    assert not {"opener_statement", "length_long"} & variants


def test_time_window_uses_the_members_timezone():
    # 03:00 UTC is 08:30 in Kolkata → the 06–10 local window.
    samples = [_sample(engagement=4.0, hour=3) for _ in range(3)] + [_sample(engagement=1.0, hour=15) for _ in range(3)]
    [finding] = [f for f in find_patterns(samples, "Asia/Kolkata") if f.variant == "time"]
    assert finding.params == {"start": 6, "end": 10}


# ─────────────────────────────────────────────────────────────────────────────
# Remy + Odette, end to end
# ─────────────────────────────────────────────────────────────────────────────

async def _seed_posts(ws_id: str, user_id: str) -> None:
    """12 measured posts: Threads clearly outperforms LinkedIn."""
    now = datetime.now(timezone.utc)
    for i in range(12):
        piece_id = str(uuid4())
        winner = i < 6
        await content_pieces.insert_one({
            "piece_id": piece_id, "workspace_id": ws_id, "user_id": user_id,
            "content": "Same opener\nbody", "word_count": 120,
        })
        await post_metric_checkpoints.insert_one({
            "_id": f"{piece_id}:24h", "workspace_id": ws_id, "piece_id": piece_id,
            "user_id": user_id, "checkpoint": "24h",
            "platform": "threads" if winner else "linkedin", "word_count": 120,
            "metrics": {"engagement_rate": 6.0 if winner else 2.0},
            "published_at": now - timedelta(days=2, hours=i),
            "captured_at": now - timedelta(days=1, hours=i),
        })


async def test_remy_coaches_the_member_in_their_active_lane(api_client):
    me = await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Coaching", tier="large")
    await _seed_posts(ws_id, me["id"])

    signal_id = await sweep.coach_member(ws_id, me["id"])
    assert signal_id
    signal = await personal_signals.find_one({"_id": signal_id})
    assert signal["signal_type"] == "performance_pattern"
    assert signal["pattern_key"] == "platform:Threads"
    assert "Threads" in signal["member_message"] and "3.0×" in signal["member_message"]
    row = await activity_entries.find_one({"_id": f"remy_signal:{signal_id}"})
    assert row["lane"] == "active" and row["visibility"] == "member"

    # Weekly cadence: an immediate second sweep adds nothing.
    assert await sweep.coach_member(ws_id, me["id"]) is None


async def test_two_dismissals_silence_that_kind_of_finding(api_client):
    me = await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Silenced", tier="large")
    await _seed_posts(ws_id, me["id"])
    old = datetime.now(timezone.utc) - timedelta(days=20)
    for _ in range(2):
        await personal_signals.insert_one({
            "_id": str(uuid4()), "workspace_id": ws_id, "user_id": me["id"],
            "signal_type": "performance_pattern", "status": "dismissed",
            "metric": {"name": "platform"}, "pattern_key": "platform:Linkedin", "created_at": old,
        })
    assert await sweep.coach_member(ws_id, me["id"]) is None


async def test_too_little_data_means_no_coaching(api_client):
    me = await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Sparse", tier="large")
    assert await sweep.coach_member(ws_id, me["id"]) is None


async def test_odette_recommends_to_admins(api_client):
    me = await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Workspace Read", tier="large")
    await _seed_posts(ws_id, me["id"])

    insight_id = await sweep.advise_workspace(ws_id)
    insight = await workspace_insights.find_one({"_id": insight_id})
    assert insight["title"] == "Threads is where your content lands best"
    assert insight["evidence"]["metrics"]["pattern_key"] == "platform:Threads"
    row = await activity_entries.find_one({"_id": f"odette_insight:{insight_id}"})
    assert row["lane"] == "active" and row["visibility"] == "admins"
    assert await sweep.advise_workspace(ws_id) is None      # weekly cadence


# ─────────────────────────────────────────────────────────────────────────────
# Chaining as a suggestion (app.agents.feedback.next_steps)
# ─────────────────────────────────────────────────────────────────────────────

async def _run_completed(ws_id, user_id, *, platforms, trigger="manual"):
    from app.shared.events import emit_event
    await emit_event(
        event_type="pipeline.run_completed", pipeline_type="text",
        workspace_id=ws_id, actor_user_id=user_id, actor_role="owner",
        payload={"session_id": str(uuid4()), "pieces": len(platforms), "platforms": platforms,
                 "title": "Hiring update", "trigger": trigger},
        idempotency_key=f"next-step:{uuid4()}",
    )


async def _connect(ws_id, *platforms, broken=()):
    from app.db.mongo import workspace_connections
    for p in platforms:
        await workspace_connections.insert_one({
            "id": str(uuid4()), "workspace_id": ws_id, "platform": p, "is_active": True,
            "health": {"state": "broken" if p in broken else "healthy"},
        })


async def test_uncovered_connected_channels_become_one_suggestion(api_client):
    me = await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Next Step", tier="large")
    await _connect(ws_id, "linkedin", "facebook", "instagram", broken=("instagram",))

    await _run_completed(ws_id, me["id"], platforms=["LinkedIn"])
    rows = await activity_entries.find({"workspace_id": ws_id, "source.kind": "next_step"}).to_list(5)
    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == "Repurpose this for Facebook too?"   # Instagram's connection is broken
    assert row["lane"] == "active" and row["member_user_id"] == me["id"]

    # Only one open next step at a time.
    await _run_completed(ws_id, me["id"], platforms=["LinkedIn"])
    assert await activity_entries.count_documents({"workspace_id": ws_id, "source.kind": "next_step"}) == 1

    # Accepting closes it (and the page then navigates to the Library).
    res = await api_client.post(
        f"/api/v1/activity/{row['_id']}/decision", json={"decision": "accept"},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text
    assert res.json()["lane"] == "passive" and res.json()["decision"] == "accepted"


async def test_no_suggestion_for_campaign_runs_or_full_coverage(api_client):
    me = await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "No Next Step", tier="large")
    await _connect(ws_id, "linkedin")
    await _run_completed(ws_id, me["id"], platforms=["LinkedIn"])                  # fully covered
    await _connect(ws_id, "facebook")
    await _run_completed(ws_id, me["id"], platforms=["LinkedIn"], trigger="campaign")  # autonomous run
    assert await activity_entries.count_documents({"workspace_id": ws_id, "source.kind": "next_step"}) == 0
