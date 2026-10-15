"""Campaign video: each post is voiced over its own picture, built into an editable project, rendered and attached. The posts of a
run are worked on a few at a time and one failure never stops the others. Only the calls that spend provider quota or need the
render worker are replaced."""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.agents.chain import topic_to_video
from app.api.v1 import audio_assets as audio_module
from app.api.v1 import image_assets as image_module
from app.db.mongo import audio_assets, content_pieces, image_assets
from app.pipelines.campaigns import media as campaign_media
from app.pipelines.publish import attachments
from app.video import service


def test_video_is_a_kind_and_is_counted_before_a_run_starts():
    assert "video" in campaign_media.GENERATED_KINDS
    plan = {"enabled": True, "kinds": ["image", "audio", "video"], "count_per_post": 2}
    assert campaign_media.wanted_kinds(plan) == ["image", "audio", "video"]
    assert campaign_media.estimate(plan, 4) == {"images": 8, "audio": 4, "videos": 4}
    assert campaign_media.estimate({"enabled": True, "kinds": ["image"]}, 4)["videos"] == 0


@pytest.fixture
def world(monkeypatch):
    ws_id = f"ws-{uuid4()}"
    ctx = SimpleNamespace(workspace_id=ws_id, user_id="u1")
    calls = {"image": 0, "audio": 0, "built": [], "rendered": [], "attached": [], "active": 0, "peak": 0}

    async def request(c, p):
        return None

    async def create_image(req, context):
        calls["image"] += 1
        asset_id = f"img-{uuid4()}"
        await image_assets.insert_one({"id": asset_id, "workspace_id": ws_id})
        return SimpleNamespace(id=asset_id, slides=[SimpleNamespace(media_id=f"pic-{asset_id}")])

    async def create_audio(req, context, run):
        calls["audio"] += 1
        asset_id = f"aud-{uuid4()}"
        await audio_assets.insert_one({"id": asset_id, "workspace_id": ws_id, "source_piece_id": req.source_piece_id, "script": req.script})
        return SimpleNamespace(id=asset_id, media_id=f"voice-{asset_id}")

    async def attach_image(piece_id, workspace_id, user_id, image_id):
        calls["attached"].append(("image", piece_id))

    async def build(ctx_, **kw):
        calls["built"].append(kw)
        return f"proj-{uuid4()}", 8.0

    async def render(**kw):
        calls["active"] += 1
        calls["peak"] = max(calls["peak"], calls["active"])
        await asyncio.sleep(0.6)
        calls["active"] -= 1
        calls["rendered"].append(kw)
        return {"media_id": "m"}

    async def resolve(workspace_id, asset_type, asset_id=None, **kw):
        return {"asset_type": asset_type, "asset_id": asset_id}

    async def attach(piece, workspace_id, user_id, resolved):
        calls["attached"].append((resolved["asset_type"], piece["piece_id"]))

    async def context(workspace_id, user_id):
        return ctx

    monkeypatch.setattr(campaign_media, "_image_request", request)
    monkeypatch.setattr(campaign_media, "attach_generated_image", attach_image)
    monkeypatch.setattr(campaign_media, "_context", context)
    monkeypatch.setattr(image_module, "create_image_asset", create_image)
    monkeypatch.setattr(audio_module, "create_audio_from_script", create_audio)
    monkeypatch.setattr(topic_to_video, "build_narrated_project", build)
    monkeypatch.setattr(service, "render_project_and_wait", render)
    monkeypatch.setattr(attachments, "resolve_asset", resolve)
    monkeypatch.setattr(attachments, "attach", attach)
    return ws_id, ctx, calls


def _campaign(kinds, **extra):
    return {"id": f"c-{uuid4()}", "brand_id": "b1", "media_plan": {"enabled": True, "kinds": kinds, **extra}}


async def _stored_piece(ws_id, content="A post about clear writing."):
    piece = {"piece_id": f"p-{uuid4()}", "workspace_id": ws_id, "platform": "LinkedIn", "content": content, "deleted": False}
    await content_pieces.insert_one(dict(piece))
    return piece


async def test_a_post_is_voiced_built_rendered_and_attached(world):
    ws_id, ctx, calls = world
    piece = await _stored_piece(ws_id)
    campaign = _campaign(["image", "video"], video={"aspect": "9:16"})

    states = await campaign_media._make_for_piece(campaign, piece, ["image", "video"], ctx)

    assert states == {"image": "ready", "video": "ready"}
    assert calls["image"] == 1 and calls["audio"] == 1 and len(calls["rendered"]) == 1
    [built] = calls["built"]
    assert built["size"] == "vertical" and built["voice_media_id"].startswith("voice-") and built["cover_media_id"].startswith("pic-")
    assert built["created_from"]["kind"] == "campaign" and built["created_from"]["piece_id"] == piece["piece_id"]
    assert ("image", piece["piece_id"]) in calls["attached"] and ("project", piece["piece_id"]) in calls["attached"]


