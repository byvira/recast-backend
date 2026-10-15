"""Trust controls for video: staff can switch video off for one workspace, a workspace can keep its audio away from outside services, and
consent for voices and faces is recorded, withdrawn and checked in one place."""
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.core import auth
from app.db.mongo import get_db, media_assets
from app.video import governance, jobs
from tests.conftest import create_workspace
from tests.test_video_worker_routes import INPUTS, _edit

G = "/api/v1/video-governance"


def _staff_on(app):
    async def staff():
        return {"id": "staff-1"}

    app.dependency_overrides[auth.require_platform_staff] = staff


async def test_video_is_on_by_default_and_staff_can_switch_it_off_and_on_for_one_workspace(signup_user):
    from app.main import app

    client, _ = await signup_user()
    ws = await create_workspace(client, f"Access {uuid4().hex[:6]}")
    other = await create_workspace(client, f"Other {uuid4().hex[:6]}")
    assert await governance.video_enabled(ws) is True

    _staff_on(app)
    try:
        off = await client.put(f"/api/v1/ops/video/workspaces/{ws}/access", json={"enabled": False, "note": "Closed group only"})
        missing = await client.put("/api/v1/ops/video/workspaces/nope/access", json={"enabled": False})
    finally:
        app.dependency_overrides.pop(auth.require_platform_staff, None)

    assert off.status_code == 200 and missing.status_code == 404
    assert await governance.video_enabled(ws) is False and await governance.video_enabled(other) is True
    with pytest.raises(jobs.JobError) as err:
        await jobs.enqueue(workspace_id=ws, user_id="u1", inputs=INPUTS, edit=_edit())
    assert err.value.status == 403 and "not switched on" in err.value.message
    assert (await client.post("/api/v1/video-projects", headers={"X-Workspace-Id": ws}, json={"source": "blank"})).status_code == 403
    upload = await client.post("/api/v1/video-media/uploads", headers={"X-Workspace-Id": ws}, json={"filename": "a.mp4", "content_type": "video/mp4", "bytes": 1000})
    assert upload.status_code == 403
    assert (await client.post("/api/v1/video-projects", headers={"X-Workspace-Id": other}, json={"source": "blank"})).status_code == 201

    _staff_on(app)
    try:
        await client.put(f"/api/v1/ops/video/workspaces/{ws}/access", json={"enabled": True})
    finally:
        app.dependency_overrides.pop(auth.require_platform_staff, None)
    assert (await client.post("/api/v1/video-projects", headers={"X-Workspace-Id": ws}, json={"source": "blank"})).status_code == 201


async def test_a_workspace_that_keeps_its_audio_private_cannot_have_words_written_out(signup_user):
    client, _ = await signup_user()
    ws = await create_workspace(client, f"Private {uuid4().hex[:6]}")
    h = {"X-Workspace-Id": ws}
    mid = uuid4().hex
    await media_assets.insert_one({"id": mid, "workspace_id": ws, "kind": "video", "url": "https://x/v.mp4", "mime_type": "video/mp4", "duration_s": 30.0,
                                   "created_by": "u1", "created_at": datetime.now(timezone.utc)})

    assert (await client.get(f"{G}/privacy", headers=h)).json() == {"keep_audio_private": False}
    on = await client.put(f"{G}/privacy", headers=h, json={"keep_audio_private": True})
    refused = await client.post(f"/api/v1/video-transcripts/{mid}", headers=h, json={})

    assert on.status_code == 200 and on.json() == {"keep_audio_private": True}
    assert refused.status_code == 409 and "stay away from outside services" in refused.json()["detail"]
    assert await get_db()["video_transcripts"].count_documents({"workspace_id": ws}) == 0


