"""The public video API: workspace keys shown once and stored hashed, scopes, a rate limit per key, the same 401 for every bad key, and signed
webhooks that are https-only, never reach a private address, retry with a wait and switch themselves off when they keep failing."""
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest

from app.db.mongo import get_db
from app.video import api_keys, webhooks
from tests.conftest import create_workspace

D = "/api/v1/developer"
A = "/api/v1/public/video"


@pytest.fixture
async def world(signup_user):
    client, _ = await signup_user()
    ws = await create_workspace(client, f"Api {uuid4().hex[:6]}")
    return client, ws, {"X-Workspace-Id": ws}


async def _key(client, h, scopes=("video:read",), name="Studio script") -> str:
    made = await client.post(f"{D}/keys", headers=h, json={"name": name, "scopes": list(scopes)})
    assert made.status_code == 201, made.text
    return made.json()["secret"]


async def _project(ws: str) -> str:
    pid = uuid4().hex
    edit = {"fps": {"num": 30, "den": 1}, "width": 1280, "height": 720, "duration_f": 300, "video": [], "audio": [], "markers": [], "captions": {"words": []}}
    await get_db()["video_projects"].insert_one({"id": pid, "workspace_id": ws, "title": "Studio cut", "version": 2, "edit": edit, "updated_at": datetime.now(timezone.utc)})
    return pid


# ── keys ─────────────────────────────────────────────────────────────────────────

async def test_a_key_is_shown_once_stored_only_as_a_hash_and_listed_without_the_secret(world):
    client, ws, h = world

    made = await client.post(f"{D}/keys", headers=h, json={"name": "Studio script", "scopes": ["video:read", "video:render"]})

    assert made.status_code == 201
    secret = made.json()["secret"]
    assert secret.startswith("ek_live_") and made.json()["key"]["prefix"] == secret[:14] and made.json()["key"]["scopes"] == ["video:read", "video:render"]
    stored = await get_db()["api_keys"].find_one({"workspace_id": ws})
    assert secret not in json.dumps({k: str(v) for k, v in stored.items()}) and stored["hash"] != secret
    listed = (await client.get(f"{D}/keys", headers=h)).json()["keys"]
    assert len(listed) == 1 and "secret" not in listed[0] and "hash" not in listed[0]


async def test_a_key_needs_a_name_and_a_known_permission_and_a_workspace_holds_ten(world):
    client, ws, h = world
    assert (await client.post(f"{D}/keys", headers=h, json={"name": "  ", "scopes": ["video:read"]})).status_code in (400, 422)
    assert (await client.post(f"{D}/keys", headers=h, json={"name": "x", "scopes": ["admin:everything"]})).status_code == 400
    for i in range(api_keys.MAX_KEYS):
        await api_keys.create(ws, "u1", f"key {i}", ["video:read"])        # made directly: the route itself is limited to 10 a minute
    over = await client.post(f"{D}/keys", headers=h, json={"name": "one more", "scopes": ["video:read"]})
    assert over.status_code == 400 and "10 keys" in over.json()["detail"]


async def test_every_bad_key_gets_the_same_401_and_a_revoked_key_stops_working(world):
    client, ws, h = world
    secret = await _key(client, h)
    key_id = (await client.get(f"{D}/keys", headers=h)).json()["keys"][0]["id"]

    none = await client.get(f"{A}/projects")
    wrong = await client.get(f"{A}/projects", headers={"Authorization": "Bearer ek_live_nope"})
    junk = await client.get(f"{A}/projects", headers={"Authorization": "Bearer something"})
    assert none.status_code == wrong.status_code == junk.status_code == 401 and none.json() == wrong.json() == junk.json()
    assert (await client.get(f"{A}/projects", headers={"Authorization": f"Bearer {secret}"})).status_code == 200

    assert (await client.delete(f"{D}/keys/{key_id}", headers=h)).status_code == 204
    assert (await client.get(f"{A}/projects", headers={"Authorization": f"Bearer {secret}"})).status_code == 401


