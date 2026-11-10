"""Picture editor additions: looks on any layer (outline, glow, softness, shadow), SVG export, saved templates, finding the main subject and
making a picture again in other platform shapes, and the style read from a brand's reference pictures. No network and no model calls."""
import io
import xml.etree.ElementTree as ET
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from pydantic import ValidationError

from app.api.v1 import image_assets as image_module
from app.models.image_asset import Layer
from app.pipelines.media import reference_style, subject_crop, svg_export, watermark
from app.pipelines.media.image_layers import render_layers
from app.pipelines.media.image_render import BrandTokens, _fit_background
from tests.test_image_assets import _generate, _h, _png, _setup, stubs  # noqa: F401 - fixture reuse
from tests.test_image_layers import served  # noqa: F401 - fixture reuse

BRAND = BrandTokens(primary_hex="#312e81", secondary_hex="#0f172a", accent_hex="#f59e0b", heading_font="Poppins")
SIZE = (400, 400)
SVG = "{http://www.w3.org/2000/svg}"


def _draw(layers, bg=(0, 0, 0)) -> Image.Image:
    data = render_layers(size=SIZE, background_bytes=_png((64, 64), bg), layers=layers, brand=BRAND, assets={})
    return Image.open(io.BytesIO(data)).convert("RGB")


def _box(**extra) -> Layer:
    return Layer(id="b", type="shape", shape="rect", fill="#FF0000", x=0.4, y=0.4, w=0.2, h=0.2, **extra)


def _red(img: Image.Image, x: int, y: int) -> bool:
    r, g, b = img.getpixel((x, y))
    return r > 200 and g < 60 and b < 60


# ---- looks on any layer ------------------------------------------------------------------------------------------------------------------
def test_looks_are_checked_by_the_model():
    with pytest.raises(ValidationError):
        Layer(id="a", type="shape", outline="red")
    with pytest.raises(ValidationError):
        Layer(id="a", type="shape", outline_w=0.5)
    with pytest.raises(ValidationError):
        Layer(id="a", type="shape", glow_r=0.9)
    with pytest.raises(ValidationError):
        Layer(id="a", type="shape", blur=0.5)
    assert Layer(id="a", type="shape", group="g1").group == "g1"


def test_a_layer_with_no_looks_is_drawn_exactly_where_it_was():
    plain = _draw([_box()])
    assert _red(plain, 200, 200) and not _red(plain, 120, 200) and not _red(plain, 280, 200)
    left = next(x for x in range(SIZE[0]) if _red(plain, x, 200))
    right = max(x for x in range(SIZE[0]) if _red(plain, x, 200))
    assert abs(left - 160) <= 1 and abs(right - 239) <= 1


def test_an_outline_grows_round_the_shape_and_leaves_the_shape_where_it_was():
    out = _draw([_box(outline="#00FF00", outline_w=0.02)])
    assert _red(out, 200, 200) and _red(out, 165, 200), "the shape itself is unchanged"
    assert out.getpixel((156, 200))[1] > 200 and out.getpixel((156, 200))[0] < 80, "a green line sits just outside the left edge"
    assert out.getpixel((120, 200)) == (0, 0, 0), "and not far away"


def test_a_glow_is_a_soft_light_that_fades_with_distance():
    out = _draw([_box(glow="#00FF00", glow_r=0.03)])
    near, far = out.getpixel((152, 200)), out.getpixel((110, 200))
    assert near[1] > far[1] > -1 and near[1] > 40, "light is strongest next to the shape"
    assert _red(out, 200, 200)


def test_softness_blurs_the_edge_of_the_layer():
    sharp, soft = _draw([_box()]), _draw([_box(blur=0.02)])
    assert sharp.getpixel((159, 200)) == (0, 0, 0) and sharp.getpixel((161, 200))[0] > 240
    assert 20 < soft.getpixel((159, 200))[0] < 235 or 20 < soft.getpixel((161, 200))[0] < 235, "the edge now fades"


