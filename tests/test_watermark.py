"""A brand's watermark: the layer that always draws the brand's current mark, stamping it on exports, and the save and suggestion
routes. Runs with the LLM mocked; no picture provider is ever called."""

import io
from datetime import datetime, timezone
from uuid import uuid4

from PIL import Image

from app.api.v1 import image_assets as image_routes
from app.db.mongo import brand_profiles, media_assets
from app.models.image_asset import ImageAsset, Layer
from app.pipelines.brand.watermark_tools import mark_prompt, usable_suggestions
from app.pipelines.media import watermark as W
from app.pipelines.media.image_render import BrandTokens
from tests.conftest import create_workspace

MARK = {"text": "@ekavo", "text_color": "#FFFFFF", "text_bold": True, "image_url": "", "auto_apply": True, "auto_kind": "text",
        "position": "bottom_right", "size": 0.3, "opacity": 1.0}


def _png(size=(400, 300), colour=(0, 0, 0, 255), mode="RGB") -> bytes:
    out = io.BytesIO()
    Image.new(mode, size, colour if mode == "RGBA" else colour[:3]).save(out, format="PNG")
    return out.getvalue()


def _changed(before: bytes, after: bytes, box: tuple[int, int, int, int]) -> bool:
    a = Image.open(io.BytesIO(before)).convert("RGBA").crop(box)
    b = Image.open(io.BytesIO(after)).convert("RGBA").crop(box)
    return a.tobytes() != b.tobytes()


# ── the layer draws the brand's current mark ─────────────────────────────

def test_a_watermark_layer_becomes_the_brands_words_or_picture():
    layers = [
        Layer(id="a", type="watermark", watermark_kind="text", x=0.1, y=0.8, opacity=0.5),
        Layer(id="b", type="watermark", watermark_kind="image"),
        Layer(id="c", type="text", text="Headline"),
    ]
    resolved = W.resolve_layers(layers, {**MARK, "image_url": "https://files/mark.png", "text_font": "Inter"})
    assert [(l.id, l.type) for l in resolved] == [("a", "text"), ("b", "image"), ("c", "text")]
    assert resolved[0].text == "@ekavo" and resolved[0].opacity == 0.5 and resolved[0].x == 0.1 and resolved[0].font == "Inter"


def test_a_mark_the_brand_has_not_set_draws_nothing():
    layers = [Layer(id="a", type="watermark", watermark_kind="image"), Layer(id="b", type="watermark", watermark_kind="text")]
    assert W.resolve_layers(layers, {"text": "  ", "image_url": ""}) == []
    assert W.resolve_layers(layers, None) == []


def test_the_watermark_layer_asks_for_its_picture_only_when_it_is_a_picture():
    from app.pipelines.media.image_layers import layer_assets_needed

    layers = [Layer(id="a", type="watermark", watermark_kind="image"), Layer(id="b", type="watermark", watermark_kind="text")]
    assert [l.id for l in layer_assets_needed(layers)] == ["a"]


# ── stamping a finished picture ──────────────────────────────────────────

def test_a_text_mark_lands_in_its_corner_and_nowhere_else():
    picture = _png()
    stamped = W.stamp(picture, MARK, BrandTokens())
    assert _changed(picture, stamped, (200, 200, 400, 300))          # bottom right
    assert not _changed(picture, stamped, (0, 0, 200, 150))          # top left untouched


def test_each_corner_is_honoured():
    picture = _png()
    stamped = W.stamp(picture, {**MARK, "position": "top_left"}, BrandTokens())
    assert _changed(picture, stamped, (0, 0, 200, 100)) and not _changed(picture, stamped, (200, 200, 400, 300))


