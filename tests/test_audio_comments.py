"""Tests for review comments pinned to a moment in an audio recording."""
from tests.conftest import invite_and_accept
from tests.test_audio_assets import _generate, _h, _setup, stubs  # noqa: F401 — fixture reuse


async def _add(client, ws_id, asset_id, time_s, text="Tighten this pause."):
    return await client.post(
        f"/api/v1/audio-assets/{asset_id}/comments", json={"time_s": time_s, "text": text}, headers=_h(ws_id),
    )


async def test_comments_record_the_real_author_and_come_back_in_time_order(signup_user, stubs):
    client, profile, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()

    late = await _add(client, ws_id, asset["id"], 42.5, "Levels jump here.")
    early = await _add(client, ws_id, asset["id"], 3.0)
    assert late.status_code == early.status_code == 201, late.text
    assert early.json()["user_id"] == profile["id"]
    assert early.json()["resolved"] is False

    listed = (await client.get(f"/api/v1/audio-assets/{asset['id']}/comments", headers=_h(ws_id))).json()
    assert [c["time_s"] for c in listed] == [3.0, 42.5]


async def test_comment_validation(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()

    assert (await _add(client, ws_id, asset["id"], 1.0, "   ")).status_code == 400
    assert (await _add(client, ws_id, asset["id"], 1.0, "x" * 1001)).status_code == 400
    assert (await _add(client, ws_id, asset["id"], -1.0)).status_code == 400
    assert (await _add(client, ws_id, "nope", 1.0)).status_code == 404


async def test_a_comment_can_be_resolved_and_reopened(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()
    comment = (await _add(client, ws_id, asset["id"], 5.0)).json()
    url = f"/api/v1/audio-assets/{asset['id']}/comments/{comment['id']}"

    done = await client.patch(url, json={"resolved": True}, headers=_h(ws_id))
    assert done.status_code == 200 and done.json()["resolved"] is True
    reopened = await client.patch(url, json={"resolved": False}, headers=_h(ws_id))
    assert reopened.json()["resolved"] is False


async def test_only_the_author_or_an_admin_can_delete(signup_user, make_client, stubs):
    owner, _, ws_id, brand_id = await _setup(signup_user)
    editor, _ = await invite_and_accept(owner, make_client, ws_id, "editor")
    asset = (await _generate(owner, ws_id, brand_id)).json()

    # An editor can't delete a note someone else wrote...
    owners_note = (await _add(owner, ws_id, asset["id"], 2.0, "Owner note")).json()
    forbidden = await editor.delete(
        f"/api/v1/audio-assets/{asset['id']}/comments/{owners_note['id']}", headers=_h(ws_id),
    )
    assert forbidden.status_code == 403

    # ...but can delete their own.
    mine = (await _add(editor, ws_id, asset["id"], 5.0, "Editor note")).json()
    own = await editor.delete(f"/api/v1/audio-assets/{asset['id']}/comments/{mine['id']}", headers=_h(ws_id))
    assert own.status_code == 204

    # The owner (an admin-level role) can delete anyone's.
    theirs = (await _add(editor, ws_id, asset["id"], 9.0, "Another")).json()
    override = await owner.delete(
        f"/api/v1/audio-assets/{asset['id']}/comments/{theirs['id']}", headers=_h(ws_id),
    )
    assert override.status_code == 204
    left = (await owner.get(f"/api/v1/audio-assets/{asset['id']}/comments", headers=_h(ws_id))).json()
    assert [c["id"] for c in left] == [owners_note["id"]]


async def test_comments_stay_inside_their_workspace(signup_user, stubs):
    owner, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(owner, ws_id, brand_id)).json()
    comment = (await _add(owner, ws_id, asset["id"], 1.0)).json()

    other, _, other_ws, _ = await _setup(signup_user)
    assert (await other.get(f"/api/v1/audio-assets/{asset['id']}/comments", headers=_h(other_ws))).status_code == 404
    assert (await _add(other, other_ws, asset["id"], 1.0)).status_code == 404
    gone = await other.delete(
        f"/api/v1/audio-assets/{asset['id']}/comments/{comment['id']}", headers=_h(other_ws),
    )
    assert gone.status_code == 404
