"""Approvals that cannot be bypassed and writes that do not replace newer work."""
from datetime import datetime, timezone
from uuid import uuid4

from app.db.mongo import activity_entries, content_pieces
from app.pipelines.publish.supervisor.classifier import ErrorType, classify_error
from tests.conftest import signup_new_user


async def _workspace(client) -> tuple[str, str]:
    me = (await client.get("/api/v1/users/me")).json()
    return me["default_workspace_id"], me["id"]


async def _piece(ws: str, user: str, session: str, **over) -> str:
    pid = uuid4().hex
    await content_pieces.insert_one({
        "id": pid, "piece_id": pid, "workspace_id": ws, "session_id": session, "user_id": user, "platform": "LinkedIn",
        "content": "Some text", "approval_status": "pending", "publish_status": "pending", "deleted": False, "version_count": 1,
        "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc), "pipeline_type": "text", **over,
    })
    return pid


def test_a_403_is_a_reconnect_only_when_it_says_the_sign_in_is_the_problem():
    assert classify_error(403, "Insufficient permissions for this action.") == ErrorType.AUTH
    assert classify_error(403, "") == ErrorType.AUTH
    assert classify_error(403, "This content is not allowed on the page") != ErrorType.AUTH
    assert classify_error(401, "whatever") == ErrorType.AUTH
    assert classify_error(400, "Token has expired") == ErrorType.AUTH  # Bluesky's wording
    assert classify_error(0, "") == ErrorType.FATAL  # no code is not assumed to be a temporary server error


async def test_approve_many_leaves_rejected_posts_alone_and_says_why(api_client):
    await signup_new_user(api_client)
    ws, me = await _workspace(api_client)
    waiting = await _piece(ws, "someone-else", "s1")
    rejected = await _piece(ws, "someone-else", "s1", approval_status="rejected")
    res = await api_client.post("/api/v1/content/pieces/approve-many", json={"piece_ids": [waiting, rejected, "nope"]})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["approved"] == [waiting]
    assert {s["piece_id"] for s in body["skipped"]} == {rejected, "nope"}
    assert (await content_pieces.find_one({"piece_id": rejected}))["approval_status"] == "rejected"


async def test_approve_all_for_a_run_does_not_undo_a_rejection(api_client):
    await signup_new_user(api_client)
    ws, me = await _workspace(api_client)
    sid = uuid4().hex
    from app.db.mongo import content_sessions

    await content_sessions.insert_one({"session_id": sid, "workspace_id": ws, "user_id": "someone-else"})
    waiting = await _piece(ws, "someone-else", sid)
    rejected = await _piece(ws, "someone-else", sid, approval_status="rejected")
    res = await api_client.patch(f"/api/v1/content/sessions/{sid}/approve-all")
    assert res.status_code == 200, res.text
    assert res.json()["approved_count"] == 1
    assert (await content_pieces.find_one({"piece_id": waiting}))["approval_status"] == "approved"
    assert (await content_pieces.find_one({"piece_id": rejected}))["approval_status"] == "rejected"
    again = await api_client.patch(f"/api/v1/content/sessions/{sid}/approve-all")
    assert again.status_code == 200 and again.json()["approved_count"] == 0  # nothing left to approve is not an error


async def test_rewriting_an_approved_post_sends_it_back_to_review(api_client):
    await signup_new_user(api_client)
    ws, me = await _workspace(api_client)
    pid = await _piece(ws, me, "s2", approval_status="approved")
    res = await api_client.patch(f"/api/v1/content/pieces/{pid}", json={"content": "A new version of the text"})
    assert res.status_code == 200, res.text
    assert res.json()["approval_status"] == "pending"


async def test_a_published_post_text_cannot_be_changed_and_a_stale_edit_is_refused(api_client):
    await signup_new_user(api_client)
    ws, me = await _workspace(api_client)
    published = await _piece(ws, me, "s3", publish_status="published")
    res = await api_client.patch(f"/api/v1/content/pieces/{published}", json={"content": "Changed after it went live"})
    assert res.status_code == 409

    live = await _piece(ws, me, "s3")
    stale = await api_client.patch(f"/api/v1/content/pieces/{live}", json={"content": "Edit from an old tab", "base_version": 7})
    assert stale.status_code == 409
    fresh = await api_client.patch(f"/api/v1/content/pieces/{live}", json={"content": "Edit from the right version", "base_version": 1})
    assert fresh.status_code == 200, fresh.text


async def test_editing_a_post_drops_a_trimmed_copy_kept_from_an_earlier_failed_send(api_client):
    await signup_new_user(api_client)
    ws, me = await _workspace(api_client)
    pid = await _piece(ws, me, "s4", publish_content_override="old trimmed text")
    await api_client.patch(f"/api/v1/content/pieces/{pid}", json={"content": "The member's new text"})
    assert "publish_content_override" not in await content_pieces.find_one({"piece_id": pid})


async def test_scheduling_resets_what_a_past_failure_left_and_cancelling_a_send_in_flight_is_refused(api_client):
    await signup_new_user(api_client)
    ws, me = await _workspace(api_client)
    sending = await _piece(ws, me, "s5", publish_status="publishing")
    res = await api_client.post(f"/api/v1/content/pieces/{sending}/cancel-schedule")
    assert res.status_code == 409  # being sent right now: never silently cancelled
    assert (await content_pieces.find_one({"piece_id": sending}))["publish_status"] == "publishing"

    failed = await _piece(ws, me, "s5", publish_status="failed", publish_attempts=3, publish_content_override="trimmed", auth_recovery_tried=True)
    cancelled = await api_client.post(f"/api/v1/content/pieces/{failed}/cancel-schedule")
    assert cancelled.status_code == 200, cancelled.text
    saved = await content_pieces.find_one({"piece_id": failed})
    assert saved["publish_status"] == "pending" and saved["publish_attempts"] == 0
    assert "publish_content_override" not in saved and "auth_recovery_tried" not in saved


async def test_a_patched_activity_row_keeps_what_others_marked_on_it():
    from app.shared.activity import store

    entry_id = f"act-{uuid4().hex}"
    now = datetime.now(timezone.utc)
    await activity_entries.insert_one({
        "_id": entry_id, "workspace_id": "w", "lane": "active", "visibility": "all", "occurred_at": now, "title": "t",
        "status": "success", "read_by": ["reader"], "hidden_by": [],
    })
    updated = await store.patch_entry(entry_id, {"title": "A new title"})
    assert updated["title"] == "A new title"
    assert (await activity_entries.find_one({"_id": entry_id}))["read_by"] == ["reader"]
    await activity_entries.delete_one({"_id": entry_id})