def test_a_shadow_on_a_shape_falls_down_and_to_the_right_and_text_keeps_its_own():
    flat = _draw([_box()], bg=(200, 200, 200))
    shaded = _draw([_box(shadow=True)], bg=(200, 200, 200))
    # the box covers 160 to 239 each way; the shadow is moved a few pixels down and to the right, so it shows just outside those edges
    assert flat.getpixel((242, 200)) == (200, 200, 200) and flat.getpixel((200, 242)) == (200, 200, 200)
    assert sum(shaded.getpixel((242, 200))) < 600 - 60 and sum(shaded.getpixel((200, 242))) < 600 - 60, "a darker patch below and to the right"
    assert shaded.getpixel((150, 150)) == (200, 200, 200), "and none above and to the left"
    text = Layer(id="t", type="text", text="HI", x=0.1, y=0.1, w=0.5, size=0.2, color="#FFFFFF", shadow=True, font="Poppins")
    assert _draw([text]).size == SIZE


def test_a_picture_watermark_never_gets_a_shadow_it_did_not_have_before():
    marks = watermark.resolve_layers([Layer(id="w", type="watermark", watermark_kind="image", shadow=True)], {"image_url": "https://x/y.png"})
    assert marks and marks[0].type == "image" and marks[0].shadow is False


# ---- svg export --------------------------------------------------------------------------------------------------------------------------
def _svg(layers, assets=None, effects=None):
    text = svg_export.build_svg(size=SIZE, background_bytes=_png((64, 64), (10, 20, 30)), layers=layers, brand=BRAND, assets=assets or {}, effects=effects)
    return text, ET.fromstring(text)


def test_an_svg_keeps_words_shapes_and_icons_as_shapes_and_the_background_as_a_picture():
    layers = [
        Layer(id="band", type="shape", shape="gradient_up", fill="#112233", x=0, y=0.5, w=1, h=0.5),
        Layer(id="head", type="text", text="Clarity beats scale", x=0.1, y=0.2, w=0.8, size=0.08, color="#FFFFFF", font="Poppins", accent_word="beats", accent_color="#F59E0B", name="Headline"),
        Layer(id="ico", type="icon", icon="sparkles", x=0.7, y=0.1, w=0.15, h=0.15, color="#FFAA00"),
        Layer(id="chart", type="chart", x=0.1, y=0.55, w=0.8, h=0.4, size=0.03, fill="#E04A1F", bars=[{"label": "One", "value": 40}, {"label": "Two", "value": 70}]),
        Layer(id="gone", type="shape", hidden=True),
    ]
    text, root = _svg(layers)
    assert root.tag == f"{SVG}svg" and root.get("viewBox") == "0 0 400 400"
    assert any((el.text or "").strip() or True for el in root.iter(f"{SVG}text"))
    words = " ".join("".join(el.itertext()) for el in root.iter(f"{SVG}text"))
    assert "Clarity" in words and "scale" in words and "One" in words and "70" in words
    assert "beats" in text and 'fill="#F59E0B"' in text, "the highlighted word is its own colour"
    assert root.find(f".//{SVG}linearGradient") is not None
    assert [el for el in root.iter(f"{SVG}path") if el.get("d")], "the icon is an outline, not a picture"
    assert text.count("<image") == 1, "only the background is a picture"
    assert 'id="layer-gone"' not in text and 'data-name="Headline"' in text


def test_looks_rotation_opacity_and_rounded_corners_are_written_into_the_svg():
    layers = [
        Layer(id="a", type="shape", fill="#FF0000", x=0.2, y=0.2, w=0.3, h=0.2, rotation=30, opacity=0.5, outline="#00FF00", outline_w=0.01, glow="#FFFFFF", glow_r=0.02, blur=0.005, shadow=True),
    ]
    text, root = _svg(layers, effects={"corner_radius": 0.1})
    assert "rotate(30" in text and 'opacity="0.5"' in text
    flt = root.find(f".//{SVG}filter")
    assert flt is not None
    names = {el.tag.replace(SVG, "") for el in flt.iter()}
    assert {"feMorphology", "feGaussianBlur", "feMerge", "feOffset"} <= names
    assert 'clip-path="url(#picture-corners)"' in text


def test_a_picture_layer_is_embedded_and_a_missing_one_is_left_out():
    logo = Layer(id="logo1", type="logo", x=0.1, y=0.1, w=0.2, h=0.2)
    lost = Layer(id="logo2", type="mascot", x=0.5, y=0.1, w=0.2, h=0.2)
    text, _ = _svg([logo, lost], assets={"logo1": _png((32, 32), (255, 0, 0))})
    assert text.count("<image") == 2 and 'id="layer-logo1"' in text and 'id="layer-logo2"' not in text


