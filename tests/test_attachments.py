"""Tests for attaching images, recordings and videos to a post, and for the audio
send-to-draft step. Assets and media are seeded straight into the database; no
generation, provider or platform is involved.
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from app.db.mongo import audio_assets, content_pieces, image_assets, media_assets
from app.pipelines.publish.base import PublishResult
from app.pipelines.publish.token_store import save_token
from app.pipelines.text.storage import ensure_session_exists, save_live_piece
from tests.conftest import create_workspace

H = lambda ws: {"X-Workspace-Id": ws}  # noqa: E731


async def _piece(ws_id: str, user_id: str, platform: str = "LinkedIn") -> str:
    session_id = str(uuid4())
    await ensure_session_exists(
        session_id=session_id, workspace_id=ws_id, user_id=user_id, brand_id="brand-1", source_type="text",
    )
    return await save_live_piece(
        session_id=session_id, workspace_id=ws_id, user_id=user_id, brand_id="brand-1", platform=platform,
        content="A real post.", word_count=3, char_count=12,
    )


async def _media(ws_id: str, kind: str = "image", **extra) -> str:
    mid = uuid4().hex
    mime = {"image": "image/png", "video": "video/mp4", "audio": "audio/mpeg"}[kind]
    await media_assets.insert_one({
        "id": mid, "workspace_id": ws_id, "kind": kind, "url": f"https://cdn.example/{mid}", "mime_type": mime,
        "source": "uploaded", "created_by": "u", "created_at": datetime.now(timezone.utc), **extra,
    })
    return mid


async def _image(ws_id: str, *, slides: int = 1, **extra) -> tuple[str, list[str]]:
    aid = uuid4().hex
    media = [await _media(ws_id) for _ in range(slides)]
    await image_assets.insert_one({
        "id": aid, "workspace_id": ws_id, "brand_id": "brand-1", "title": "Card", "version_count": 1, "approval_status": "approved",
        "slides": [{"slide_number": i + 1, "title": f"s{i}", "slide_type": "x", "media_id": m} for i, m in enumerate(media)],
        **extra,
    })
    return aid, media


async def _audio(ws_id: str, *, with_clip: bool = False, **extra) -> dict:
    aid = uuid4().hex
    media = await _media(ws_id, "audio")
    doc = {
        "id": aid, "workspace_id": ws_id, "brand_id": "brand-1", "title": "Episode", "version_count": 1, "approval_status": "approved",
        "media_id": media, "video_clips": [], **extra,
    }
    clip_media = clip_id = None
    if with_clip:
        clip_media, clip_id = await _media(ws_id, "video"), uuid4().hex
        doc["video_clips"] = [{
            "id": clip_id, "media_id": clip_media, "start_s": 0.0, "end_s": 10.0, "style": "cover", "size": "square",
            "title": "Best bit", "platform": "linkedin", "notes": [], "created_by": "u", "created_at": datetime.now(timezone.utc),
        }]
    await audio_assets.insert_one(doc)
    return {"id": aid, "media": media, "clip_media": clip_media, "clip_id": clip_id}


async def _attach(client, ws_id: str, piece_id: str, **body):
    return await client.post(f"/api/v1/content/pieces/{piece_id}/attachments", json=body, headers=H(ws_id))


async def _get(client, ws_id: str, piece_id: str) -> list[dict]:
    res = await client.get(f"/api/v1/content/pieces/{piece_id}/attachments", headers=H(ws_id))
    assert res.status_code == 200, res.text
    return res.json()["attachments"]


async def test_attach_an_image_sets_the_publishable_media_and_the_backlink(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Attach WS 1")
    piece_id = await _piece(ws_id, profile["id"])
    image_id, (media_id,) = await _image(ws_id)

    res = await _attach(client, ws_id, piece_id, asset_type="image", asset_id=image_id)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["created"] is True and body["warning"] is None
    att = body["attachment"]
    assert (att["asset_type"], att["asset_id"], att["media_id"], att["slide_number"]) == ("image", image_id, media_id, 1)
    assert att["asset_version"] == 1 and att["piece_version"] == 1 and att["attached_by"] == profile["id"]
    assert att["stale"] is False and att["stale_reason"] is None

    piece = await content_pieces.find_one({"piece_id": piece_id})
    assert [m["id"] for m in piece["media"]] == [media_id]  # what the publishers read
    asset = await image_assets.find_one({"id": image_id})
    assert [lp["piece_id"] for lp in asset["linked_pieces"]] == [piece_id]


async def test_attaching_the_same_asset_again_returns_the_same_attachment(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Attach WS 2")
    piece_id = await _piece(ws_id, profile["id"])
    image_id, _ = await _image(ws_id)

    first = (await _attach(client, ws_id, piece_id, asset_type="image", asset_id=image_id)).json()
    again = (await _attach(client, ws_id, piece_id, asset_type="image", asset_id=image_id)).json()
    assert again["created"] is False
    assert again["attachment"]["id"] == first["attachment"]["id"]
    assert len(await _get(client, ws_id, piece_id)) == 1
    assert len((await image_assets.find_one({"id": image_id}))["linked_pieces"]) == 1


async def test_unknown_and_foreign_assets_are_refused_plainly(signup_user, make_client):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Attach WS 3")
    piece_id = await _piece(ws_id, profile["id"])

    missing = await _attach(client, ws_id, piece_id, asset_type="image", asset_id="nope")
    assert missing.status_code == 404 and missing.json()["detail"] == "Image not found."

    other_client, other_profile = await signup_user()
    other_ws = await create_workspace(other_client, "Other WS")
    foreign_image, _ = await _image(other_ws)
    foreign = await _attach(client, ws_id, piece_id, asset_type="image", asset_id=foreign_image)
    assert foreign.status_code == 404

    empty_id = uuid4().hex
    await image_assets.insert_one({"id": empty_id, "workspace_id": ws_id, "version_count": 1, "slides": [{"slide_number": 1, "title": "", "slide_type": "x"}]})
    no_picture = await _attach(client, ws_id, piece_id, asset_type="image", asset_id=empty_id)
    assert no_picture.status_code == 422 and "no picture" in no_picture.json()["detail"]

    bad_slide = await _attach(client, ws_id, piece_id, asset_type="image", asset_id=(await _image(ws_id))[0], slide_number=9)
    assert bad_slide.status_code == 404
    assert (await _attach(client, ws_id, piece_id, asset_type="video")).status_code == 422
    assert (await _attach(client, ws_id, piece_id, asset_type="upload", media_id="nope")).status_code == 404
    assert await _get(client, ws_id, piece_id) == []


async def test_the_first_attachment_is_primary_and_detaching_moves_it_on(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Attach WS 4")
    piece_id = await _piece(ws_id, profile["id"])
    image_id, (m1, m2) = await _image(ws_id, slides=2)

    a1 = (await _attach(client, ws_id, piece_id, asset_type="image", asset_id=image_id, slide_number=1)).json()["attachment"]
    a2 = (await _attach(client, ws_id, piece_id, asset_type="image", asset_id=image_id, slide_number=2)).json()["attachment"]
    assert a1["id"] != a2["id"]
    assert [m["id"] for m in (await content_pieces.find_one({"piece_id": piece_id}))["media"]] == [m1]

    res = await client.delete(f"/api/v1/content/pieces/{piece_id}/attachments/{a1['id']}", headers=H(ws_id))
    assert res.status_code == 200, res.text
    assert [m["id"] for m in (await content_pieces.find_one({"piece_id": piece_id}))["media"]] == [m2]
    # the other slide still links this post to the image
    assert len((await image_assets.find_one({"id": image_id}))["linked_pieces"]) == 1

    res = await client.delete(f"/api/v1/content/pieces/{piece_id}/attachments/{a2['id']}", headers=H(ws_id))
    assert res.status_code == 200
    doc = await content_pieces.find_one({"piece_id": piece_id})
    assert doc["media"] == [] and doc["attachments"] == []
    assert (await image_assets.find_one({"id": image_id})).get("linked_pieces") == []

    gone = await client.delete(f"/api/v1/content/pieces/{piece_id}/attachments/{a2['id']}", headers=H(ws_id))
    assert gone.status_code == 404


async def test_an_asset_that_moved_on_is_stale_until_refreshed(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Attach WS 5")
    piece_id = await _piece(ws_id, profile["id"])
    image_id, (old_media,) = await _image(ws_id)
    await _attach(client, ws_id, piece_id, asset_type="image", asset_id=image_id)

    # the design is edited: a new picture and a new version
    new_media = await _media(ws_id)
    await image_assets.update_one(
        {"id": image_id}, {"$set": {"version_count": 2, "slides.0.media_id": new_media}},
    )
    stale = (await _get(client, ws_id, piece_id))[0]
    assert stale["stale"] is True and stale["stale_reason"] == "asset_changed"
    # the post still holds the old picture until refreshed: the change is visible, not silent
    assert (await content_pieces.find_one({"piece_id": piece_id}))["media"][0]["id"] == old_media

    res = await client.post(f"/api/v1/content/pieces/{piece_id}/attachments/refresh", headers=H(ws_id))
    assert res.status_code == 200, res.text
    fresh = res.json()["attachments"][0]
    assert fresh["stale"] is False and fresh["asset_version"] == 2 and fresh["media_id"] == new_media
    assert (await content_pieces.find_one({"piece_id": piece_id}))["media"][0]["id"] == new_media


async def test_text_edited_after_a_picture_was_made_from_it_is_flagged(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Attach WS 6")
    piece_id = await _piece(ws_id, profile["id"])
    image_id, _ = await _image(ws_id, source_piece_id=piece_id)
    unrelated, _ = await _image(ws_id)
    await _attach(client, ws_id, piece_id, asset_type="image", asset_id=image_id)
    await _attach(client, ws_id, piece_id, asset_type="image", asset_id=unrelated)
    assert not any(a["stale"] for a in await _get(client, ws_id, piece_id))

    edit = await client.patch(f"/api/v1/content/pieces/{piece_id}", json={"content": "Edited words."}, headers=H(ws_id))
    assert edit.status_code == 200, edit.text
    by_asset = {a["asset_id"]: a for a in await _get(client, ws_id, piece_id)}
    assert by_asset[image_id]["stale_reason"] == "text_changed" and by_asset[image_id]["stale"] is True
    assert by_asset[unrelated]["stale"] is False  # not made from this text


async def test_attach_detach_and_refresh_are_refused_once_published(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Attach WS 7")
    piece_id = await _piece(ws_id, profile["id"])
    image_id, _ = await _image(ws_id)
    other_image, _ = await _image(ws_id)
    att = (await _attach(client, ws_id, piece_id, asset_type="image", asset_id=image_id)).json()["attachment"]

    for status in ("published", "publishing"):
        await content_pieces.update_one({"piece_id": piece_id}, {"$set": {"publish_status": status}})
        assert (await _attach(client, ws_id, piece_id, asset_type="image", asset_id=other_image)).status_code == 409
        refresh = await client.post(f"/api/v1/content/pieces/{piece_id}/attachments/refresh", headers=H(ws_id))
        assert refresh.status_code == 409 and "published" in refresh.json()["detail"]
        assert (await client.delete(f"/api/v1/content/pieces/{piece_id}/attachments/{att['id']}", headers=H(ws_id))).status_code == 409
    # asking again for what is already attached is still just answered
    assert (await _attach(client, ws_id, piece_id, asset_type="image", asset_id=image_id)).json()["created"] is False


async def test_recordings_use_the_approved_master_and_warn_about_audio(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Attach WS 8")
    piece_id = await _piece(ws_id, profile["id"])
    audio = await _audio(ws_id)
    master = await _media(ws_id, "audio")
    await audio_assets.update_one({"id": audio["id"]}, {"$set": {"approved_master_media_id": master}})

    res = await _attach(client, ws_id, piece_id, asset_type="audio", asset_id=audio["id"])
    assert res.status_code == 200, res.text
    assert res.json()["attachment"]["media_id"] == master
    assert res.json()["warning"] == "Audio can't be posted on its own. Render it as a video and attach that."
    assert [lp["piece_id"] for lp in (await audio_assets.find_one({"id": audio["id"]}))["linked_pieces"]] == [piece_id]


async def test_a_video_clip_attaches_and_goes_stale_when_the_clip_is_gone(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Attach WS 9")
    piece_id = await _piece(ws_id, profile["id"])
    audio = await _audio(ws_id, with_clip=True)

    res = await _attach(client, ws_id, piece_id, asset_type="video", clip_id=audio["clip_id"])
    assert res.status_code == 200, res.text
    att = res.json()["attachment"]
    assert (att["asset_type"], att["asset_id"], att["media_id"]) == ("video", audio["id"], audio["clip_media"])
    assert (await content_pieces.find_one({"piece_id": piece_id}))["media"][0]["kind"] == "video"
    assert not (await _get(client, ws_id, piece_id))[0]["stale"]

    await audio_assets.update_one({"id": audio["id"]}, {"$set": {"video_clips": []}})
    assert (await _get(client, ws_id, piece_id))[0]["stale_reason"] == "asset_changed"


async def test_patch_media_keeps_working_and_records_the_attachment(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Attach WS 10")
    piece_id = await _piece(ws_id, profile["id"])
    upload = await _media(ws_id)
    image_id, (slide_media,) = await _image(ws_id)

    res = await client.patch(f"/api/v1/content/pieces/{piece_id}/media", json={"media_id": upload}, headers=H(ws_id))
    assert res.status_code == 200, res.text
    assert [m["id"] for m in res.json()["media"]] == [upload]  # same response as before
    atts = await _get(client, ws_id, piece_id)
    assert [(a["asset_type"], a["media_id"]) for a in atts] == [("upload", upload)]

    # a picture that belongs to an image is recorded as that image, with its backlink
    res = await client.patch(f"/api/v1/content/pieces/{piece_id}/media", json={"media_id": slide_media}, headers=H(ws_id))
    assert res.status_code == 200
    atts = await _get(client, ws_id, piece_id)
    assert [(a["asset_type"], a["asset_id"]) for a in atts] == [("image", image_id)]
    assert (await image_assets.find_one({"id": image_id}))["linked_pieces"][0]["piece_id"] == piece_id

    res = await client.patch(f"/api/v1/content/pieces/{piece_id}/media", json={"media_id": None}, headers=H(ws_id))
    assert res.status_code == 200 and res.json()["media"] == []
    assert await _get(client, ws_id, piece_id) == []
    assert (await image_assets.find_one({"id": image_id})).get("linked_pieces") == []

    missing = await client.patch(f"/api/v1/content/pieces/{piece_id}/media", json={"media_id": "nope"}, headers=H(ws_id))
    assert missing.status_code == 404


async def test_an_attached_image_is_what_publish_now_sends(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Attach WS 11")
    await save_token(
        workspace_id=ws_id, platform="linkedin", access_token="t", refresh_token=None, expires_at=None,
        platform_user_id="acct", username="u", connected_by="",
    )
    piece_id = await _piece(ws_id, profile["id"])
    image_id, (media_id,) = await _image(ws_id)
    await _attach(client, ws_id, piece_id, asset_type="image", asset_id=image_id)
    assert (await client.patch(f"/api/v1/content/pieces/{piece_id}/approve", headers=H(ws_id))).status_code == 200

    fake = AsyncMock()
    fake.publish = AsyncMock(return_value=PublishResult(success=True, platform="linkedin", piece_id=piece_id, platform_post_id="p"))
    with patch("app.pipelines.publish.executor.get_publisher", return_value=fake):
        res = await client.post("/api/v1/publish/now", json={"piece_id": piece_id}, headers=H(ws_id))
    assert res.status_code == 200, res.text
    sent = fake.publish.call_args.args[0]
    assert [m.id for m in sent.media] == [media_id]


# ── send a video to a draft in one step ──────────────────────────────────────

async def test_send_to_draft_makes_the_post_and_attaches_the_video(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Send WS 1")
    audio = await _audio(ws_id, with_clip=True)

    res = await client.post(
        f"/api/v1/audio-assets/{audio['id']}/send-to-draft", json={"clip_id": audio["clip_id"]}, headers=H(ws_id),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["created"] is True
    assert body["platform"] == "LinkedIn"            # the clip's chosen place to post
    assert body["content"] == "Best bit"             # defaults to the clip title
    piece = await content_pieces.find_one({"piece_id": body["piece_id"]})
    assert [m["id"] for m in piece["media"]] == [audio["clip_media"]]
    assert piece["attachments"][0]["asset_type"] == "video" and piece["attachments"][0]["clip_id"] == audio["clip_id"]
    assert piece["approval_status"] == "pending"


async def test_send_to_draft_uses_the_given_caption_and_platform(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Send WS 2")
    audio = await _audio(ws_id, with_clip=True)
    res = await client.post(
        f"/api/v1/audio-assets/{audio['id']}/send-to-draft",
        json={"clip_id": audio["clip_id"], "caption": "  My caption  ", "platform": "YouTube"}, headers=H(ws_id),
    )
    assert res.status_code == 200, res.text
    assert res.json()["content"] == "My caption" and res.json()["platform"] == "YouTube"

    bad = await client.post(
        f"/api/v1/audio-assets/{audio['id']}/send-to-draft",
        json={"clip_id": audio["clip_id"], "platform": "Nowhere"}, headers=H(ws_id),
    )
    assert bad.status_code == 400
    missing = await client.post(
        f"/api/v1/audio-assets/{audio['id']}/send-to-draft", json={"clip_id": "nope"}, headers=H(ws_id),
    )
    assert missing.status_code == 404


async def test_send_to_draft_is_idempotent_even_for_clicks_at_the_same_moment(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Send WS 3")
    audio = await _audio(ws_id, with_clip=True)
    url = f"/api/v1/audio-assets/{audio['id']}/send-to-draft"
    body = {"clip_id": audio["clip_id"]}

    results = await asyncio.gather(*(client.post(url, json=body, headers=H(ws_id)) for _ in range(3)))
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    ids = {r.json()["piece_id"] for r in results}
    assert len(ids) == 1
    assert sum(1 for r in results if r.json()["created"]) == 1

    again = await client.post(url, json=body, headers=H(ws_id))
    assert again.json()["piece_id"] in ids and again.json()["created"] is False
    count = await content_pieces.count_documents({"workspace_id": ws_id, "send_origin.clip_id": audio["clip_id"]})
    assert count == 1
    piece = await content_pieces.find_one({"piece_id": next(iter(ids))})
    assert len(piece["attachments"]) == 1


async def test_send_to_draft_after_the_draft_was_deleted_makes_a_new_one(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Send WS 4")
    audio = await _audio(ws_id, with_clip=True)
    url = f"/api/v1/audio-assets/{audio['id']}/send-to-draft"
    first = (await client.post(url, json={"clip_id": audio["clip_id"]}, headers=H(ws_id))).json()
    await client.delete(f"/api/v1/content/pieces/{first['piece_id']}", headers=H(ws_id))
    second = await client.post(url, json={"clip_id": audio["clip_id"]}, headers=H(ws_id))
    assert second.status_code == 200, second.text
    assert second.json()["piece_id"] != first["piece_id"] and second.json()["created"] is True