def test_a_picture_mark_is_drawn_from_its_file_and_keeps_see_through_corners():
    picture = _png(mode="RGBA", colour=(0, 0, 0, 0))
    mark = _png(size=(100, 100), colour=(255, 0, 0, 255))
    stamped = W.stamp(picture, {**MARK, "auto_kind": "image", "image_url": "x"}, BrandTokens(), mark)
    out = Image.open(io.BytesIO(stamped))
    assert out.mode == "RGBA" and out.getpixel((5, 5))[3] == 0      # the rest stays see-through
    assert _changed(picture, stamped, (250, 150, 400, 300))


def test_no_mark_set_leaves_the_picture_as_it_was():
    picture = _png()
    assert W.stamp(picture, {**MARK, "text": ""}, BrandTokens()) == picture
    assert W.stamp(picture, {**MARK, "auto_kind": "image", "image_url": "x"}, BrandTokens(), None) == picture


# ── suggestions and the AI mark ──────────────────────────────────────────

def test_suggestions_are_short_clean_and_different():
    result = {"suggestions": ["@ekavo", "© Ekavo", "@EKAVO", "Made with care by the Ekavo team every day", "#ekavo", "ekavo.com", "Ekavo — studio", 7]}
    assert usable_suggestions(result) == ["@ekavo", "© Ekavo", "ekavo", "ekavo.com", "Ekavo studio"]


def test_the_mark_is_asked_for_as_a_symbol_without_words():
    prompt = mark_prompt({"brand_type": "Shop", "identity": {"name": "Leaf"}}, "a  leaf")
    assert "Leaf" in prompt and "The idea: a leaf." in prompt and "No letters, words" in prompt


# ── the routes ───────────────────────────────────────────────────────────

async def _brand(client, ws_id: str) -> str:
    res = await client.post("/api/v1/brand/", json={"brand_type": "Person"}, headers={"X-Workspace-Id": ws_id})
    assert res.status_code in (200, 201), res.text
    return res.json()["brand_profile_id"]


async def test_saving_takes_the_pictures_address_from_the_library_and_keeps_the_version(signup_user):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Watermark WS")
    brand_id = await _brand(client, ws_id)
    before = await brand_profiles.find_one({"id": brand_id})
    media_id = str(uuid4())
    await media_assets.insert_one({"id": media_id, "workspace_id": ws_id, "kind": "image", "url": "https://files/mark.png"})

    res = await client.patch(f"/api/v1/brand/{brand_id}/watermark", headers={"X-Workspace-Id": ws_id},
                             json={"text": "  @me   here ", "image_media_id": media_id, "image_url": "https://evil/x.png"})
    assert res.status_code == 200, res.text
    mark = res.json()["watermark"]
    assert mark["image_url"] == "https://files/mark.png" and mark["text"] == "@me here"
    after = await brand_profiles.find_one({"id": brand_id})
    assert after["updated_at"] == before["updated_at"]               # an open Brand Assets form can still save


async def test_the_logo_can_be_the_picture_mark_but_no_other_address(signup_user):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Watermark WS")
    brand_id = await _brand(client, ws_id)
    await brand_profiles.update_one({"id": brand_id}, {"$set": {"visual_identity.logo_url": "https://files/logo.png"}})
    res = await client.patch(f"/api/v1/brand/{brand_id}/watermark", headers={"X-Workspace-Id": ws_id}, json={"image_url": "https://files/logo.png"})
    assert res.json()["watermark"]["image_url"] == "https://files/logo.png"
    res = await client.patch(f"/api/v1/brand/{brand_id}/watermark", headers={"X-Workspace-Id": ws_id}, json={"text": "@me", "image_url": "https://files/logo.png"})
    assert res.json()["watermark"]["image_url"] == "https://files/logo.png"          # kept while only the words change
    res = await client.patch(f"/api/v1/brand/{brand_id}/watermark", headers={"X-Workspace-Id": ws_id}, json={"image_url": "https://elsewhere/x.png"})
    assert res.json()["watermark"]["image_url"] == ""