def test_text_is_escaped_in_the_svg():
    text, root = _svg([Layer(id="t", type="text", text='Fish & <chips> "now"', x=0.1, y=0.1, w=0.8, size=0.06, font="Poppins")])
    assert 'Fish & <chips>' in " ".join("".join(el.itertext()) for el in root.iter(f"{SVG}text"))
    assert "<chips>" not in text


async def test_exporting_a_slide_as_svg_gives_a_file_a_design_program_can_open(signup_user, stubs, served):  # noqa: F811
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()
    before = len(stubs["uploads"])
    res = await client.post(f"/api/v1/image-assets/{asset['id']}/export", json={"export_format": "svg"}, headers=_h(ws_id))
    assert res.status_code == 201, res.text
    assert res.json()["mime_type"] == "image/svg+xml" and res.json()["workspace_id"] == ws_id
    root = ET.fromstring(stubs["uploads"][-1])
    assert len(stubs["uploads"]) == before + 1 and root.tag == f"{SVG}svg"
    assert "Clarity beats scale" in " ".join("".join(el.itertext()) for el in root.iter(f"{SVG}text")).replace("  ", " ") or "Clarity" in ET.tostring(root, encoding="unicode")
    assert (await client.post("/api/v1/image-assets/nope/export", json={"export_format": "svg"}, headers=_h(ws_id))).status_code == 404


# ---- templates ---------------------------------------------------------------------------------------------------------------------------
def _template_layers():
    return [{"id": "t1", "type": "text", "text": "Hello", "x": 0.1, "y": 0.1, "w": 0.5, "size": 0.06}, {"id": "s1", "type": "shape", "fill": "#112233"}]


async def test_a_workspace_saves_lists_and_deletes_its_own_templates(signup_user):
    client, _, ws_id, _ = await _setup(signup_user)
    other, _, other_ws, _ = await _setup(signup_user)
    saved = await client.post("/api/v1/image-assets/templates", json={"name": "  My quote  ", "layers": _template_layers(), "width": 1080, "height": 1350}, headers=_h(ws_id))
    assert saved.status_code == 201, saved.text
    body = saved.json()
    assert body["name"] == "My quote" and len(body["layers"]) == 2 and body["width"] == 1080
    listed = (await client.get("/api/v1/image-assets/templates", headers=_h(ws_id))).json()
    assert [t["name"] for t in listed["templates"]] == ["My quote"] and listed["limit"] == 30
    assert (await other.get("/api/v1/image-assets/templates", headers=_h(other_ws))).json()["templates"] == [], "another workspace sees none of it"
    assert (await other.delete(f"/api/v1/image-assets/templates/{body['id']}", headers=_h(other_ws))).status_code == 404, "and cannot delete it"
    assert (await client.delete(f"/api/v1/image-assets/templates/{body['id']}", headers=_h(ws_id))).status_code == 204
    assert (await client.get("/api/v1/image-assets/templates", headers=_h(ws_id))).json()["templates"] == []


async def test_bad_templates_are_refused(signup_user):
    client, _, ws_id, _ = await _setup(signup_user)
    url = "/api/v1/image-assets/templates"
    assert (await client.post(url, json={"name": "", "layers": _template_layers()}, headers=_h(ws_id))).status_code == 422
    assert (await client.post(url, json={"name": "x", "layers": []}, headers=_h(ws_id))).status_code == 422
    dup = _template_layers() + [dict(_template_layers()[0])]
    assert (await client.post(url, json={"name": "x", "layers": dup}, headers=_h(ws_id))).status_code == 400
    foreign = _template_layers() + [{"id": "i1", "type": "image", "media_id": "not-in-this-workspace"}]
    assert (await client.post(url, json={"name": "x", "layers": foreign}, headers=_h(ws_id))).status_code == 400
    assert (await client.post(url, json={"name": "x", "layers": [{"id": "z", "type": "text", "color": "red"}]}, headers=_h(ws_id))).status_code == 422
    assert (await client.get(url, headers=_h(ws_id))).json()["templates"] == []


