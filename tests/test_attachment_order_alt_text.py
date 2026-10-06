"""A post's picture list: putting the pictures in order, writing what each one shows, and what a form can read about them. The
publish side must send the pictures in that order with the member's own descriptions. Assets are seeded straight into the database."""
from app.db.mongo import content_pieces, media_assets
from app.pipelines.publish.spine import media_for_publish
from tests.conftest import create_workspace
from tests.test_attachments import H, _attach, _get, _media, _piece


async def _post_with_pictures(client, profile, name: str, count: int = 3):
    ws_id = await create_workspace(client, name)
    piece_id = await _piece(ws_id, profile["id"], platform="Bluesky")
    media_ids = []
    for index in range(count):
        media_id = await _media(ws_id, size_bytes=1000 * (index + 1), width=800, height=600)
        media_ids.append(media_id)
        res = await _attach(client, ws_id, piece_id, asset_type="upload", media_id=media_id)
        assert res.status_code == 200, res.text
    return ws_id, piece_id, media_ids


async def test_the_list_carries_what_a_form_needs_to_check_each_picture(signup_user):
    client, profile = await signup_user()
    ws_id, piece_id, media_ids = await _post_with_pictures(client, profile, "Order WS 1", 2)

    listed = await _get(client, ws_id, piece_id)

    assert [a["media_id"] for a in listed] == media_ids
    assert (listed[0]["mime_type"], listed[0]["width"], listed[0]["height"], listed[0]["size_bytes"]) == ("image/png", 800, 600, 1000)
    assert listed[1]["size_bytes"] == 2000


async def test_alt_text_can_be_written_changed_and_cleared_for_each_picture(signup_user):
    client, profile = await signup_user()
    ws_id, piece_id, _ = await _post_with_pictures(client, profile, "Alt WS 1")
    listed = await _get(client, ws_id, piece_id)
    first, second = listed[0]["id"], listed[1]["id"]

    res = await client.patch(f"/api/v1/content/pieces/{piece_id}/attachments/{second}", json={"alt_text": "  A chart of review time  "}, headers=H(ws_id))
    assert res.status_code == 200, res.text
    assert res.json()["attachment"]["alt_text"] == "A chart of review time"
    assert (await _get(client, ws_id, piece_id))[0]["alt_text"] is None

    # The first picture is the post's primary one: its description is kept on what gets published too.
    await client.patch(f"/api/v1/content/pieces/{piece_id}/attachments/{first}", json={"alt_text": "The cover"}, headers=H(ws_id))
    assert (await content_pieces.find_one({"piece_id": piece_id}))["media"][0]["alt_text"] == "The cover"

    await client.patch(f"/api/v1/content/pieces/{piece_id}/attachments/{first}", json={"alt_text": ""}, headers=H(ws_id))
    assert (await _get(client, ws_id, piece_id))[0]["alt_text"] is None
    assert (await content_pieces.find_one({"piece_id": piece_id}))["media"][0].get("alt_text") is None


async def test_alt_text_is_limited_and_only_for_attachments_on_the_post(signup_user):
    client, profile = await signup_user()
    ws_id, piece_id, _ = await _post_with_pictures(client, profile, "Alt WS 2", 1)
    attachment = (await _get(client, ws_id, piece_id))[0]["id"]

    too_long = await client.patch(f"/api/v1/content/pieces/{piece_id}/attachments/{attachment}", json={"alt_text": "x" * 1001}, headers=H(ws_id))
    missing = await client.patch(f"/api/v1/content/pieces/{piece_id}/attachments/nope", json={"alt_text": "Hi"}, headers=H(ws_id))

    assert too_long.status_code == 422
    assert missing.status_code == 404


