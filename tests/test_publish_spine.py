"""Tests for the publish spine: scheduling intent, time handling, the approval
gate, double-publish protection, the stuck-post reaper, platform keys and the
audio-on-its-own message.

Every publisher is a stub; nothing talks to a real platform.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.db.mongo import activity_entries, content_pieces
from app.models.media import MediaAsset
from app.pipelines.publish.base import AUDIO_ALONE_MESSAGE, PublishRequest, PublishResult
from app.pipelines.publish.registry import get_publisher
from app.pipelines.publish.spine import (
    check_gate,
    iso_utc,
    parse_schedule_time,
    platform_key,
    to_utc_datetime,
)
from app.pipelines.publish.token_store import save_token
from app.pipelines.text.storage import ensure_session_exists, save_live_piece
from app.workers import scheduled_posts as worker
from tests.conftest import create_workspace

H = lambda ws: {"X-Workspace-Id": ws}  # noqa: E731


def _later(days: int = 30) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=days)


async def _seed(ws_id: str, user_id: str, platform: str = "LinkedIn", session_id: str | None = None, **kw) -> str:
    session_id = session_id or str(uuid4())
    await ensure_session_exists(
        session_id=session_id, workspace_id=ws_id, user_id=user_id, brand_id=str(uuid4()), source_type="text",
    )
    return await save_live_piece(
        session_id=session_id, workspace_id=ws_id, user_id=user_id, brand_id=str(uuid4()),
        platform=platform, content=f"Real post for {platform}.", word_count=4, char_count=30, **kw,
    )


async def _connect(ws_id: str, platform: str = "linkedin") -> None:
    await save_token(
        workspace_id=ws_id, platform=platform, access_token="fake-access-token", refresh_token=None,
        expires_at=None, platform_user_id="acct-1", username="tester", connected_by="",
    )


async def _approve(client, ws_id: str, piece_id: str):
    res = await client.patch(f"/api/v1/content/pieces/{piece_id}/approve", headers=H(ws_id))
    assert res.status_code == 200, res.text
    return res.json()


def _published_ids(fake: AsyncMock) -> set[str]:
    """Which pieces a stub publisher was asked to send (other tests may leave due posts behind)."""
    return {call.args[0].piece_id for call in fake.publish.call_args_list}


def _ok_publisher(piece_id: str = "x") -> AsyncMock:
    fake = AsyncMock()
    fake.publish = AsyncMock(return_value=PublishResult(
        success=True, platform="linkedin", piece_id=piece_id, platform_post_id="p1", platform_post_url="https://x/p1",
    ))
    return fake


# ── platform key ─────────────────────────────────────────────────────────────

def test_platform_key_uses_the_registry():
    assert platform_key("Twitter/X") == "twitter"
    assert platform_key("Twitter/X Thread") == "twitter"
    assert platform_key("LinkedIn") == "linkedin"
    assert platform_key("linkedin") == "linkedin"
    assert platform_key("YouTube") == "youtube"


def test_platform_key_falls_back_to_lowercase_for_unknown_text():
    assert platform_key("Some New Network") == "some new network"
    assert platform_key("") == ""
    assert platform_key(None) == ""


# ── time handling ────────────────────────────────────────────────────────────

def test_parse_schedule_time_converts_offsets_to_utc():
    parsed = parse_schedule_time("2099-01-01T10:00:00+05:30")
    assert parsed == datetime(2099, 1, 1, 4, 30, tzinfo=timezone.utc)
    assert parsed.tzinfo is not None


def test_parse_schedule_time_treats_a_bare_time_as_utc_and_accepts_z():
    assert parse_schedule_time("2099-01-01T10:00:00") == datetime(2099, 1, 1, 10, 0, tzinfo=timezone.utc)
    assert parse_schedule_time("2099-01-01T10:00:00Z") == datetime(2099, 1, 1, 10, 0, tzinfo=timezone.utc)


def test_parse_schedule_time_refuses_the_past_but_forgives_a_slow_click():
    with pytest.raises(HTTPException) as exc:
        parse_schedule_time((datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat())
    assert exc.value.status_code == 422
    parse_schedule_time((datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat())  # no error


def test_parse_schedule_time_refuses_nonsense_in_plain_words():
    with pytest.raises(HTTPException) as exc:
        parse_schedule_time("next tuesday")
    assert exc.value.status_code == 422
    assert "isn't valid" in exc.value.detail


def test_to_utc_datetime_reads_real_datetimes_and_old_strings():
    assert to_utc_datetime("2026-09-12T09:00:00+00:00") == datetime(2026, 9, 12, 9, 0, tzinfo=timezone.utc)
    assert to_utc_datetime(datetime(2026, 9, 12, 9, 0)) == datetime(2026, 9, 12, 9, 0, tzinfo=timezone.utc)
    assert to_utc_datetime("") is None and to_utc_datetime(None) is None and to_utc_datetime("junk") is None
    assert iso_utc(datetime(2026, 9, 12, 9, 0)) == "2026-09-12T09:00:00Z"
    assert iso_utc("2026-09-12T09:00:00+00:00") == "2026-09-12T09:00:00Z"


async def test_schedule_stores_a_real_utc_datetime_and_returns_iso_text(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Time WS")
    await _connect(ws_id)
    piece_id = await _seed(ws_id, profile["id"])
    await _approve(client, ws_id, piece_id)

    res = await client.patch(
        f"/api/v1/content/pieces/{piece_id}/schedule",
        json={"scheduled_at": "2099-01-01T10:00:00+05:30"}, headers=H(ws_id),
    )
    assert res.status_code == 200, res.text
    assert res.json()["publish_scheduled_at"] == "2099-01-01T04:30:00Z"
    stored = await content_pieces.find_one({"piece_id": piece_id})
    assert isinstance(stored["publish_scheduled_at"], datetime)
    assert stored["publish_scheduled_at"] == datetime(2099, 1, 1, 4, 30)  # Mongo returns UTC without a zone
    assert stored["publish_target"] == "linkedin"


async def test_schedule_refuses_a_time_in_the_past_and_nonsense(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Past WS")
    await _connect(ws_id)
    piece_id = await _seed(ws_id, profile["id"])
    await _approve(client, ws_id, piece_id)

    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    for bad in (past, "not a time"):
        res = await client.patch(
            f"/api/v1/content/pieces/{piece_id}/schedule", json={"scheduled_at": bad}, headers=H(ws_id),
        )
        assert res.status_code == 422, res.text
        assert isinstance(res.json()["detail"], str)
    assert (await content_pieces.find_one({"piece_id": piece_id}))["publish_status"] == "pending"


# ── never queue a piece twice ────────────────────────────────────────────────

@pytest.mark.parametrize("status", ["published", "publishing"])
async def test_schedule_on_a_published_or_publishing_piece_is_refused(signup_user, status):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, f"Twice WS {status}")
    await _connect(ws_id)
    piece_id = await _seed(ws_id, profile["id"])
    await _approve(client, ws_id, piece_id)
    await content_pieces.update_one({"piece_id": piece_id}, {"$set": {"publish_status": status}})

    res = await client.patch(
        f"/api/v1/content/pieces/{piece_id}/schedule",
        json={"scheduled_at": _later().isoformat()}, headers=H(ws_id),
    )
    assert res.status_code == 409, res.text
    assert (await content_pieces.find_one({"piece_id": piece_id}))["publish_status"] == status


# ── the gate ─────────────────────────────────────────────────────────────────

def test_gate_rules_in_isolation():
    ok = {"approval_status": "approved", "quality_passed": True}
    assert check_gate(ok) is None
    assert check_gate({"approval_status": "pending"}).code == "NOT_APPROVED"
    assert check_gate({}).code == "NOT_APPROVED"
    assert check_gate({"approval_status": "rejected"}).code == "REJECTED"
    # rejected stays blocked even when the caller confirms
    assert check_gate({"approval_status": "rejected"}, confirm_anyway=True).code == "REJECTED"
    assert check_gate({**ok, "flagged_for_review": True}).code == "NEEDS_REVIEW"
    assert check_gate({**ok, "quality_passed": False}).code == "NEEDS_REVIEW"
    assert check_gate({**ok, "media": [{"qa_flagged": True}]}).code == "NEEDS_REVIEW"
    assert check_gate({**ok, "flagged_for_review": True}, confirm_anyway=True) is None
    # a recorded override counts only where it is honoured (the worker)
    recorded = {**ok, "flagged_for_review": True, "publish_override_at": datetime.now(timezone.utc)}
    assert check_gate(recorded).code == "NEEDS_REVIEW"
    assert check_gate(recorded, honour_recorded_override=True) is None


def test_gate_messages_are_plain_english():
    for piece in ({}, {"approval_status": "rejected"}, {"approval_status": "approved", "flagged_for_review": True}):
        message = check_gate(piece).message
        assert "—" not in message and "approval_status" not in message


async def test_publish_now_needs_an_approved_piece(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Gate WS 1")
    await _connect(ws_id)
    piece_id = await _seed(ws_id, profile["id"])
    fake = _ok_publisher(piece_id)

    with patch("app.pipelines.publish.executor.get_publisher", return_value=fake):
        res = await client.post("/api/v1/publish/now", json={"piece_id": piece_id}, headers=H(ws_id))
    assert res.status_code == 409, res.text
    assert res.json()["detail"]["code"] == "NOT_APPROVED"
    assert res.json()["detail"]["message"]
    fake.publish.assert_not_awaited()
    # the claim was put back, so approving then publishing works
    assert (await content_pieces.find_one({"piece_id": piece_id}))["publish_status"] == "pending"
    await _approve(client, ws_id, piece_id)
    with patch("app.pipelines.publish.executor.get_publisher", return_value=fake):
        again = await client.post("/api/v1/publish/now", json={"piece_id": piece_id}, headers=H(ws_id))
    assert again.status_code == 200, again.text


async def test_a_rejected_piece_is_blocked_for_publish_and_schedule(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Gate WS 2")
    await _connect(ws_id)
    piece_id = await _seed(ws_id, profile["id"])
    rej = await client.patch(f"/api/v1/content/pieces/{piece_id}/reject", headers=H(ws_id))
    assert rej.status_code == 200, rej.text
    fake = _ok_publisher(piece_id)

    with patch("app.pipelines.publish.executor.get_publisher", return_value=fake):
        # even "publish anyway" cannot get a rejected piece out
        res = await client.post(
            "/api/v1/publish/now", json={"piece_id": piece_id, "confirm_publish_anyway": True}, headers=H(ws_id),
        )
    assert res.status_code == 409 and res.json()["detail"]["code"] == "REJECTED"
    sched = await client.patch(
        f"/api/v1/content/pieces/{piece_id}/schedule",
        json={"scheduled_at": _later().isoformat(), "confirm_publish_anyway": True}, headers=H(ws_id),
    )
    assert sched.status_code == 409 and sched.json()["detail"]["code"] == "REJECTED"
    fake.publish.assert_not_awaited()


@pytest.mark.parametrize("flag", [
    {"flagged_for_review": True},
    {"quality_passed": False},
    {"media": [{
        "id": "m1", "workspace_id": "x", "kind": "image", "url": "https://cdn.example/a.png", "mime_type": "image/png",
        "source": "uploaded", "created_by": "u", "created_at": "2026-09-01T00:00:00Z", "qa_flagged": True,
    }]},
])
async def test_a_flagged_piece_needs_publish_anyway_and_the_override_is_recorded(signup_user, flag):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, f"Gate WS flag {uuid4().hex[:6]}")
    await _connect(ws_id)
    piece_id = await _seed(ws_id, profile["id"])
    await _approve(client, ws_id, piece_id)
    await content_pieces.update_one({"piece_id": piece_id}, {"$set": flag})
    fake = _ok_publisher(piece_id)

    with patch("app.pipelines.publish.executor.get_publisher", return_value=fake):
        refused = await client.post("/api/v1/publish/now", json={"piece_id": piece_id}, headers=H(ws_id))
        assert refused.status_code == 409 and refused.json()["detail"]["code"] == "NEEDS_REVIEW"
        fake.publish.assert_not_awaited()

        sent = await client.post(
            "/api/v1/publish/now", json={"piece_id": piece_id, "confirm_publish_anyway": True}, headers=H(ws_id),
        )
    assert sent.status_code == 200, sent.text
    doc = await content_pieces.find_one({"piece_id": piece_id})
    assert doc["publish_status"] == "published"
    assert doc["publish_override_by"] == profile["id"]
    assert doc["publish_override_at"] is not None
    row = await activity_entries.find_one({"_id": f"system:publish-override:{piece_id}"})
    assert row is not None and row["workspace_id"] == ws_id


async def test_schedule_gate_and_the_worker_honours_a_recorded_override(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Gate WS 3")
    await _connect(ws_id)
    piece_id = await _seed(ws_id, profile["id"])
    await _approve(client, ws_id, piece_id)
    await content_pieces.update_one({"piece_id": piece_id}, {"$set": {"flagged_for_review": True}})

    body = {"scheduled_at": _later().isoformat()}
    refused = await client.patch(f"/api/v1/content/pieces/{piece_id}/schedule", json=body, headers=H(ws_id))
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "NEEDS_REVIEW"
    ok = await client.patch(
        f"/api/v1/content/pieces/{piece_id}/schedule", json={**body, "confirm_publish_anyway": True}, headers=H(ws_id),
    )
    assert ok.status_code == 200, ok.text
    doc = await content_pieces.find_one({"piece_id": piece_id})
    assert doc["publish_status"] == "queued" and doc["publish_override_by"] == profile["id"]

    # make it due: the worker lets it through because a person already confirmed
    await content_pieces.update_one(
        {"piece_id": piece_id}, {"$set": {"publish_scheduled_at": datetime.now(timezone.utc) - timedelta(minutes=1)}},
    )
    fake = _ok_publisher(piece_id)
    with patch("app.pipelines.publish.executor.get_publisher", return_value=fake):
        await worker.process_scheduled_posts.__wrapped__()
    assert (await content_pieces.find_one({"piece_id": piece_id}))["publish_status"] == "published"


async def test_the_emergency_switch_lifts_the_gate(signup_user, monkeypatch):
    from app.core.config import settings

    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Gate WS 4")
    await _connect(ws_id)
    piece_id = await _seed(ws_id, profile["id"])
    monkeypatch.setattr(settings, "PUBLISH_REQUIRE_APPROVAL", False)
    with patch("app.pipelines.publish.executor.get_publisher", return_value=_ok_publisher(piece_id)):
        res = await client.post("/api/v1/publish/now", json={"piece_id": piece_id}, headers=H(ws_id))
    assert res.status_code == 200, res.text


# ── a planned time at generation is intent only ──────────────────────────────

async def test_collect_output_records_intent_and_does_not_queue():
    from app.agents.text.nodes import collect_output_node
    from app.models.text import Platform

    when = _later(3)
    state = {
        "generated_content": "A post.", "current_platform": Platform.LINKEDIN, "hooks": [], "seo_package": {},
        "readability_score": None, "quality_passed": True, "quality_issues": [], "flagged_for_review": False,
        "publish_target": "linkedin", "schedule_mode": "scheduled", "scheduled_at": when.isoformat(),
        "pieces": [], "emitter": None,
    }
    out = await collect_output_node(state)
    piece = out["pieces"][0]
    assert piece["publish_status"] == "pending"
    assert piece["publish_scheduled_at"] is None
    assert piece["intended_publish_at"] == when
    assert piece["publish_target"] == "linkedin"

    now_state = {**state, "schedule_mode": "now", "scheduled_at": None}
    assert (await collect_output_node(now_state))["pieces"][0]["intended_publish_at"] is None


async def test_an_approved_piece_with_a_planned_time_is_queued(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Intent WS 1")
    await _connect(ws_id)
    when = _later(5).replace(microsecond=0)
    piece_id = await _seed(ws_id, profile["id"], intended_publish_at=when, publish_target="linkedin")

    before = await content_pieces.find_one({"piece_id": piece_id})
    assert before["publish_status"] == "pending" and before["intended_publish_at"] is not None

    approved = await _approve(client, ws_id, piece_id)
    assert approved["publish_status"] == "queued"
    assert approved["stage"] == "scheduled"
    assert approved["publish_scheduled_at"] == when.isoformat().replace("+00:00", "Z")
    stored = await content_pieces.find_one({"piece_id": piece_id})
    assert isinstance(stored["publish_scheduled_at"], datetime)
    assert stored["publish_target"] == "linkedin"


async def test_a_planned_time_that_has_passed_stays_pending_with_a_note(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Intent WS 2")
    await _connect(ws_id)
    piece_id = await _seed(ws_id, profile["id"], intended_publish_at=datetime.now(timezone.utc) - timedelta(hours=2))
    approved = await _approve(client, ws_id, piece_id)
    assert approved["publish_status"] == "pending"
    assert "passed" in approved["schedule_note"]


async def test_a_planned_time_without_a_connected_account_stays_pending_with_a_note(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Intent WS 3")
    piece_id = await _seed(ws_id, profile["id"], intended_publish_at=_later(2))
    approved = await _approve(client, ws_id, piece_id)
    assert approved["publish_status"] == "pending"
    assert "not connected" in approved["schedule_note"].lower()


async def test_a_flagged_piece_with_a_planned_time_is_not_queued_by_approval(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Intent WS 4")
    await _connect(ws_id)
    piece_id = await _seed(ws_id, profile["id"], intended_publish_at=_later(2), flagged_for_review=True)
    approved = await _approve(client, ws_id, piece_id)
    assert approved["publish_status"] == "pending"
    assert "review" in approved["schedule_note"].lower()


async def test_approve_all_queues_planned_pieces_too(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Intent WS 5")
    await _connect(ws_id)
    session_id = str(uuid4())
    ids = [
        await _seed(ws_id, profile["id"], session_id=session_id, intended_publish_at=_later(4)),
        await _seed(ws_id, profile["id"], session_id=session_id, intended_publish_at=_later(6)),
        await _seed(ws_id, profile["id"], session_id=session_id),  # no plan: just approved
    ]
    res = await client.patch(f"/api/v1/content/sessions/{session_id}/approve-all", headers=H(ws_id))
    assert res.status_code == 200, res.text
    assert res.json()["approved_count"] == 3 and res.json()["queued_count"] == 2
    statuses = [(await content_pieces.find_one({"piece_id": i}))["publish_status"] for i in ids]
    assert statuses == ["queued", "queued", "pending"]


# ── the worker ───────────────────────────────────────────────────────────────

async def _due(ws_id: str, user_id: str, *, as_string: bool, approved: bool = True, **fields) -> str:
    piece_id = await _seed(ws_id, user_id)
    due = datetime.now(timezone.utc) - timedelta(minutes=1)
    await content_pieces.update_one({"piece_id": piece_id}, {"$set": {
        "publish_status": "queued", "publish_target": "linkedin",
        "publish_scheduled_at": due.isoformat() if as_string else due,
        "approval_status": "approved" if approved else "pending", **fields,
    }})
    return piece_id


async def test_the_worker_publishes_both_real_datetimes_and_old_iso_strings(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Worker WS 1")
    await _connect(ws_id)
    new_row = await _due(ws_id, profile["id"], as_string=False)
    legacy_row = await _due(ws_id, profile["id"], as_string=True)
    future = await _seed(ws_id, profile["id"])
    await content_pieces.update_one({"piece_id": future}, {"$set": {
        "publish_status": "queued", "approval_status": "approved", "publish_target": "linkedin",
        "publish_scheduled_at": _later(), "workspace_id": ws_id,
    }})

    fake = _ok_publisher()
    with patch("app.pipelines.publish.executor.get_publisher", return_value=fake):
        await worker.process_scheduled_posts.__wrapped__()

    assert (await content_pieces.find_one({"piece_id": new_row}))["publish_status"] == "published"
    assert (await content_pieces.find_one({"piece_id": legacy_row}))["publish_status"] == "published"
    assert (await content_pieces.find_one({"piece_id": future}))["publish_status"] == "queued"


async def test_the_worker_holds_back_unapproved_and_rejected_pieces(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Worker WS 2")
    await _connect(ws_id)
    unapproved = await _due(ws_id, profile["id"], as_string=False, approved=False)
    rejected = await _due(ws_id, profile["id"], as_string=False, approval_status="rejected")
    flagged = await _due(ws_id, profile["id"], as_string=False, flagged_for_review=True)

    fake = _ok_publisher()
    with patch("app.pipelines.publish.executor.get_publisher", return_value=fake):
        await worker.process_scheduled_posts.__wrapped__()
        await worker.process_scheduled_posts.__wrapped__()  # a second tick must not pick them up again

    assert not _published_ids(fake) & {unapproved, rejected, flagged}
    for pid in (unapproved, rejected, flagged):
        doc = await content_pieces.find_one({"piece_id": pid})
        assert doc["publish_status"] == "pending"
        assert doc["schedule_note"].startswith("Not published.")
    assert "approve" in (await content_pieces.find_one({"piece_id": unapproved}))["schedule_note"].lower()


async def test_a_missing_connection_fails_the_piece_with_a_plain_reason(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Worker WS 3")  # no account connected
    piece_id = await _due(ws_id, profile["id"], as_string=False)
    await worker.process_scheduled_posts.__wrapped__()
    doc = await content_pieces.find_one({"piece_id": piece_id})
    assert doc["publish_status"] == "failed"
    assert "isn't connected" in doc["last_error"] and "None" not in doc["last_error"]


async def test_a_missing_publisher_fails_the_piece_and_does_not_repeat(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Worker WS 4")
    await _connect(ws_id)
    piece_id = await _due(ws_id, profile["id"], as_string=False)
    with patch("app.pipelines.publish.executor.get_publisher", side_effect=ValueError("none")):
        await worker.process_scheduled_posts.__wrapped__()
    doc = await content_pieces.find_one({"piece_id": piece_id})
    assert doc["publish_status"] == "failed"
    assert "isn't supported yet" in doc["last_error"]


async def test_the_worker_derives_a_missing_publish_target(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Worker WS 5")
    await _connect(ws_id)
    piece_id = await _due(ws_id, profile["id"], as_string=False, publish_target=None)
    fake = _ok_publisher()
    with patch("app.pipelines.publish.executor.get_publisher", return_value=fake) as getter:
        await worker.process_scheduled_posts.__wrapped__()
    getter.assert_called_with("linkedin")
    doc = await content_pieces.find_one({"piece_id": piece_id})
    assert doc["publish_status"] == "published" and doc["publish_target"] == "linkedin"


async def test_a_publisher_that_raises_cannot_leave_the_piece_publishing(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Worker WS 6")
    await _connect(ws_id)
    piece_id = await _due(ws_id, profile["id"], as_string=False)
    fake = AsyncMock()
    fake.publish = AsyncMock(side_effect=RuntimeError("boom"))
    with patch("app.pipelines.publish.executor.get_publisher", return_value=fake):
        await worker.process_scheduled_posts.__wrapped__()
    doc = await content_pieces.find_one({"piece_id": piece_id})
    assert doc["publish_status"] == "failed"
    assert "Check the platform" in doc["last_error"]
    assert fake.publish.await_count == 1


async def test_only_one_claim_wins_and_publish_now_takes_queued_pieces_too(signup_user):
    from app.api.v1.publish import _claim_piece_for_publishing

    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Claim WS")
    piece_id = await _due(ws_id, profile["id"], as_string=True)
    now = datetime.now(timezone.utc)

    first, second = await asyncio.gather(worker._claim_due_piece(piece_id, now), worker._claim_due_piece(piece_id, now))
    assert sorted([first is None, second is None]) == [False, True]
    winner = first or second
    assert winner["publish_status"] == "publishing" and winner["publishing_started_at"] is not None

    # a queued piece can be claimed by Publish Now, after which the worker cannot
    other = await _due(ws_id, profile["id"], as_string=False)
    claimed, previous = await _claim_piece_for_publishing(other, ws_id)
    assert previous == "queued" and claimed["publish_status"] == "publishing"
    assert await worker._claim_due_piece(other, now) is None
    with pytest.raises(HTTPException) as exc:
        await _claim_piece_for_publishing(other, ws_id)
    assert exc.value.status_code == 409


async def test_the_reaper_fails_posts_stuck_publishing_and_never_republishes(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Reaper WS")
    old = await _seed(ws_id, profile["id"])
    fresh = await _seed(ws_id, profile["id"])
    legacy = await _seed(ws_id, profile["id"])
    fine = await _seed(ws_id, profile["id"])
    now = datetime.now(timezone.utc)
    await content_pieces.update_one({"piece_id": old}, {"$set": {
        "publish_status": "publishing", "publishing_started_at": now - timedelta(minutes=20)}})
    await content_pieces.update_one({"piece_id": fresh}, {"$set": {
        "publish_status": "publishing", "publishing_started_at": now - timedelta(minutes=2)}})
    # from before the start time was stamped: judged by when it was last touched
    await content_pieces.update_one({"piece_id": legacy}, {"$set": {
        "publish_status": "publishing", "updated_at": now - timedelta(hours=2)}})

    fake = _ok_publisher()
    with patch("app.pipelines.publish.executor.get_publisher", return_value=fake):
        await worker.process_scheduled_posts.__wrapped__()

    for pid in (old, legacy):
        doc = await content_pieces.find_one({"piece_id": pid})
        assert doc["publish_status"] == "failed"
        assert doc["last_error"] == "Publishing was interrupted. Check the platform before trying again."
    assert (await content_pieces.find_one({"piece_id": fresh}))["publish_status"] == "publishing"
    assert (await content_pieces.find_one({"piece_id": fine}))["publish_status"] == "pending"
    assert not _published_ids(fake) & {old, fresh, legacy, fine}


# ── audio on its own (D21) ───────────────────────────────────────────────────

def _media(kind: str, mime: str) -> MediaAsset:
    return MediaAsset(
        id=uuid4().hex, workspace_id="w", kind=kind, url=f"https://cdn.example/f.{kind}", mime_type=mime,
        source="uploaded", created_by="u", created_at=datetime.now(timezone.utc),
    )


def _request(platform: str, media: MediaAsset) -> PublishRequest:
    return PublishRequest(
        piece_id="p", user_id="u", brand_id="b", platform=platform, content="Hello there", workspace_id="w",
        media=[media],
    )


def test_no_platform_declares_audiogram():
    from app.platforms.base import PLATFORM_REGISTRY, import_all

    import_all()
    assert PLATFORM_REGISTRY
    for definition in PLATFORM_REGISTRY.values():
        assert "audiogram" not in definition.native_formats.values(), definition.key


@pytest.mark.parametrize("platform", ["linkedin", "facebook", "instagram", "youtube", "bluesky", "threads"])
def test_audio_is_refused_honestly_and_video_still_works(platform):
    publisher = get_publisher(platform)
    audio = publisher.attach_media(_request(platform, _media("audio", "audio/mpeg")))
    assert not audio.has_media
    assert audio.dropped_reason == AUDIO_ALONE_MESSAGE == "Audio can't be posted on its own. Render it as a video and attach that."
    video = publisher.attach_media(_request(platform, _media("video", "video/mp4")))
    assert video.has_media


async def test_instagram_and_youtube_give_the_same_plain_message_for_audio_only():
    for platform in ("instagram", "youtube"):
        result = await get_publisher(platform).publish(_request(platform, _media("audio", "audio/mpeg")), "token")
        assert result.success is False
        assert result.error_message == AUDIO_ALONE_MESSAGE
