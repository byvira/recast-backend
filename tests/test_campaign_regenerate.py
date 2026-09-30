"""Make a post's picture again from the campaign. The image generator is replaced by a fake that stores a
picture or a text card, so no provider is called. A new picture replaces the old one; a text card never
replaces a real picture; a picture that is only a text card says so."""
from types import SimpleNamespace
from uuid import uuid4

from tests.conftest import signup_new_user
from tests.test_campaigns import _create_brand, _post_with_failed_media, _valid_body


def _fake_create(card: bool = False, fail: bool = False):
    async def create(body, ctx):
        from app.db.mongo import image_assets, media_assets

        if fail:
            raise RuntimeError("no image service")
        media_id, asset_id = "m-" + uuid4().hex[:8], "i-" + uuid4().hex[:8]
        await media_assets.insert_one({
            "id": media_id, "workspace_id": ctx.workspace_id, "url": f"https://f/{media_id}.png", "mime_type": "image/png",
            "qa_flagged": card, "qa_flag_reason": "No AI picture, so this is a text card. The image service did not return a picture." if card else None,
        })
        await image_assets.insert_one({
            "id": asset_id, "workspace_id": ctx.workspace_id, "source_piece_id": body.source_piece_id, "title": body.title,
            "slides": [{"slide_number": 1, "media_id": media_id}],
        })
        return SimpleNamespace(id=asset_id, slides=[SimpleNamespace(media_id=media_id)])

    return create


async def _seed_old(campaign, *, card: bool):
    from app.db.mongo import image_assets, media_assets

    ws, pid = campaign["workspace_id"], campaign["_pid"]
    media_id, asset_id = "m-old-" + uuid4().hex[:6], "i-old-" + uuid4().hex[:6]
    await media_assets.insert_one({"id": media_id, "workspace_id": ws, "url": f"https://f/{media_id}.png", "mime_type": "image/png", "qa_flagged": card})
    await image_assets.insert_one({"id": asset_id, "workspace_id": ws, "source_piece_id": pid, "title": "old", "slides": [{"slide_number": 1, "media_id": media_id}]})
    return asset_id, f"https://f/{media_id}.png"


async def _urls(api_client, campaign):
    res = await api_client.get(f"/api/v1/campaigns/{campaign['id']}/media")
    return [m["url"] for m in res.json()["items"].get(campaign["_pid"], []) if m["kind"] == "image"], res.json()


def _regen(api_client, campaign):
    return api_client.post(f"/api/v1/campaigns/{campaign['id']}/pieces/{campaign['_pid']}/regenerate-media")


async def test_a_new_picture_replaces_the_old_one(api_client, monkeypatch):
    from app.api.v1 import image_assets as image_module
    from app.db.mongo import image_assets

    monkeypatch.setattr(image_module, "create_image_asset", _fake_create(card=False))
    campaign = await _post_with_failed_media(api_client, {"image": "ready"})
    old_id, old_url = await _seed_old(campaign, card=False)
    res = await _regen(api_client, campaign)
    assert res.status_code == 200, res.text
    assert res.json()["state"] == "ready"
    urls, body = await _urls(api_client, campaign)
    assert len(urls) == 1 and urls[0] != old_url
    assert body["status"][campaign["_pid"]]["image"] == "ready"
    assert (await image_assets.find_one({"id": old_id}))["replaced_by"]


async def test_a_text_card_never_replaces_a_real_picture(api_client, monkeypatch):
    from app.api.v1 import image_assets as image_module

    monkeypatch.setattr(image_module, "create_image_asset", _fake_create(card=True))
    campaign = await _post_with_failed_media(api_client, {"image": "ready"})
    _, old_url = await _seed_old(campaign, card=False)
    res = await _regen(api_client, campaign)
    assert res.json()["state"] == "kept"
    assert "text card" in (res.json()["note"] or "")
    urls, _ = await _urls(api_client, campaign)
    assert urls == [old_url]


async def test_a_real_picture_replaces_a_text_card(api_client, monkeypatch):
    from app.api.v1 import image_assets as image_module

    monkeypatch.setattr(image_module, "create_image_asset", _fake_create(card=False))
    campaign = await _post_with_failed_media(api_client, {"image": "ready"})
    _, old_url = await _seed_old(campaign, card=True)
    res = await _regen(api_client, campaign)
    assert res.json()["state"] == "ready"
    urls, _ = await _urls(api_client, campaign)
    assert len(urls) == 1 and urls[0] != old_url


async def test_a_card_is_reported_as_flagged_with_the_reason(api_client, monkeypatch):
    from app.api.v1 import image_assets as image_module

    monkeypatch.setattr(image_module, "create_image_asset", _fake_create(card=True))
    campaign = await _post_with_failed_media(api_client, {"image": "failed"})
    res = await _regen(api_client, campaign)
    assert res.json()["state"] == "card"
    _, body = await _urls(api_client, campaign)
    item = body["items"][campaign["_pid"]][0]
    assert item["flagged"] is True and "did not return a picture" in item["flag_reason"]


async def test_a_failed_regenerate_keeps_the_old_picture(api_client, monkeypatch):
    from app.api.v1 import image_assets as image_module

    monkeypatch.setattr(image_module, "create_image_asset", _fake_create(fail=True))
    campaign = await _post_with_failed_media(api_client, {"image": "ready"})
    _, old_url = await _seed_old(campaign, card=False)
    res = await _regen(api_client, campaign)
    assert res.status_code == 200 and res.json()["state"] == "failed"
    urls, body = await _urls(api_client, campaign)
    assert urls == [old_url] and body["status"][campaign["_pid"]]["image"] == "ready"


async def test_a_failed_first_picture_is_marked_failed(api_client, monkeypatch):
    from app.api.v1 import image_assets as image_module

    monkeypatch.setattr(image_module, "create_image_asset", _fake_create(fail=True))
    campaign = await _post_with_failed_media(api_client, {"audio": "ready"})
    res = await _regen(api_client, campaign)
    assert res.json()["state"] == "failed"
    _, body = await _urls(api_client, campaign)
    assert body["status"][campaign["_pid"]]["image"] == "failed"


async def test_unknown_campaign_post_and_a_campaign_without_pictures_are_404(api_client, monkeypatch):
    from app.api.v1 import image_assets as image_module

    async def boom(*a, **k):
        raise AssertionError("must not generate")

    monkeypatch.setattr(image_module, "create_image_asset", boom)
    await signup_new_user(api_client)
    assert (await api_client.post("/api/v1/campaigns/nope/pieces/x/regenerate-media")).status_code == 404
    brand_id = await _create_brand(api_client)
    text_only = (await api_client.post("/api/v1/campaigns/", json=_valid_body(brand_id))).json()
    assert (await api_client.post(f"/api/v1/campaigns/{text_only['id']}/pieces/x/regenerate-media")).status_code == 404
    campaign = await _post_with_failed_media(api_client, {"image": "ready"})
    assert (await api_client.post(f"/api/v1/campaigns/{campaign['id']}/pieces/not-a-post/regenerate-media")).status_code == 404
