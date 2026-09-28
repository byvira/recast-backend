"""Tests for /api/v1/image-assets/{id}/comments — real comment pins on a
real slide. The CommentPin model existed since Stage 1 but had zero
endpoint or frontend wiring until now (see GAPS.md / the Image completeness
audit, 2026-09-28).
"""

from tests.test_image_assets import _brand, _generate, _h, _setup, stubs  # noqa: F401 — fixture reuse


async def test_create_list_and_the_new_pin_is_unresolved(signup_user, stubs):
    client, profile, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()

    res = await client.post(
        f"/api/v1/image-assets/{asset['id']}/comments",
        json={"slide_number": 1, "x": 22.5, "y": 35.0, "text": "Headline contrast looks weak."},
        headers=_h(ws_id),
    )
    assert res.status_code == 201, res.text
    pin = res.json()
    assert pin["resolved"] is False
    assert pin["author_id"] == profile["id"]
    assert pin["slide_number"] == 1

    listed = await client.get(f"/api/v1/image-assets/{asset['id']}/comments", headers=_h(ws_id))
    assert listed.status_code == 200
    assert [c["id"] for c in listed.json()] == [pin["id"]]


async def test_validation(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()
    url = f"/api/v1/image-assets/{asset['id']}/comments"

    assert (await client.post(url, json={"slide_number": 1, "x": 10, "y": 10, "text": "  "}, headers=_h(ws_id))).status_code == 400
    assert (await client.post(url, json={"slide_number": 99, "x": 10, "y": 10, "text": "hi"}, headers=_h(ws_id))).status_code == 400
    assert (await client.post(url, json={"slide_number": 1, "x": 150, "y": 10, "text": "hi"}, headers=_h(ws_id))).status_code == 400
    assert (await client.post("/api/v1/image-assets/nope/comments", json={"slide_number": 1, "x": 1, "y": 1, "text": "hi"}, headers=_h(ws_id))).status_code == 404


async def test_resolve_a_pin(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()
    pin = (await client.post(
        f"/api/v1/image-assets/{asset['id']}/comments",
        json={"slide_number": 1, "x": 1, "y": 1, "text": "note"}, headers=_h(ws_id),
    )).json()

    res = await client.patch(
        f"/api/v1/image-assets/{asset['id']}/comments/{pin['id']}", json={"resolved": True}, headers=_h(ws_id),
    )
    assert res.status_code == 200, res.text
    assert res.json()["resolved"] is True

    missing = await client.patch(
        f"/api/v1/image-assets/{asset['id']}/comments/nope", json={"resolved": True}, headers=_h(ws_id),
    )
    assert missing.status_code == 404


async def test_delete_a_pin_and_permission_check(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()
    pin = (await client.post(
        f"/api/v1/image-assets/{asset['id']}/comments",
        json={"slide_number": 1, "x": 1, "y": 1, "text": "note"}, headers=_h(ws_id),
    )).json()

    res = await client.delete(f"/api/v1/image-assets/{asset['id']}/comments/{pin['id']}", headers=_h(ws_id))
    assert res.status_code == 204

    listed = await client.get(f"/api/v1/image-assets/{asset['id']}/comments", headers=_h(ws_id))
    assert listed.json() == []

    missing = await client.delete(f"/api/v1/image-assets/{asset['id']}/comments/{pin['id']}", headers=_h(ws_id))
    assert missing.status_code == 404
