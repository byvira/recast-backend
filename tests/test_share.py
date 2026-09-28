"""Tests for the public share endpoint (`GET /api/v1/share/{token}`) and the
share-link list and revoke endpoints that sit behind the Inspector's
"Stop sharing" button. The public route must work with no login, return
the same 404 for unknown, expired and revoked tokens, and never expose
workspace, user or brand ids.
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.db.mongo import audio_share_links, image_assets, media_assets
from tests.test_audio_assets import _generate, _h, _setup, stubs  # noqa: F401 — fixture reuse


async def _audio_share(client, ws_id, brand_id):
    asset = (await _generate(client, ws_id, brand_id, title="Shared Episode")).json()
    res = await client.post(f"/api/v1/audio-assets/{asset['id']}/share-link", headers=_h(ws_id))
    assert res.status_code == 201, res.text
    return asset, res.json()


async def test_public_audio_share_needs_no_login_and_leaks_no_ids(signup_user, make_client, stubs):
    client, profile, ws_id, brand_id = await _setup(signup_user)
    asset, link = await _audio_share(client, ws_id, brand_id)

    public = make_client()  # its own cookie jar: not logged in
    res = await public.get(f"/api/v1/share/{link['token']}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["kind"] == "audio"
    assert body["title"] == "Shared Episode"
    assert body["media"]["url"].startswith("https://")
    assert body["slides"] == []

    raw = res.text
    for private in (ws_id, brand_id, profile["id"], asset["id"]):
        assert private not in raw


async def test_unknown_expired_and_revoked_tokens_all_return_the_same_404(signup_user, make_client, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    public = make_client()

    unknown = await public.get("/api/v1/share/not-a-real-token")
    assert unknown.status_code == 404

    _, expired_link = await _audio_share(client, ws_id, brand_id)
    await audio_share_links.update_one(
        {"token": expired_link["token"]},
        {"$set": {"expires_at": datetime.now(timezone.utc) - timedelta(days=1)}},
    )
    expired = await public.get(f"/api/v1/share/{expired_link['token']}")

    asset, revoked_link = await _audio_share(client, ws_id, brand_id)
    revoke = await client.delete(
        f"/api/v1/audio-assets/{asset['id']}/share-link/{revoked_link['token']}", headers=_h(ws_id),
    )
    assert revoke.status_code == 204
    revoked = await public.get(f"/api/v1/share/{revoked_link['token']}")

    assert expired.status_code == revoked.status_code == 404
    assert unknown.json() == expired.json() == revoked.json()


async def test_list_shows_only_live_links_and_revoke_removes_one(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset, first = await _audio_share(client, ws_id, brand_id)
    second = (await client.post(f"/api/v1/audio-assets/{asset['id']}/share-link", headers=_h(ws_id))).json()

    listed = (await client.get(f"/api/v1/audio-assets/{asset['id']}/share-links", headers=_h(ws_id))).json()
    assert {l["token"] for l in listed} == {first["token"], second["token"]}

    await client.delete(f"/api/v1/audio-assets/{asset['id']}/share-link/{first['token']}", headers=_h(ws_id))
    listed = (await client.get(f"/api/v1/audio-assets/{asset['id']}/share-links", headers=_h(ws_id))).json()
    assert [l["token"] for l in listed] == [second["token"]]

    missing = await client.delete(f"/api/v1/audio-assets/{asset['id']}/share-link/nope", headers=_h(ws_id))
    assert missing.status_code == 404


async def test_another_workspace_cannot_revoke_or_list_my_links(signup_user, stubs):
    owner, _, ws_id, brand_id = await _setup(signup_user)
    asset, link = await _audio_share(owner, ws_id, brand_id)

    other, _, other_ws, _ = await _setup(signup_user)
    revoke = await other.delete(
        f"/api/v1/audio-assets/{asset['id']}/share-link/{link['token']}", headers=_h(other_ws),
    )
    assert revoke.status_code == 404
    listed = await other.get(f"/api/v1/audio-assets/{asset['id']}/share-links", headers=_h(other_ws))
    assert listed.status_code == 404


async def test_public_image_share_lists_slides_in_order(signup_user, make_client, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    now = datetime.now(timezone.utc)
    media_ids = []
    for n in (1, 2):
        mid = uuid4().hex
        media_ids.append(mid)
        await media_assets.insert_one({
            "id": mid, "workspace_id": ws_id, "kind": "image", "url": f"https://res.example.com/{mid}.png",
            "mime_type": "image/png", "source": "ai_generated", "created_by": "u", "created_at": now,
        })
    image_id = uuid4().hex
    await image_assets.insert_one({
        "id": image_id, "workspace_id": ws_id, "brand_id": brand_id, "created_by": "u",
        "created_at": now, "updated_at": now, "title": "Launch Carousel",
        "slides": [
            {"slide_number": 2, "title": "Second", "slide_type": "content", "media_id": media_ids[1]},
            {"slide_number": 1, "title": "First", "slide_type": "cover", "media_id": media_ids[0]},
        ],
    })
    link = (await client.post(f"/api/v1/image-assets/{image_id}/share-link", headers=_h(ws_id))).json()

    res = await make_client().get(f"/api/v1/share/{link['token']}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["kind"] == "image"
    assert [s["title"] for s in body["slides"]] == ["First", "Second"]
    assert body["slides"][0]["media"]["url"].endswith(".png")
    assert ws_id not in res.text and brand_id not in res.text