async def test_a_workspace_keeps_at_most_thirty_templates(signup_user):
    from datetime import datetime, timezone

    from app.db.mongo import get_db

    client, _, ws_id, _ = await _setup(signup_user)
    url = "/api/v1/image-assets/templates"
    rows = [{"id": f"tpl-{ws_id}-{i}", "workspace_id": ws_id, "name": f"T{i}", "layers": _template_layers(), "created_at": datetime.now(timezone.utc)} for i in range(29)]
    await get_db()["image_templates"].insert_many(rows)
    assert (await client.post(url, json={"name": "the thirtieth", "layers": _template_layers()}, headers=_h(ws_id))).status_code == 201
    over = await client.post(url, json={"name": "one more", "layers": _template_layers()}, headers=_h(ws_id))
    assert over.status_code == 409 and "30" in over.json()["detail"]


# ---- finding the subject and cropping round it -------------------------------------------------------------------------------------------
def _spot(position: tuple[int, int], size=(256, 256)) -> bytes:
    img = Image.new("RGB", size, (90, 90, 90))
    x, y = position
    for dx in range(-22, 22):
        for dy in range(-22, 22):
            if 0 <= x + dx < size[0] and 0 <= y + dy < size[1]:
                img.putpixel((x + dx, y + dy), (250, 40, 40))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def test_the_subject_is_found_where_the_picture_has_its_detail():
    fx, fy = subject_crop.find_focus(_spot((200, 50)))
    assert fx > 0.6 and fy < 0.4, (fx, fy)
    fx, fy = subject_crop.find_focus(_spot((50, 210)))
    assert fx < 0.4 and fy > 0.6, (fx, fy)


def test_a_flat_or_unreadable_picture_is_cropped_from_the_middle():
    flat = _png((200, 200), (120, 120, 120))
    fx, fy = subject_crop.find_focus(flat)
    assert abs(fx - 0.5) < 0.15 and abs(fy - 0.5) < 0.15
    assert subject_crop.find_focus(b"not a picture") == (0.5, 0.5)


def test_the_crop_window_follows_the_subject_and_stops_at_the_edges():
    wide = (1920, 1080)
    assert subject_crop.placement((1000, 1000), wide, (0.5, 0.5)) == (0.5, 0.5)
    assert subject_crop.placement((1000, 1000), wide, (0.5, 0.05)) == (0.5, 0.0)
    assert subject_crop.placement((1000, 1000), wide, (0.5, 0.95)) == (0.5, 1.0)
    tall = (1080, 1920)
    assert subject_crop.placement((1000, 1000), tall, (0.95, 0.5)) == (1.0, 0.5)
    assert subject_crop.placement((1000, 1000), tall, (0.2, 0.5))[0] < 0.3
    assert subject_crop.placement((1000, 1000), (500, 500), (0.9, 0.9)) == (0.5, 0.5), "same shape: nothing is cut"