async def test_a_key_only_sees_its_own_workspace(world, signup_user):
    client, ws, h = world
    secret = await _key(client, h)
    mine = await _project(ws)
    other_client, _ = await signup_user()
    other_ws = await create_workspace(other_client, f"Other {uuid4().hex[:6]}")
    theirs = await _project(other_ws)
    bearer = {"Authorization": f"Bearer {secret}"}

    listed = (await client.get(f"{A}/projects", headers=bearer)).json()["projects"]

    assert [p["id"] for p in listed] == [mine] and listed[0]["duration_s"] == 10.0 and listed[0]["title"] == "Studio cut"
    assert (await client.get(f"{A}/projects/{theirs}", headers=bearer)).status_code == 404


# ── scopes and rendering ─────────────────────────────────────────────────────────

async def test_reading_is_not_rendering_and_a_render_is_queued_with_a_job_to_poll(world, monkeypatch):
    client, ws, h = world
    read_only = await _key(client, h, scopes=("video:read",), name="read")
    can_render = await _key(client, h, scopes=("video:read", "video:render"), name="render")
    pid = await _project(ws)
    queued = {}

    async def fake_start(**kw):
        queued.update(kw)
        return {"id": "job-1", "status": "queued"}, {}

    from app.video import service
    monkeypatch.setattr(service, "start_project_render", fake_start)

    denied = await client.post(f"{A}/projects/{pid}/render", headers={"Authorization": f"Bearer {read_only}"}, json={})
    ok = await client.post(f"{A}/projects/{pid}/render", headers={"Authorization": f"Bearer {can_render}"}, json={"preset": "high"})

    assert denied.status_code == 403 and "video:render" in denied.json()["detail"]
    assert ok.status_code == 202 and ok.json() == {"job_id": "job-1", "status": "queued"}
    assert queued["workspace_id"] == ws and queued["project_id"] == pid and queued["preset"] == "high"


async def test_a_render_that_cannot_start_says_why_and_a_job_of_another_workspace_is_not_found(world, monkeypatch):
    client, ws, h = world
    secret = await _key(client, h, scopes=("video:read", "video:render"))
    pid = await _project(ws)
    bearer = {"Authorization": f"Bearer {secret}"}
    from app.video import service

    async def refuse(**kw):
        raise service.RenderFailed("This would use more than your 120 render minutes for this month.")

    monkeypatch.setattr(service, "start_project_render", refuse)

    out = await client.post(f"{A}/projects/{pid}/render", headers=bearer, json={})
    assert out.status_code == 409 and "render minutes" in out.json()["detail"]
    await get_db()["video_jobs"].insert_one({"id": "foreign", "workspace_id": "someone-else", "status": "done"})
    assert (await client.get(f"{A}/jobs/foreign", headers=bearer)).status_code == 404


async def test_the_handoff_files_come_through_the_api_too(world):
    client, ws, h = world
    secret = await _key(client, h)
    pid = await _project(ws)
    bearer = {"Authorization": f"Bearer {secret}"}

    nothing = await client.get(f"{A}/projects/{pid}/handoff/edl", headers=bearer)
    unknown = await client.get(f"{A}/projects/{pid}/handoff/nonsense", headers=bearer)

    assert nothing.status_code == 409 and "no clips" in nothing.json()["detail"] and unknown.status_code == 404


async def test_each_key_has_its_own_rate_limit(world, monkeypatch):
    client, ws, h = world
    first, second = await _key(client, h, name="a"), await _key(client, h, name="b")
    statuses = [(await client.get(f"{A}/projects", headers={"Authorization": f"Bearer {first}"})).status_code for _ in range(61)]
    assert statuses[:60] == [200] * 60 and statuses[60] == 429
    assert (await client.get(f"{A}/projects", headers={"Authorization": f"Bearer {second}"})).status_code == 200


# ── webhooks ─────────────────────────────────────────────────────────────────────

class _Receiver:
    """Stands in for the receiving server: records what it is sent and answers with the status it is told to."""

    def __init__(self, status=200):
        self.status, self.calls = status, []

    async def post(self, url, content=None, headers=None, **kw):
        self.calls.append({"url": url, "body": content, "headers": headers})
        return httpx.Response(self.status, request=httpx.Request("POST", url))


@pytest.fixture
def receiver(monkeypatch):
    r = _Receiver()

    class Client:
        def __init__(self, *a, **kw):
            self.kw = kw

        async def __aenter__(self):
            assert self.kw.get("follow_redirects") is False          # a redirect is never followed
            return r

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(webhooks.httpx, "AsyncClient", Client)

    async def fine(url):
        return None

    monkeypatch.setattr(webhooks, "assert_safe_url", fine)
    return r


