"""Notes on a post, a recording or a picture inside the workspace: anyone in the workspace can read and write, an editor marks them done,
and a person can always remove their own. One model for all three editors."""
from uuid import uuid4

from app.db.mongo import audio_assets, brand_profiles, content_pieces
from tests.conftest import create_workspace


async def _piece(signup_user):
    client, profile = await signup_user()
    ws = await create_workspace(client, "Notes WS", tier="large")
    res = await client.post("/api/v1/brand/", json={"brand_type": "Person"}, headers={"X-Workspace-Id": ws})
    brand_id = res.json()["brand_profile_id"]
    await brand_profiles.update_one({"id": brand_id}, {"$set": {"is_complete": True}})
    made = await client.post("/api/v1/text/pieces/manual", json={"platform": "LinkedIn", "brand_id": brand_id, "content": "A post to review."}, headers={"X-Workspace-Id": ws})
    return client, ws, made.json()["piece_id"]


async def test_a_note_replies_and_marking_it_done_work_on_a_post(signup_user):
    client, ws, pid = await _piece(signup_user)
    head = {"X-Workspace-Id": ws}
    made = await client.post("/api/v1/editor-comments", json={"target_type": "piece", "target_id": pid, "text": "Shorter opening?", "anchor": {"block_id": "b1", "evil": "x"}}, headers=head)
    assert made.status_code == 201, made.text
    note = made.json()
    assert note["anchor"] == {"block_id": "b1"} and note["resolved"] is False

    replied = await client.post(f"/api/v1/editor-comments/{note['id']}/replies", json={"text": "Done, tightened."}, headers=head)
    assert replied.status_code == 201 and len(replied.json()["replies"]) == 1
    listed = (await client.get("/api/v1/editor-comments", params={"target_type": "piece", "target_id": pid}, headers=head)).json()
    assert listed["open"] == 1 and listed["comments"][0]["text"] == "Shorter opening?"

    done = await client.put(f"/api/v1/editor-comments/{note['id']}/resolved", json={"resolved": True}, headers=head)
    assert done.status_code == 200 and done.json()["resolved"] is True
    assert (await client.get("/api/v1/editor-comments", params={"target_type": "piece", "target_id": pid}, headers=head)).json()["open"] == 0

    gone = await client.delete(f"/api/v1/editor-comments/{note['id']}", headers=head)
    assert gone.status_code == 204


async def test_notes_need_work_that_exists_in_this_workspace_and_text_that_is_not_empty(signup_user):
    client, ws, pid = await _piece(signup_user)
    head = {"X-Workspace-Id": ws}
    assert (await client.post("/api/v1/editor-comments", json={"target_type": "piece", "target_id": "nope", "text": "x"}, headers=head)).status_code == 404
    assert (await client.post("/api/v1/editor-comments", json={"target_type": "audio", "target_id": pid, "text": "x"}, headers=head)).status_code == 404
    assert (await client.post("/api/v1/editor-comments", json={"target_type": "piece", "target_id": pid, "text": "   "}, headers=head)).status_code == 400
    assert (await client.get("/api/v1/editor-comments", params={"target_type": "image", "target_id": uuid4().hex}, headers=head)).status_code == 404


async def test_another_workspace_cannot_see_or_change_notes(signup_user):
    client, ws, pid = await _piece(signup_user)
    note = (await client.post("/api/v1/editor-comments", json={"target_type": "piece", "target_id": pid, "text": "Private"}, headers={"X-Workspace-Id": ws})).json()
    other, _ = await signup_user()
    other_ws = await create_workspace(other, "Other", tier="large")
    head = {"X-Workspace-Id": other_ws}
    assert (await other.get("/api/v1/editor-comments", params={"target_type": "piece", "target_id": pid}, headers=head)).status_code == 404
    assert (await other.put(f"/api/v1/editor-comments/{note['id']}/resolved", json={"resolved": True}, headers=head)).status_code == 404
    assert (await other.delete(f"/api/v1/editor-comments/{note['id']}", headers=head)).status_code == 404
