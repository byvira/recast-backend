"""Publishing as background jobs (one post, or a batch where each post reports its own result), the note on a published post about what
was left out, and the sign-in popup telling the page how it went. The platform publish itself is replaced by a stand-in."""
import json
import re

from fastapi import HTTPException

from app.api.v1 import oauth as oauth_module
from app.api.v1 import publish as publish_module
from app.shared import job_actions, jobs
from tests.conftest import create_workspace
from tests.test_attachments import H, _piece
from tests.test_pipeline_runs import _wait_for


async def _start(client, ws_id, action, payload):
    res = await client.post("/api/v1/jobs", json={"action": action, "payload": payload}, headers=H(ws_id))
    assert res.status_code == 202, res.text
    return res.json()


def _stub_publish(monkeypatch, behaviour):
    async def fake(request, body, ctx):
        return behaviour(body)

    monkeypatch.setattr(publish_module, "publish_now", fake)


async def test_the_publish_actions_are_registered_and_are_never_started_again_after_a_restart():
    jobs.ACTIONS.clear()
    job_actions.register_all()

    assert {n for n in jobs.ACTIONS if n.startswith("publish.")} == {"publish.now", "publish.batch"}
    assert not jobs.ACTIONS["publish.now"].restartable and not jobs.ACTIONS["publish.batch"].restartable
    assert jobs.ACTIONS["publish.batch"].retries == 0 and jobs.ACTIONS["publish.now"].permission == "publish_content"


async def test_a_batch_reports_each_post_and_one_failing_never_stops_the_others(signup_user, monkeypatch):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Publish Jobs 1")
    ok = await _piece(ws_id, profile["id"], platform="LinkedIn")
    blocked = await _piece(ws_id, profile["id"], platform="Bluesky")
    youtube = await _piece(ws_id, profile["id"], platform="YouTube")
    seen: list[str] = []

    def behaviour(body):
        seen.append(body.piece_id)
        if body.piece_id == blocked:
            raise HTTPException(status_code=409, detail={"code": "NOT_APPROVED", "message": "Approve this post first."})
        return {"success": True, "piece_id": body.piece_id, "status": "published", "platform_post_url": "https://example.com/p"}

    _stub_publish(monkeypatch, behaviour)

    run = await _start(client, ws_id, "publish.batch", {"piece_ids": [ok, blocked, youtube, "missing-id", ok]})
    done = await _wait_for(client, run["id"], ("done", "failed"), seconds=60, headers=H(ws_id))

    assert done["status"] == "done", done
    data = done["result"]["data"]
    by_id = {r["piece_id"]: r for r in data["results"]}
    assert seen == [ok, blocked]  # the YouTube post and the missing one never reached the platform; the repeat id ran once
    assert by_id[ok]["success"] is True
    assert (by_id[blocked]["code"], by_id[blocked]["reason"]) == ("NOT_APPROVED", "Approve this post first.")
    assert by_id[youtube]["code"] == "review_first" and by_id["missing-id"]["code"] == "not_found"
    assert (data["published"], data["left_out"]) == (1, 3)


async def test_a_single_publish_job_ends_with_the_platforms_answer(signup_user, monkeypatch):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Publish Jobs 2")
    piece_id = await _piece(ws_id, profile["id"], platform="LinkedIn")
    _stub_publish(monkeypatch, lambda body: {"success": False, "piece_id": body.piece_id, "status": "retry_scheduled", "reason": "Busy", "code": "platform_unavailable"})

    run = await _start(client, ws_id, "publish.now", {"piece_id": piece_id})
    done = await _wait_for(client, run["id"], ("done", "failed"), headers=H(ws_id))

    assert done["status"] == "done"
    assert done["result"]["data"]["status"] == "retry_scheduled" and done["result"]["data"]["code"] == "platform_unavailable"


async def test_a_single_publish_job_that_is_refused_ends_with_the_reason_not_a_crash(signup_user, monkeypatch):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Publish Jobs 3")
    piece_id = await _piece(ws_id, profile["id"], platform="LinkedIn")

    def refuse(body):
        raise HTTPException(status_code=409, detail={"code": "PLATFORM_PAUSED", "message": "LinkedIn is paused for now."})

    _stub_publish(monkeypatch, refuse)
    run = await _start(client, ws_id, "publish.now", {"piece_id": piece_id})
    done = await _wait_for(client, run["id"], ("done", "failed"), headers=H(ws_id))

    assert done["status"] == "done"
    assert done["result"]["data"]["code"] == "PLATFORM_PAUSED" and done["result"]["data"]["success"] is False


async def test_a_batch_is_limited_to_fifty_posts_and_needs_at_least_one(signup_user):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Publish Jobs 4")

    too_many = await client.post("/api/v1/jobs", json={"action": "publish.batch", "payload": {"piece_ids": [str(i) for i in range(51)]}}, headers=H(ws_id))
    none = await client.post("/api/v1/jobs", json={"action": "publish.batch", "payload": {"piece_ids": []}}, headers=H(ws_id))

    assert too_many.status_code == 422 and none.status_code == 422


# ── The sign-in popup ─────────────────────────────────────────────────

def _message_from(html: str) -> dict:
    match = re.search(r"postMessage\((\{.*?\}), (\".*?\")\);", html, re.S)
    assert match, html
    literal = match.group(1).replace("type:", '"type":').replace("success:", '"success":').replace("message:", '"message":')
    return json.loads(literal)


def test_the_popup_tells_the_page_how_the_sign_in_went():
    ok = oauth_module._oauth_popup_response(True, "LinkedIn connected").body.decode()
    failed = oauth_module._oauth_popup_response(False, "No Pages were found for this account.").body.decode()

    assert _message_from(ok) == {"type": "recast_oauth", "success": True, "message": "LinkedIn connected"}
    assert _message_from(failed) == {"type": "recast_oauth", "success": False, "message": "No Pages were found for this account."}
    assert "window.opener" in ok


def test_text_in_the_popup_message_cannot_break_out_of_the_page_script():
    page = oauth_module._oauth_popup_response(False, 'Bad </script><script>alert(1)</script> "quote"').body.decode()

    assert page.count("<script>") == 1 and page.count("</script>") == 1
    assert "<script>alert(1)" not in page