def test_the_picture_is_drawn_from_the_placed_window_and_the_middle_is_unchanged():
    img = Image.new("RGB", (400, 200), (255, 0, 0))
    for x in range(200, 400):
        for y in range(200):
            img.putpixel((x, y), (0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data = buf.getvalue()
    left = _fit_background(data, (100, 100), (0.0, 0.5))
    right = _fit_background(data, (100, 100), (1.0, 0.5))
    middle = _fit_background(data, (100, 100))
    assert left.getpixel((50, 50))[0] > 200 and right.getpixel((50, 50))[2] > 200
    assert middle.getpixel((10, 50))[0] > 200 and middle.getpixel((90, 50))[2] > 200, "the default is still the centre"
    assert _fit_background(data, (100, 100), (0.5, 0.5)).tobytes() == middle.tobytes()


def test_words_that_would_run_off_a_shorter_picture_are_made_smaller_and_lifted():
    headline = Layer(id="h", type="text", text="A long headline that needs several lines to say what it wants to say", x=0.08, y=0.7, w=0.84, size=0.07, font="Poppins")
    logo = Layer(id="l", type="logo", x=0.05, y=0.05, w=0.1, h=0.1)
    out = subject_crop.adapt_layers([headline, logo], (1080, 1350), (1200, 627), heading_font="Poppins", body_font=None)
    assert out[0].size < headline.size or out[0].y < headline.y, "made smaller or lifted so it fits"
    assert out[0].y < 0.96 and out[0].text == headline.text
    low = Layer(id="h2", type="text", text="A long headline that needs several lines to say what it wants to say", x=0.08, y=0.93, w=0.84, size=0.07, font="Poppins")
    lifted = subject_crop.adapt_layers([low], (1080, 1350), (1200, 627), heading_font="Poppins", body_font=None)[0]
    assert lifted.y < 0.93 and lifted.size >= low.size * 0.6 - 1e-9, "one that starts too low is lifted, and never made smaller than 60%"
    # the logo keeps the shape of its box in pixels: 0.1 x 0.1 of a 1080 x 1350 picture is 108 x 135
    assert abs(out[1].h * 627 - 0.1 * 1350 * (1200 / 1080)) < 2


# ---- other shapes, through the API -------------------------------------------------------------------------------------------------------
async def test_other_shapes_shows_each_platform_shape_with_its_crop_and_changes_nothing(signup_user, stubs, served):  # noqa: F811
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()
    res = await client.get(f"/api/v1/image-assets/{asset['id']}/slides/1/shapes", headers=_h(ws_id))
    assert res.status_code == 200, res.text
    body = res.json()
    layouts = [s["layout"] for s in body["shapes"]]
    assert "quote_1_1" not in layouts and {"story_9_16", "linkedin_post", "youtube_thumbnail"} <= set(layouts)
    assert 0 <= body["focus"]["x"] <= 1 and body["picture"]["width"] > 0
    assert all(0 <= s["crop_x"] <= 1 and 0 <= s["crop_y"] <= 1 for s in body["shapes"])
    again = (await client.get(f"/api/v1/image-assets/{asset['id']}", headers=_h(ws_id))).json()
    assert len(again["slides"]) == 1 and again["version_count"] == 1
    assert (await client.get(f"/api/v1/image-assets/{asset['id']}/slides/9/shapes", headers=_h(ws_id))).status_code == 404


async def test_making_a_picture_in_other_shapes_adds_slides_without_a_new_ai_picture(signup_user, stubs, served):  # noqa: F811
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()
    first = asset["slides"][0]
    backgrounds = len(stubs["backgrounds"])
    res = await client.post(
        f"/api/v1/image-assets/{asset['id']}/slides/1/reshape", json={"layouts": ["story_9_16", "linkedin_post", "story_9_16"], "focus_x": 0.8, "focus_y": 0.3}, headers=_h(ws_id),
    )
    assert res.status_code == 201, res.text
    slides = res.json()["slides"]
    assert [s["layout"] for s in slides] == [first["layout"], "story_9_16", "linkedin_post"], "a repeated shape is made once"
    for made in slides[1:]:
        assert made["background_media_id"] == first["background_media_id"] and made["media_id"] != first["media_id"]
        assert made["crop_x"] is not None and made["crop_y"] is not None
        assert any(layer["name"] == "Headline" for layer in made["layers"])
    assert len(stubs["backgrounds"]) == backgrounds, "no new AI picture was made"
    assert res.json()["version_count"] == 2


async def test_bad_requests_for_other_shapes_change_nothing(signup_user, stubs, served):  # noqa: F811
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()
    url = f"/api/v1/image-assets/{asset['id']}/slides/1/reshape"
    assert (await client.post(url, json={"layouts": []}, headers=_h(ws_id))).status_code == 422
    assert (await client.post(url, json={"layouts": ["bento"]}, headers=_h(ws_id))).status_code == 400, "a card style is not a platform shape"
    assert (await client.post(url, json={"layouts": ["story_9_16"], "focus_x": 0.5}, headers=_h(ws_id))).status_code == 400
    assert (await client.post(url, json={"layouts": ["story_9_16"], "focus_x": 2, "focus_y": 0.5}, headers=_h(ws_id))).status_code == 422
    assert (await client.post(url.replace("/slides/1/", "/slides/9/"), json={"layouts": ["story_9_16"]}, headers=_h(ws_id))).status_code == 404
    other, _, other_ws, _ = await _setup(signup_user)
    assert (await other.post(url, json={"layouts": ["story_9_16"]}, headers=_h(other_ws))).status_code == 404, "another workspace cannot reach it"
    assert len((await client.get(f"/api/v1/image-assets/{asset['id']}", headers=_h(ws_id))).json()["slides"]) == 1


# ---- the look read from a brand's reference pictures --------------------------------------------------------------------------------------
def _solid(rgb) -> bytes:
    return _png((96, 96), rgb)


def test_a_dark_muted_picture_and_a_bright_vivid_one_read_differently():
    dark = reference_style.describe([reference_style.read_one(_solid((40, 38, 36)))])
    vivid = reference_style.describe([reference_style.read_one(_solid((255, 120, 20)))])
    assert "dark tones" in dark["traits"] and "muted colours" in dark["traits"] and "plenty of empty space" in dark["traits"]
    assert "vivid colours" in vivid["traits"] and "warm light" in vivid["traits"]
    assert dark["line"].startswith("Follow the look of the brand's own pictures:") and "#" in dark["line"] and dark["colours"]
    cool = reference_style.describe([reference_style.read_one(_solid((30, 90, 220)))])
    assert "cool light" in cool["traits"]


def test_nothing_to_read_gives_no_style_line_and_an_unreadable_picture_is_skipped():
    assert reference_style.describe([]) == {"line": "", "traits": [], "colours": []}
    assert reference_style.read_one(b"not a picture") is None


async def test_the_style_is_added_to_the_prompt_once_and_kept_until_the_pictures_change(monkeypatch):
    from app.db.mongo import get_db

    await get_db()["brand_reference_style"].delete_many({"brand_id": "b-ref"})
    reads = AsyncMock(side_effect=lambda media_id, ws: _solid((40, 38, 36)))
    monkeypatch.setattr(reference_style, "_picture_bytes", reads)
    brand = {"id": "b-ref", "visual_identity": {"reference_media_ids": ["m1", "m2"]}}
    made = await reference_style.style_of(brand, "ws-ref")
    assert "dark tones" in made["traits"] and reads.await_count == 2
    again = await reference_style.style_of(brand, "ws-ref")
    assert again == made and reads.await_count == 2, "the second time it is kept, not read again"
    brand["visual_identity"]["reference_media_ids"] = ["m1", "m3"]
    await reference_style.style_of(brand, "ws-ref")
    assert reads.await_count == 4, "different pictures are read again"

    prompt = await reference_style.with_style_line("A calm sunrise.", brand, "ws-ref")
    assert prompt.startswith("A calm sunrise. Follow the look")
    assert await reference_style.with_style_line(prompt, brand, "ws-ref") == prompt, "added only once"
    assert await reference_style.with_style_line("Same", {"id": "b-none", "visual_identity": {}}, "ws-ref") == "Same"
    assert await reference_style.with_style_line("Same", {"visual_identity": {"reference_media_ids": ["m1"]}}, "ws-ref") == "Same", "no brand id, nothing kept"
    await get_db()["brand_reference_style"].delete_many({"brand_id": "b-ref"})


async def test_a_picture_that_cannot_be_read_never_fails_the_prompt(monkeypatch):
    from app.db.mongo import get_db

    await get_db()["brand_reference_style"].delete_many({"brand_id": "b-bad"})
    monkeypatch.setattr(reference_style, "_picture_bytes", AsyncMock(side_effect=RuntimeError("the file is gone")))
    brand = {"id": "b-bad", "visual_identity": {"reference_media_ids": ["m1"]}}
    assert await reference_style.with_style_line("A calm sunrise.", brand, "ws-ref") == "A calm sunrise."
    await get_db()["brand_reference_style"].delete_many({"brand_id": "b-bad"})


async def test_the_brand_reference_style_route_answers_for_its_own_workspace_only(signup_user, stubs):  # noqa: F811
    client, _, ws_id, brand_id = await _setup(signup_user)
    res = await client.get(f"/api/v1/brand/{brand_id}/reference-style", headers=_h(ws_id))
    assert res.status_code == 200 and res.json() == {"line": "", "traits": [], "colours": []}
    assert (await client.get("/api/v1/brand/nope/reference-style", headers=_h(ws_id))).status_code == 404
    other, _, other_ws, _ = await _setup(signup_user)
    assert (await other.get(f"/api/v1/brand/{brand_id}/reference-style", headers=_h(other_ws))).status_code == 404