async def test_a_webhook_is_made_with_a_secret_shown_once_and_an_unsafe_address_is_refused(world):
    client, ws, h = world

    ok = await client.post(f"{D}/webhooks", headers=h, json={"url": "https://example.com/hook", "events": ["video.render_done"]})
    plain = await client.post(f"{D}/webhooks", headers=h, json={"url": "http://example.com/hook", "events": ["video.render_done"]})
    inside = await client.post(f"{D}/webhooks", headers=h, json={"url": "https://localhost/hook", "events": ["video.render_done"]})
    metadata = await client.post(f"{D}/webhooks", headers=h, json={"url": "https://169.254.169.254/latest", "events": ["video.render_done"]})
    nothing = await client.post(f"{D}/webhooks", headers=h, json={"url": "https://example.com/x", "events": ["nonsense"]})

    assert ok.status_code == 201 and ok.json()["signing_secret"].startswith("whsec_")
    assert plain.status_code == 400 and "https" in plain.json()["detail"]
    assert inside.status_code == 400 and metadata.status_code == 400 and "private network" in metadata.json()["detail"]
    assert nothing.status_code == 400
    listed = (await client.get(f"{D}/webhooks", headers=h)).json()["webhooks"]
    assert len(listed) == 1 and "secret" not in listed[0]


async def test_an_event_is_sent_signed_so_the_receiver_can_check_it(world, receiver):
    client, ws, h = world
    made = (await client.post(f"{D}/webhooks", headers=h, json={"url": "https://example.com/hook", "events": ["video.render_done"]})).json()
    secret = made["signing_secret"]

    count = await webhooks.dispatch(ws, "video.render_done", {"project_id": "p1", "job_id": "j1"})
    await __import__("asyncio").sleep(0.3)

    assert count == 1 and len(receiver.calls) == 1
    call = receiver.calls[0]
    stamp = call["headers"]["X-Ekavo-Timestamp"]
    assert call["headers"]["X-Ekavo-Signature"] == webhooks.sign(secret, stamp, call["body"])
    body = json.loads(call["body"])
    assert body["event"] == "video.render_done" and body["data"] == {"project_id": "p1", "job_id": "j1"} and call["headers"]["X-Ekavo-Event"] == "video.render_done"
    assert webhooks.sign("another-secret", stamp, call["body"]) != call["headers"]["X-Ekavo-Signature"]
    delivery = await get_db()["video_webhook_deliveries"].find_one({"workspace_id": ws})
    assert delivery["status"] == "delivered" and delivery["attempts"] == 1


async def test_only_webhooks_that_asked_for_an_event_are_told_and_a_workspace_never_hears_anothers(world, receiver):
    client, ws, h = world
    await client.post(f"{D}/webhooks", headers=h, json={"url": "https://example.com/a", "events": ["video.qc_failed"]})

    assert await webhooks.dispatch(ws, "video.render_done", {}) == 0
    assert await webhooks.dispatch(f"ws-{uuid4()}", "video.qc_failed", {}) == 0
    assert await webhooks.dispatch(ws, "video.something_else", {}) == 0
    assert receiver.calls == []


async def test_a_failed_delivery_waits_and_tries_again_and_finally_gives_up(world, receiver):
    client, ws, h = world
    await client.post(f"{D}/webhooks", headers=h, json={"url": "https://example.com/hook", "events": ["video.render_done"]})
    receiver.status = 500

    await webhooks.dispatch(ws, "video.render_done", {"project_id": "p"})
    await __import__("asyncio").sleep(0.3)
    first = await get_db()["video_webhook_deliveries"].find_one({"workspace_id": ws})
    assert first["status"] == "retrying" and first["attempts"] == 1 and "500" in first["last_error"] and first["next_at"] is not None

    for expected in range(2, webhooks.MAX_ATTEMPTS + 1):
        await get_db()["video_webhook_deliveries"].update_one({"id": first["id"]}, {"$set": {"next_at": datetime.now(timezone.utc) - timedelta(seconds=1)}})
        await webhooks.retry_due()
        now = await get_db()["video_webhook_deliveries"].find_one({"id": first["id"]})
        assert now["attempts"] == expected
    assert now["status"] == "failed" and now["next_at"] is None
    assert len(receiver.calls) == webhooks.MAX_ATTEMPTS