async def test_waiting_transcripts_of_a_private_workspace_are_not_carried_on(monkeypatch):
    from app.video import transcribe

    ws = f"ws-{uuid4()}"
    await get_db()["workspaces"].insert_one({"id": ws, "video_privacy": {"keep_audio_private": True}})
    tid = uuid4().hex
    await get_db()["video_transcripts"].insert_one({"id": tid, "workspace_id": ws, "media_id": "m", "user_id": "u", "status": "queued", "lease_until": None})
    ran = []

    async def fake_run(transcript_id, **kw):
        ran.append(transcript_id)
        return {"status": "done"}

    monkeypatch.setattr(transcribe, "run", fake_run)
    await transcribe.continue_waiting()

    assert tid not in ran


# ── consent ──────────────────────────────────────────────────────────────────────

GOOD = {"kind": "voice", "person": "Asha Rao", "scope": "Making videos in her voice for the company's own channels",
        "evidence": "Signed form, kept in the team drive", "given_on": "2026-09-30"}


async def test_consent_is_recorded_listed_checked_and_withdrawn(signup_user):
    client, _ = await signup_user()
    ws = await create_workspace(client, f"Consent {uuid4().hex[:6]}")
    h = {"X-Workspace-Id": ws}
    assert await governance.has_consent(ws, "voice", "Asha Rao") is False

    made = await client.post(f"{G}/consents", headers=h, json=GOOD)

    assert made.status_code == 201 and made.json()["withdrawn"] is False and made.json()["given_on"] == "2026-09-30"
    assert await governance.has_consent(ws, "voice", "asha rao") is True          # a name is matched without regard to case
    assert await governance.has_consent(ws, "face", "Asha Rao") is False          # a voice is not a face
    assert await governance.has_consent("another-workspace", "voice", "Asha Rao") is False
    assert (await client.delete(f"{G}/consents/{made.json()['id']}", headers=h)).status_code == 204
    assert await governance.has_consent(ws, "voice", "Asha Rao") is False
    listed = (await client.get(f"{G}/consents", headers=h)).json()["consents"]
    assert len(listed) == 1 and listed[0]["withdrawn"] is True and listed[0]["withdrawn_at"]          # the record stays
    assert (await client.delete(f"{G}/consents/{made.json()['id']}", headers=h)).status_code == 404


@pytest.mark.parametrize("change,fragment", [
    ({"person": "  "}, "whose voice or face"),
    ({"scope": "ok"}, "what they agreed to"),
    ({"evidence": "no"}, "how they agreed"),
    ({"given_on": "30 Sept"}, "year, month and day"),
])
async def test_a_consent_record_must_say_who_what_how_and_when(signup_user, change, fragment):
    client, _ = await signup_user()
    ws = await create_workspace(client, f"Consent {uuid4().hex[:6]}")

    out = await client.post(f"{G}/consents", headers={"X-Workspace-Id": ws}, json={**GOOD, **change})

    assert out.status_code == 400 and fragment in out.json()["detail"]


async def test_an_ai_made_video_gets_the_disclosure_advice_when_attached():
    from app.pipelines.publish.attachments import AI_NOTE, attach_advice

    ai = attach_advice({"asset_type": "project", "media": {"contains_ai": True, "origin": "mixed"}}, {"platform": "LinkedIn"})
    plain = attach_advice({"asset_type": "project", "media": {"contains_ai": False, "origin": "own"}}, {"platform": "LinkedIn"})
    assert AI_NOTE in ai and AI_NOTE not in plain


async def test_a_render_that_used_ai_material_carries_a_label_to_the_worker_and_one_that_did_not_does_not():
    from app.video import jobs

    ws = f"ws-{uuid4()}"
    ai = await jobs.enqueue(workspace_id=ws, user_id="u1", inputs=INPUTS, edit=_edit(), origin={"origin": "mixed", "contains_ai": True, "contains_stock": False})
    own = await jobs.enqueue(workspace_id=ws, user_id="u1", inputs=INPUTS, edit=_edit(), origin={"origin": "own", "contains_ai": False, "contains_stock": False})

    stored_ai = await get_db()["video_jobs"].find_one({"id": ai["id"]})
    stored_own = await get_db()["video_jobs"].find_one({"id": own["id"]})
    assert stored_ai["output"]["label"] == "Includes AI-made material" and "label" not in stored_own["output"]
