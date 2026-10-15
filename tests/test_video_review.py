"""Client review links: view only, 14 days by default, a name to comment, approval or a request for changes, an earlier version beside the
current one, and the member able to end a link at any time. Every unusable link answers the same plain 404."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.db.mongo import activity_entries, get_db, media_assets
from tests.conftest import create_workspace

M = "/api/v1/video-review"
P = "/api/v1/video-review-public"


async def _finished_project(ws: str, user: str = "u1", renders: int = 1) -> tuple[str, list[str]]:
    pid = uuid4().hex
    await get_db()["video_projects"].insert_one({"id": pid, "workspace_id": ws, "title": "Launch teaser", "edit": {}})
    jobs = []
    for i in range(renders):
        mid = uuid4().hex
        await media_assets.insert_one({"id": mid, "workspace_id": ws, "kind": "video", "url": f"https://cdn.example/{mid}.mp4", "mime_type": "video/mp4",
                                       "duration_s": 30.0, "created_by": user, "created_at": datetime.now(timezone.utc)})
        jid = uuid4().hex
        await get_db()["video_jobs"].insert_one({"id": jid, "workspace_id": ws, "project_id": pid, "status": "done", "media_id": mid,
                                                  "finished_at": datetime.now(timezone.utc) + timedelta(minutes=i), "created_at": datetime.now(timezone.utc)})
        jobs.append(jid)
    return pid, jobs


@pytest.fixture
async def world(signup_user):
    client, profile = await signup_user()
    ws = await create_workspace(client, f"Review {uuid4().hex[:6]}")
    return client, ws, {"X-Workspace-Id": ws}


def _token(made) -> str:
    return made.json()["url"].rsplit("/", 1)[1]


async def test_a_link_needs_a_finished_video_and_lasts_fourteen_days_by_default(world):
    client, ws, h = world
    empty = uuid4().hex
    await get_db()["video_projects"].insert_one({"id": empty, "workspace_id": ws, "title": "Draft", "edit": {}})
    refused = await client.post(f"{M}/projects/{empty}/links", headers=h, json={})
    assert refused.status_code == 409 and "Make the video first" in refused.json()["detail"]

    pid, _ = await _finished_project(ws)
    made = await client.post(f"{M}/projects/{pid}/links", headers=h, json={})

    assert made.status_code == 201
    body = made.json()
    assert body["active"] is True and "/review/" in body["url"] and body["has_compare"] is False
    expires = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))
    assert abs((expires - datetime.now(timezone.utc) - timedelta(days=14)).total_seconds()) < 120
    assert (await client.post(f"{M}/projects/{pid}/links", headers=h, json={"days": 3})).status_code == 422       # 7 to 30 days only


async def test_the_client_sees_only_the_video_and_never_a_workspace_user_or_brand(world):
    client, ws, h = world
    pid, _ = await _finished_project(ws)
    token = _token(await client.post(f"{M}/projects/{pid}/links", headers=h, json={"note": "Please check the intro"}))

    seen = await client.get(f"{P}/{token}")

    assert seen.status_code == 200
    data = seen.json()
    assert data["title"] == "Launch teaser" and data["media"]["url"].startswith("https://cdn.example/") and data["note"] == "Please check the intro"
    assert data["compare"] is None and data["decision"] is None
    assert ws not in seen.text and "workspace" not in seen.text and "created_by" not in seen.text


async def test_unknown_expired_and_ended_links_all_give_the_same_404(world):
    client, ws, h = world
    pid, _ = await _finished_project(ws)
    made = await client.post(f"{M}/projects/{pid}/links", headers=h, json={})
    token, link_id = _token(made), made.json()["id"]

    unknown = await client.get(f"{P}/not-a-real-token")
    await get_db()["video_review_links"].update_one({"token": token}, {"$set": {"expires_at": datetime.now(timezone.utc) - timedelta(minutes=1)}})
    expired = await client.get(f"{P}/{token}")
    await get_db()["video_review_links"].update_one({"token": token}, {"$set": {"expires_at": datetime.now(timezone.utc) + timedelta(days=1)}})
    assert (await client.get(f"{P}/{token}")).status_code == 200
    assert (await client.delete(f"{M}/links/{link_id}", headers=h)).status_code == 204
    ended = await client.get(f"{P}/{token}")

    assert unknown.status_code == expired.status_code == ended.status_code == 404
    assert unknown.json() == expired.json() == ended.json()


async def test_a_client_comments_at_a_moment_with_a_name_and_the_member_sees_it_in_video_order(world):
    client, ws, h = world
    pid, _ = await _finished_project(ws)
    token = _token(await client.post(f"{M}/projects/{pid}/links", headers=h, json={}))

    late = await client.post(f"{P}/{token}/comments", json={"time_ms": 20000, "text": "Logo is cut off", "guest_name": "Priya"})
    early = await client.post(f"{P}/{token}/comments", json={"time_ms": 4500, "text": "Love this opening", "guest_name": "Priya"})
    nameless = await client.post(f"{P}/{token}/comments", json={"time_ms": 100, "text": "Hi", "guest_name": "  "})
    blank = await client.post(f"{P}/{token}/comments", json={"time_ms": 100, "text": "   ", "guest_name": "Priya"})

    assert late.status_code == early.status_code == 201
    assert nameless.status_code == 400 and "name" in nameless.json()["detail"] and blank.status_code == 400
    shown = (await client.get(f"{P}/{token}/comments")).json()
    assert [c["time_ms"] for c in shown] == [4500, 20000] and shown[0]["author_name"] == "Priya"
    mine = (await client.get(f"{M}/projects/{pid}/comments", headers=h)).json()["comments"]
    assert [c["text"] for c in mine] == ["Love this opening", "Logo is cut off"]


async def test_a_bot_that_fills_the_hidden_field_is_told_it_worked_and_nothing_is_stored(world):
    client, ws, h = world
    pid, _ = await _finished_project(ws)
    token = _token(await client.post(f"{M}/projects/{pid}/links", headers=h, json={}))

    out = await client.post(f"{P}/{token}/comments", json={"time_ms": 1, "text": "Buy now", "guest_name": "Bot", "website": "http://spam"})

    assert out.status_code == 201
    assert await get_db()["video_review_comments"].count_documents({"workspace_id": ws}) == 0


async def test_the_member_can_resolve_and_reply_and_the_client_sees_both(world):
    client, ws, h = world
    pid, _ = await _finished_project(ws)
    token = _token(await client.post(f"{M}/projects/{pid}/links", headers=h, json={}))
    await client.post(f"{P}/{token}/comments", json={"time_ms": 2000, "text": "Fix the title", "guest_name": "Sam"})
    cid = (await client.get(f"{M}/projects/{pid}/comments", headers=h)).json()["comments"][0]["id"]

    replied = await client.post(f"{M}/comments/{cid}/replies", headers=h, json={"text": "Done, new version coming"})
    resolved = await client.put(f"{M}/comments/{cid}/resolved", headers=h, json={"resolved": True})

    assert replied.status_code == 201 and resolved.status_code == 200 and resolved.json()["resolved"] is True
    shown = (await client.get(f"{P}/{token}/comments")).json()[0]
    assert shown["resolved"] is True and shown["replies"][0]["text"] == "Done, new version coming"
    listing = (await client.get(f"{M}/projects/{pid}/links", headers=h)).json()["links"][0]
    assert listing["comments"] == 1 and listing["open_comments"] == 0


async def test_the_client_approves_or_asks_for_changes_and_the_latest_answer_shows_and_is_noted_in_activity(world):
    client, ws, h = world
    pid, _ = await _finished_project(ws)
    token = _token(await client.post(f"{M}/projects/{pid}/links", headers=h, json={}))

    changes = await client.post(f"{P}/{token}/decision", json={"outcome": "changes_requested", "name": "Priya", "note": "Shorter intro"})
    approved = await client.post(f"{P}/{token}/decision", json={"outcome": "approved", "name": "Priya"})
    nameless = await client.post(f"{P}/{token}/decision", json={"outcome": "approved", "name": ""})

    assert changes.status_code == approved.status_code == 201 and nameless.status_code == 422
    assert (await client.get(f"{P}/{token}")).json()["decision"]["outcome"] == "approved"
    listing = (await client.get(f"{M}/projects/{pid}/links", headers=h)).json()["links"][0]
    assert listing["decision"]["outcome"] == "approved" and listing["decision"]["name"] == "Priya"
    titles = sorted(r["title"] for r in await activity_entries.find({"workspace_id": ws}).to_list(length=10))
    assert titles == ["Priya approved Launch teaser", "Priya asked for changes to Launch teaser"]


async def test_an_earlier_version_can_sit_beside_the_current_one_but_not_the_same_or_a_stranger(world):
    client, ws, h = world
    pid, jobs = await _finished_project(ws, renders=2)                 # jobs[1] finished last, so it is the current one
    _, other_jobs = await _finished_project(ws)

    same = await client.post(f"{M}/projects/{pid}/links", headers=h, json={"compare_job_id": jobs[1]})
    stranger = await client.post(f"{M}/projects/{pid}/links", headers=h, json={"compare_job_id": other_jobs[0]})
    made = await client.post(f"{M}/projects/{pid}/links", headers=h, json={"compare_job_id": jobs[0]})

    assert same.status_code == 400 and stranger.status_code == 404 and made.status_code == 201 and made.json()["has_compare"] is True
    seen = (await client.get(f"{P}/{_token(made)}")).json()
    assert seen["compare"]["url"] != seen["media"]["url"]


async def test_another_workspace_cannot_list_or_end_a_link(world, signup_user):
    client, ws, h = world
    pid, _ = await _finished_project(ws)
    made = (await client.post(f"{M}/projects/{pid}/links", headers=h, json={})).json()
    other_client, _ = await signup_user()
    other_ws = await create_workspace(other_client, f"Other {uuid4().hex[:6]}")
    oh = {"X-Workspace-Id": other_ws}

    assert (await other_client.get(f"{M}/projects/{pid}/links", headers=oh)).json()["links"] == []
    assert (await other_client.delete(f"{M}/links/{made['id']}", headers=oh)).status_code == 404