async def test_a_video_only_plan_builds_over_the_picture_but_attaches_only_the_video(world):
    ws_id, ctx, calls = world
    piece = await _stored_piece(ws_id)
    campaign = _campaign(["video"], video={"aspect": "1:1"})

    states = await campaign_media._make_for_piece(campaign, piece, ["video"], ctx)

    assert states == {"video": "ready"}
    assert calls["built"][0]["size"] == "square" and calls["built"][0]["cover_media_id"]
    assert [a for a in calls["attached"] if a[1] == piece["piece_id"]] == [("project", piece["piece_id"])]


async def test_a_failed_video_is_marked_failed_and_keeps_the_picture_and_recording(world, monkeypatch):
    ws_id, ctx, calls = world

    async def broken(**kw):
        raise service.RenderFailed("The render host is busy.")

    monkeypatch.setattr(service, "render_project_and_wait", broken)
    piece = await _stored_piece(ws_id)

    states = await campaign_media._make_for_piece(_campaign(["image", "audio", "video"]), piece, ["image", "audio", "video"], ctx)

    assert states == {"image": "ready", "audio": "ready", "video": "failed"}


async def test_posts_are_worked_on_together_up_to_the_limit_and_one_failure_does_not_stop_the_rest(world, monkeypatch):
    ws_id, ctx, calls = world
    pieces = [await _stored_piece(ws_id, f"Post number {i} about clear writing.") for i in range(6)]
    for p in pieces:
        p["campaign_id"] = "ignored"
    real = campaign_media._make_for_piece
    seen = {"n": 0}

    async def sometimes_broken(campaign, piece, kinds, ctx_):
        seen["n"] += 1
        if seen["n"] == 2:
            raise RuntimeError("one post broke")
        return await real(campaign, piece, kinds, ctx_)

    monkeypatch.setattr(campaign_media, "_make_for_piece", sometimes_broken)

    out = await campaign_media.generate_media_for_pieces(
        _campaign(["video"]), [p["piece_id"] for p in pieces], workspace_id=ws_id, user_id="u1",
    )

    assert out["videos"] == 5 and out["failed"] == 1
    assert 1 < calls["peak"] <= campaign_media.POSTS_AT_ONCE


async def test_a_cancel_ends_the_run_instead_of_being_counted_as_a_failed_post(world, monkeypatch):
    from app.shared.pipeline_runs import RunCancelled

    ws_id, ctx, calls = world
    piece = await _stored_piece(ws_id)

    async def cancelled(campaign, piece_, kinds, ctx_):
        raise RunCancelled()

    monkeypatch.setattr(campaign_media, "_make_for_piece", cancelled)

    with pytest.raises(RunCancelled):
        await campaign_media.generate_media_for_pieces(_campaign(["video"]), [piece["piece_id"]], workspace_id=ws_id, user_id="u1")


async def test_the_post_list_carries_a_small_picture_for_each_attachment(world):
    from app.db.mongo import media_assets
    from app.pipelines.text.storage import get_workspace_pieces

    ws_id, ctx, calls = world
    await media_assets.insert_one({"id": "mv", "workspace_id": ws_id, "kind": "video", "url": "https://cdn.example/v.mp4",
                                   "poster_url": "https://cdn.example/v.jpg"})
    await media_assets.insert_one({"id": "mi", "workspace_id": ws_id, "kind": "image", "url": "https://cdn.example/i.png"})
    piece = await _stored_piece(ws_id)
    await content_pieces.update_one({"piece_id": piece["piece_id"]}, {"$set": {"attachments": [
        {"asset_type": "project", "asset_id": "p1", "media_id": "mv"}, {"asset_type": "image", "asset_id": "i1", "media_id": "mi"}]}})

    listing = await get_workspace_pieces(ws_id)
    [item] = [i for i in listing["items"] if i["piece_id"] == piece["piece_id"]]

    assert item["attachment_count"] == 2
    assert [m["thumbnail_url"] for m in item["media_preview"]] == ["https://cdn.example/v.jpg", "https://cdn.example/i.png"]
    assert item["media_preview"][0]["kind"] == "video"
