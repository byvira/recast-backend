"""Pictures and recordings a campaign makes are tagged with the campaign, are not made twice for the same post and words,
and a failure to make one never stops the post."""
from types import SimpleNamespace
from uuid import uuid4

from app.api.v1 import audio_assets as audio_module
from app.api.v1 import image_assets as image_module
from app.db.mongo import audio_assets, image_assets
from app.pipelines.campaigns import media as campaign_media


def _setup(monkeypatch):
    ws_id = f"ws-{uuid4()}"
    ctx = SimpleNamespace(workspace_id=ws_id, user_id="u1")
    campaign = {"id": f"c-{uuid4()}", "brand_id": "b1", "media_plan": {"enabled": True, "kinds": ["image", "audio"], "count_per_post": 1}}
    piece = {"piece_id": f"p-{uuid4()}", "platform": "LinkedIn", "content": "A post about clear writing."}
    calls = {"image": 0, "audio": 0, "attached": []}

    async def _request(c, p):
        return None

    async def _create_image(request, context):
        calls["image"] += 1
        asset_id = f"img-{uuid4()}"
        await image_assets.insert_one({"id": asset_id, "workspace_id": ws_id})
        return SimpleNamespace(id=asset_id)

    async def _attach(piece_id, workspace_id, user_id, image_id):
        calls["attached"].append((piece_id, image_id))

    async def _create_audio(request, context, run):
        calls["audio"] += 1
        asset_id = f"aud-{uuid4()}"
        await audio_assets.insert_one({
            "id": asset_id, "workspace_id": ws_id, "source_piece_id": piece["piece_id"], "script": request.script,
        })
        return SimpleNamespace(id=asset_id)

    monkeypatch.setattr(campaign_media, "_image_request", _request)
    monkeypatch.setattr(campaign_media, "attach_generated_image", _attach)
    monkeypatch.setattr(image_module, "create_image_asset", _create_image)
    monkeypatch.setattr(audio_module, "create_audio_from_script", _create_audio)
    return ws_id, ctx, campaign, piece, calls


async def test_a_picture_made_for_a_campaign_carries_the_campaign_and_goes_onto_its_post(monkeypatch):
    ws_id, ctx, campaign, piece, calls = _setup(monkeypatch)

    states = await campaign_media._make_for_piece(campaign, piece, ["image"], ctx)

    assert states == {"image": "ready"}
    [stored] = await image_assets.find({"workspace_id": ws_id}).to_list(length=5)
    assert stored["campaign_id"] == campaign["id"]
    assert calls["attached"] == [(piece["piece_id"], stored["id"])]


async def test_a_recording_made_for_a_campaign_carries_the_campaign(monkeypatch):
    ws_id, ctx, campaign, piece, calls = _setup(monkeypatch)

    states = await campaign_media._make_for_piece(campaign, piece, ["audio"], ctx)

    assert states == {"audio": "ready"}
    [stored] = await audio_assets.find({"workspace_id": ws_id}).to_list(length=5)
    assert stored["campaign_id"] == campaign["id"]


async def test_running_the_campaign_again_reuses_the_recording_for_the_same_post_and_words(monkeypatch):
    ws_id, ctx, campaign, piece, calls = _setup(monkeypatch)

    await campaign_media._make_for_piece(campaign, piece, ["audio"], ctx)
    again = await campaign_media._make_for_piece(campaign, piece, ["audio"], ctx)

    assert again == {"audio": "ready"}
    assert calls["audio"] == 1                                     # voiced once, not twice
    assert await audio_assets.count_documents({"workspace_id": ws_id}) == 1


async def test_changed_words_get_a_new_recording(monkeypatch):
    ws_id, ctx, campaign, piece, calls = _setup(monkeypatch)
    await campaign_media._make_for_piece(campaign, piece, ["audio"], ctx)

    await campaign_media._make_for_piece(campaign, {**piece, "content": "Completely different words now."}, ["audio"], ctx)

    assert calls["audio"] == 2


async def test_a_media_failure_is_recorded_as_failed_and_never_raised(monkeypatch):
    ws_id, ctx, campaign, piece, calls = _setup(monkeypatch)

    async def _boom(request, context):
        raise RuntimeError("the picture service is down")

    monkeypatch.setattr(image_module, "create_image_asset", _boom)

    states = await campaign_media._make_for_piece(campaign, piece, ["image", "audio"], ctx)

    assert states["image"] == "failed"
    assert states["audio"] == "ready"                              # one kind failing does not stop the other
    assert calls["attached"] == []