async def test_pictures_can_be_put_in_a_new_order_and_the_first_becomes_the_primary(signup_user):
    client, profile = await signup_user()
    ws_id, piece_id, media_ids = await _post_with_pictures(client, profile, "Order WS 2")
    ids = [a["id"] for a in await _get(client, ws_id, piece_id)]
    await client.patch(f"/api/v1/content/pieces/{piece_id}/attachments/{ids[2]}", json={"alt_text": "Third one"}, headers=H(ws_id))

    res = await client.put(f"/api/v1/content/pieces/{piece_id}/attachments/order", json={"attachment_ids": [ids[2], ids[0], ids[1]]}, headers=H(ws_id))

    assert res.status_code == 200, res.text
    assert [a["id"] for a in res.json()["attachments"]] == [ids[2], ids[0], ids[1]]
    piece = await content_pieces.find_one({"piece_id": piece_id})
    assert piece["media"][0]["id"] == media_ids[2]
    assert piece["media"][0]["alt_text"] == "Third one"


async def test_an_order_must_name_every_picture_once(signup_user):
    client, profile = await signup_user()
    ws_id, piece_id, _ = await _post_with_pictures(client, profile, "Order WS 3")
    ids = [a["id"] for a in await _get(client, ws_id, piece_id)]

    for bad in ([ids[0], ids[1]], [ids[0], ids[0], ids[1]], [ids[0], ids[1], "nope"]):
        res = await client.put(f"/api/v1/content/pieces/{piece_id}/attachments/order", json={"attachment_ids": bad}, headers=H(ws_id))
        assert res.status_code == 422, bad
    assert [a["id"] for a in await _get(client, ws_id, piece_id)] == ids


async def test_a_published_post_cannot_be_reordered_or_described(signup_user):
    client, profile = await signup_user()
    ws_id, piece_id, _ = await _post_with_pictures(client, profile, "Order WS 4", 2)
    ids = [a["id"] for a in await _get(client, ws_id, piece_id)]
    await content_pieces.update_one({"piece_id": piece_id}, {"$set": {"publish_status": "published"}})

    order = await client.put(f"/api/v1/content/pieces/{piece_id}/attachments/order", json={"attachment_ids": ids[::-1]}, headers=H(ws_id))
    alt = await client.patch(f"/api/v1/content/pieces/{piece_id}/attachments/{ids[0]}", json={"alt_text": "Hi"}, headers=H(ws_id))

    assert order.status_code == 409 and alt.status_code == 409


async def test_a_picture_attached_with_a_description_keeps_it(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Alt WS 3")
    piece_id = await _piece(ws_id, profile["id"])
    media_id = await _media(ws_id)

    res = await _attach(client, ws_id, piece_id, asset_type="upload", media_id=media_id, alt_text="A team photo")

    assert res.json()["attachment"]["alt_text"] == "A team photo"
    assert (await content_pieces.find_one({"piece_id": piece_id}))["media"][0]["alt_text"] == "A team photo"


async def test_publishing_sends_the_pictures_in_the_chosen_order_with_the_members_descriptions(signup_user):
    client, profile = await signup_user()
    ws_id, piece_id, media_ids = await _post_with_pictures(client, profile, "Order WS 5")
    ids = [a["id"] for a in await _get(client, ws_id, piece_id)]
    await client.patch(f"/api/v1/content/pieces/{piece_id}/attachments/{ids[1]}", json={"alt_text": "Second picture"}, headers=H(ws_id))
    await client.put(f"/api/v1/content/pieces/{piece_id}/attachments/order", json={"attachment_ids": [ids[1], ids[2], ids[0]]}, headers=H(ws_id))
    await media_assets.update_one({"id": media_ids[0]}, {"$set": {"alt_text": "From the library"}})

    piece = await content_pieces.find_one({"piece_id": piece_id})
    sent = await media_for_publish(piece, ws_id, "Bluesky")

    assert [m["id"] for m in sent] == [media_ids[1], media_ids[2], media_ids[0]]
    assert sent[0]["alt_text"] == "Second picture"
    assert sent[2]["alt_text"] == "From the library"