async def test_the_editor_sets_the_logo_from_the_library_without_a_new_version(signup_user):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Watermark WS")
    brand_id = await _brand(client, ws_id)
    before = await brand_profiles.find_one({"id": brand_id})
    media_id = str(uuid4())
    await media_assets.insert_one({"id": media_id, "workspace_id": ws_id, "kind": "image", "url": "https://files/logo.png"})
    res = await client.put(f"/api/v1/brand/{brand_id}/logo", headers={"X-Workspace-Id": ws_id}, json={"media_id": media_id})
    assert res.status_code == 200, res.text
    assert res.json()["visual_identity"]["logo_url"] == "https://files/logo.png"
    assert (await brand_profiles.find_one({"id": brand_id}))["updated_at"] == before["updated_at"]
    res = await client.put(f"/api/v1/brand/{brand_id}/logo", headers={"X-Workspace-Id": ws_id}, json={"media_id": "nope"})
    assert res.status_code == 404


async def test_another_workspaces_picture_and_an_empty_auto_mark_are_refused(signup_user):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Watermark WS")
    brand_id = await _brand(client, ws_id)
    other = str(uuid4())
    await media_assets.insert_one({"id": other, "workspace_id": "someone-else", "kind": "image", "url": "https://files/theirs.png"})
    res = await client.patch(f"/api/v1/brand/{brand_id}/watermark", headers={"X-Workspace-Id": ws_id}, json={"image_media_id": other})
    assert res.status_code == 404
    res = await client.patch(f"/api/v1/brand/{brand_id}/watermark", headers={"X-Workspace-Id": ws_id},
                             json={"auto_apply": True, "auto_kind": "image"})
    assert res.status_code == 422


async def test_suggestions_come_back_from_the_brand(signup_user, mock_llm):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Watermark WS")
    brand_id = await _brand(client, ws_id)
    mock_llm.set_structured({"suggestions": ["@me", "© Me", "me.com"]})
    res = await client.post(f"/api/v1/brand/{brand_id}/watermark/suggestions", headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 200, res.text
    assert res.json()["suggestions"] == ["@me", "© Me", "me.com"]


async def test_exports_follow_the_brands_setting_unless_told_otherwise(signup_user):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Watermark WS")
    brand_id = await _brand(client, ws_id)
    asset = ImageAsset(id="a1", workspace_id=ws_id, brand_id=brand_id, created_by="u", created_at=datetime.now(timezone.utc),
                       updated_at=datetime.now(timezone.utc), title="T", source_type="uploaded", slides=[])
    assert await image_routes._export_stamper(asset, ws_id, None) is None              # nothing set
    await brand_profiles.update_one({"id": brand_id}, {"$set": {"watermark": MARK}})
    assert await image_routes._export_stamper(asset, ws_id, None) is not None          # on for every export
    assert await image_routes._export_stamper(asset, ws_id, False) is None             # switched off for this one
    await brand_profiles.update_one({"id": brand_id}, {"$set": {"watermark.auto_apply": False}})
    assert await image_routes._export_stamper(asset, ws_id, None) is None
    assert await image_routes._export_stamper(asset, ws_id, True) is not None          # asked for on this one


def test_a_long_text_mark_stays_on_one_line_in_its_corner():
    layer = W.auto_layer({**MARK, "text": "© A Fairly Long Brand Name"}, 1000, 1000)
    assert layer.w > 0.9 and layer.align == "right"                    # the box spans the picture, the words hug the corner
    assert W.auto_layer({**MARK, "position": "bottom_left"}, 1000, 1000).align == "left"


def test_a_slide_that_already_shows_the_watermark_gets_no_second_one():
    from app.models.image_asset import Slide

    marked = Slide(slide_number=1, title="", slide_type="x", layers=[Layer(id="w", type="watermark")])
    hidden = Slide(slide_number=2, title="", slide_type="x", layers=[Layer(id="w", type="watermark", hidden=True)])
    plain = Slide(slide_number=3, title="", slide_type="x")
    assert image_routes._has_watermark_layer(marked) and not image_routes._has_watermark_layer(hidden)
    assert not image_routes._has_watermark_layer(plain)