async def test_a_delivery_that_works_on_retry_is_marked_delivered_and_clears_the_failure_count(world, receiver):
    client, ws, h = world
    await client.post(f"{D}/webhooks", headers=h, json={"url": "https://example.com/hook", "events": ["video.render_done"]})
    receiver.status = 503
    await webhooks.dispatch(ws, "video.render_done", {})
    await __import__("asyncio").sleep(0.3)
    d = await get_db()["video_webhook_deliveries"].find_one({"workspace_id": ws})
    assert (await get_db()["video_webhooks"].find_one({"workspace_id": ws}))["failures"] == 1

    receiver.status = 200
    await get_db()["video_webhook_deliveries"].update_one({"id": d["id"]}, {"$set": {"next_at": datetime.now(timezone.utc) - timedelta(seconds=1)}})
    await webhooks.retry_due()

    assert (await get_db()["video_webhook_deliveries"].find_one({"id": d["id"]}))["status"] == "delivered"
    assert (await get_db()["video_webhooks"].find_one({"workspace_id": ws}))["failures"] == 0


async def test_a_webhook_that_keeps_failing_is_switched_off_and_says_why(world, receiver):
    client, ws, h = world
    await client.post(f"{D}/webhooks", headers=h, json={"url": "https://example.com/hook", "events": ["video.render_done"]})
    await get_db()["video_webhooks"].update_one({"workspace_id": ws}, {"$set": {"failures": webhooks.SWITCH_OFF_AFTER - 1}})
    receiver.status = 500

    await webhooks.dispatch(ws, "video.render_done", {})
    await __import__("asyncio").sleep(0.3)

    hook = (await client.get(f"{D}/webhooks", headers=h)).json()["webhooks"][0]
    assert hook["active"] is False and "Switched off" in hook["switched_off_reason"]
    assert await webhooks.dispatch(ws, "video.render_done", {}) == 0


async def test_an_address_that_turns_unsafe_after_saving_is_not_sent_to(world, receiver, monkeypatch):
    client, ws, h = world
    await client.post(f"{D}/webhooks", headers=h, json={"url": "https://example.com/hook", "events": ["video.render_done"]})

    async def now_private(url):
        raise webhooks.UnsafeUrl("That address points inside a private network.")

    monkeypatch.setattr(webhooks, "assert_safe_url", now_private)
    await webhooks.dispatch(ws, "video.render_done", {})
    await __import__("asyncio").sleep(0.3)

    assert receiver.calls == []
    d = await get_db()["video_webhook_deliveries"].find_one({"workspace_id": ws})
    assert d["status"] == "retrying" and "private network" in d["last_error"]


async def test_the_test_button_sends_one_ping_and_reports_what_the_receiver_said(world, receiver):
    client, ws, h = world
    made = (await client.post(f"{D}/webhooks", headers=h, json={"url": "https://example.com/hook", "events": ["video.render_done"]})).json()["webhook"]
    receiver.status = 404

    out = await client.post(f"{D}/webhooks/{made['id']}/test", headers=h)

    assert out.status_code == 200 and out.json()["delivered"] is False and out.json()["status_code"] == 404
    assert (await client.post(f"{D}/webhooks/nope/test", headers=h)).status_code == 404
    assert len((await client.get(f"{D}/webhooks/{made['id']}/deliveries", headers=h)).json()["deliveries"]) == 1


async def test_a_finished_render_event_reaches_a_webhook_through_the_activity_note(world, receiver):
    from app.video import activity

    client, ws, h = world
    await client.post(f"{D}/webhooks", headers=h, json={"url": "https://example.com/hook", "events": ["video.render_done", "video.review_answered"]})
    pid = await _project(ws)

    await activity.video_made(ws, None, pid, "job-77", duration_s=12.0, origin="own")
    await activity.review_answered(ws, None, pid, "link-1", "approved", "Priya", "Looks good")
    await __import__("asyncio").sleep(0.4)

    events = sorted(json.loads(c["body"])["event"] for c in receiver.calls)
    assert events == ["video.render_done", "video.review_answered"]
    answered = next(json.loads(c["body"]) for c in receiver.calls if json.loads(c["body"])["event"] == "video.review_answered")
    assert answered["data"]["outcome"] == "approved" and answered["data"]["name"] == "Priya"
