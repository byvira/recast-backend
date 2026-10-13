"""The layer engine behind the image editor: model checks, default design, drawing, and the save and reset endpoints.
No network: the AI picture, uploads and file fetches are stubbed like the rest of the image tests."""
import io

import pytest
from PIL import Image
from pydantic import ValidationError

from app.api.v1 import image_assets as image_module
from app.db.mongo import media_assets
from app.models.image_asset import Layer
from app.pipelines.media.image_layers import MAX_LAYERS, default_layers, layer_assets_needed, render_layers
from app.pipelines.media.image_render import BrandTokens
from tests.test_image_assets import _generate, _h, _png, _setup, stubs  # noqa: F401 — fixture reuse

BRAND = BrandTokens(primary_hex="#312e81", secondary_hex="#0f172a", accent_hex="#f59e0b", heading_font="Poppins")
SIZE = (600, 750)


def _bg(color=(60, 90, 140)) -> bytes:
    return _png((256, 256), color)


def _draw(layers, bg=None, assets=None):
    return Image.open(io.BytesIO(render_layers(size=SIZE, background_bytes=bg if bg is not None else _bg(), layers=layers, brand=BRAND, assets=assets or {}))).convert("RGB")


# ---- the model refuses bad values -----------------------------------------------------------------------------------------------------
def test_a_layer_with_a_bad_colour_or_size_is_refused():
    with pytest.raises(ValidationError):
        Layer(id="a", type="text", color="red")
    with pytest.raises(ValidationError):
        Layer(id="a", type="text", size=5)
    with pytest.raises(ValidationError):
        Layer(id="a", type="shape", opacity=2)
    with pytest.raises(ValidationError):
        Layer(id="a", type="widget")
    with pytest.raises(ValidationError):
        Layer(id="a", type="text", text="x" * 2001)


# ---- the starting design ---------------------------------------------------------------------------------------------------------------
def test_the_default_design_is_editable_layers_in_a_sensible_order():
    layers = default_layers(size=SIZE, brand=BRAND, headline="Good systems scale. To-do lists choke.", accent_keyword="scale", author="Vinod", has_logo=True, has_mascot=True)
    kinds = [(layer.type, layer.name) for layer in layers]
    assert kinds[0] == ("shape", "Text band") and ("shape", "Accent bar") in kinds and kinds.index(("text", "Headline")) > kinds.index(("shape", "Text band"))
    assert ("logo", "Logo") in kinds and ("mascot", "Mascot") in kinds and ("text", "Author") in kinds
    headline = next(layer for layer in layers if layer.name == "Headline")
    assert headline.accent_word == "scale" and headline.font == "Poppins" and 0 < headline.y < 1 and headline.y + 0.05 < 1
    assert [layer.id for layer in layer_assets_needed(layers)] == [next(l.id for l in layers if l.type == "logo"), next(l.id for l in layers if l.type == "mascot")]


def test_text_off_and_no_assets_give_no_text_band_logo_or_mascot():
    layers = default_layers(size=SIZE, brand=BRAND, headline="Hello", show_text=False)
    assert layers == []
    assert default_layers(size=SIZE, brand=BRAND, headline="") == []
    assert not [layer for layer in default_layers(size=SIZE, brand=BRAND, headline="Hi") if layer.type in ("logo", "mascot")]


def test_a_long_headline_stays_inside_the_picture():
    layers = default_layers(size=SIZE, brand=BRAND, headline="Your current posting process is silently draining budget and brand voice and no one is calling it out loud enough")
    out = _draw(layers)
    assert out.size == SIZE
    # the bottom band is dark brand colour and the top of the picture is untouched
    assert sum(out.getpixel((5, SIZE[1] - 5))) < sum(out.getpixel((5, 5)))


# ---- drawing ------------------------------------------------------------------------------------------------------------------------------
def test_moving_a_text_layer_moves_the_words():
    def text_at(x):
        layer = Layer(id="t", type="text", text="HELLO", x=x, y=0.4, w=0.4, size=0.1, color="#FF0000", font="Poppins")
        img = _draw([layer], bg=_png((64, 64), (0, 0, 0)))
        red = [(px, py) for px in range(0, SIZE[0], 3) for py in range(0, SIZE[1], 3) if img.getpixel((px, py))[0] > 180 and img.getpixel((px, py))[1] < 80]
        return min(px for px, _ in red), max(px for px, _ in red)

    left_a, _ = text_at(0.05)
    left_b, right_b = text_at(0.5)
    assert left_b - left_a >= int(0.4 * SIZE[0]) and right_b <= SIZE[0]


def test_rotation_opacity_hidden_and_order_are_obeyed():
    base = [Layer(id="r", type="shape", shape="rect", fill="#FF0000", x=0.25, y=0.4, w=0.5, h=0.1)]
    flat, rotated = _draw(base), _draw([base[0].model_copy(update={"rotation": 90})])
    assert flat.getpixel((SIZE[0] // 2, int(0.45 * SIZE[1]))) == (255, 0, 0)
    assert rotated.getpixel((SIZE[0] // 2, int(0.45 * SIZE[1] - 0.15 * SIZE[1]))) == (255, 0, 0)  # a tall bar now
    half = _draw([base[0].model_copy(update={"opacity": 0.5})]).getpixel((SIZE[0] // 2, int(0.45 * SIZE[1])))
    assert 100 < half[0] < 230
    hidden = _draw([base[0].model_copy(update={"hidden": True})])
    assert hidden.getpixel((SIZE[0] // 2, int(0.45 * SIZE[1]))) != (255, 0, 0)
    top = Layer(id="b", type="shape", shape="rect", fill="#0000FF", x=0.25, y=0.4, w=0.5, h=0.1)
    assert _draw([base[0], top]).getpixel((SIZE[0] // 2, int(0.45 * SIZE[1]))) == (0, 0, 255)
    assert _draw([top, base[0]]).getpixel((SIZE[0] // 2, int(0.45 * SIZE[1]))) == (255, 0, 0)


def test_a_layer_hanging_off_the_edge_is_clipped_not_an_error():
    out = _draw([Layer(id="o", type="shape", shape="rect", fill="#00FF00", x=0.8, y=0.9, w=0.5, h=0.3)])
    assert out.getpixel((SIZE[0] - 2, SIZE[1] - 2)) == (0, 255, 0)
    assert out.size == SIZE


def test_picture_layers_use_the_fetched_bytes_and_a_missing_one_is_skipped():
    logo = Layer(id="logo1", type="logo", x=0.1, y=0.1, w=0.3, h=0.2)
    red = _png((50, 50), (255, 0, 0))
    out = _draw([logo], assets={"logo1": red})
    assert out.getpixel((int(0.25 * SIZE[0]), int(0.2 * SIZE[1]))) == (255, 0, 0)
    missing = _draw([logo])
    assert missing.getpixel((int(0.25 * SIZE[0]), int(0.2 * SIZE[1]))) != (255, 0, 0)  # no placeholder drawn


def test_text_options_uppercase_box_shadow_spacing_and_accent_word_draw_without_error():
    layer = Layer(id="t", type="text", text="Scale steadily\nnot loudly", x=0.1, y=0.3, w=0.6, size=0.07, uppercase=True, box_color="#000000", shadow=True,
                  letter_spacing=0.1, accent_word="steadily", accent_color="#FF8800", align="center", rotation=-5)
    out = _draw([layer])
    assert any(out.getpixel((x, y)) == (0, 0, 0) for x in range(80, 400, 7) for y in range(int(0.3 * SIZE[1]), int(0.3 * SIZE[1]) + 40, 5))


def test_more_than_the_maximum_layers_are_ignored_not_fatal():
    many = [Layer(id=f"l{i}", type="shape", shape="rect", fill="#FF0000", x=0, y=0, w=0.01, h=0.01) for i in range(MAX_LAYERS + 5)]
    assert _draw(many).size == SIZE


# ---- the save and reset endpoints ------------------------------------------------------------------------------------------------------
@pytest.fixture
def served(monkeypatch):
    """Stored files are 'fetched' as a small picture (the suite never touches the network)."""
    async def fake_fetch(url):
        return _png((256, 256), (40, 70, 120))

    monkeypatch.setattr(image_module, "_fetch_logo_bytes", fake_fetch)


async def test_saving_edited_layers_redraws_without_a_new_ai_picture(signup_user, stubs, served):  # noqa: F811
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()
    slide = asset["slides"][0]
    layers = slide["layers"]
    headline = next(layer for layer in layers if layer["name"] == "Headline")
    headline["text"] = "A better headline"
    headline["x"] = 0.2
    before_backgrounds = len(stubs["backgrounds"])

    res = await client.put(f"/api/v1/image-assets/{asset['id']}/slides/1/layers", json={"layers": layers}, headers=_h(ws_id))
    assert res.status_code == 200, res.text
    updated = res.json()["slides"][0]
    assert updated["media_id"] != slide["media_id"] and updated["background_media_id"] == slide["background_media_id"]
    assert next(layer for layer in updated["layers"] if layer["name"] == "Headline")["text"] == "A better headline"
    assert len(stubs["backgrounds"]) == before_backgrounds, "editing must not make a new AI picture"
    assert res.json()["version_count"] == 2
    new_media = await media_assets.find_one({"id": updated["media_id"]})
    assert new_media["source"] == "rendered" and new_media["workspace_id"] == ws_id


async def test_saving_refuses_bad_layers(signup_user, stubs, served):  # noqa: F811
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()
    url = f"/api/v1/image-assets/{asset['id']}/slides/1/layers"
    good = asset["slides"][0]["layers"]
    dup = good + [dict(good[0])]
    assert (await client.put(url, json={"layers": dup}, headers=_h(ws_id))).status_code == 400
    foreign = good + [{"id": "img1", "type": "image", "media_id": "not-in-this-workspace"}]
    assert (await client.put(url, json={"layers": foreign}, headers=_h(ws_id))).status_code == 400
    assert (await client.put(url, json={"layers": [{"id": "x", "type": "text", "color": "red"}]}, headers=_h(ws_id))).status_code == 422
    too_many = [{"id": f"l{i}", "type": "shape"} for i in range(MAX_LAYERS + 1)]
    assert (await client.put(url, json={"layers": too_many}, headers=_h(ws_id))).status_code == 422
    assert (await client.put(f"/api/v1/image-assets/{asset['id']}/slides/9/layers", json={"layers": good}, headers=_h(ws_id))).status_code == 404


async def test_reset_goes_back_to_the_starting_design(signup_user, stubs, served):  # noqa: F811
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()
    base = f"/api/v1/image-assets/{asset['id']}/slides/1/layers"
    await client.put(base, json={"layers": []}, headers=_h(ws_id))
    res = await client.post(f"{base}/reset", headers=_h(ws_id))
    assert res.status_code == 200, res.text
    layers = res.json()["slides"][0]["layers"]
    assert any(layer["name"] == "Headline" and layer["text"] == "Clarity beats scale" for layer in layers)
    assert res.json()["version_count"] == 3


async def _upload(client, ws_id, brand_id, size=(300, 200)):
    up = await client.post("/api/v1/image-assets/upload", data={"title": "Photo", "brand_id": brand_id}, files={"file": ("p.png", _png(size), "image/png")}, headers=_h(ws_id))
    assert up.status_code == 201, up.text
    return up.json()


async def test_an_uploaded_picture_keeps_its_own_shape_and_can_be_edited(signup_user, stubs, served):  # noqa: F811
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = await _upload(client, ws_id, brand_id)
    slide = asset["slides"][0]
    assert (slide["width"], slide["height"]) == (300, 200) and slide["layout"] is None and slide["layers"] == []

    layers = [{"id": "t1", "type": "text", "text": "Hello", "x": 0.1, "y": 0.1, "w": 0.5, "size": 0.08, "color": "#FFFFFF"}]
    res = await client.put(f"/api/v1/image-assets/{asset['id']}/slides/1/layers", json={"layers": layers}, headers=_h(ws_id))
    assert res.status_code == 200, res.text
    edited = res.json()["slides"][0]
    # the uploaded picture is kept as the backdrop and the finished picture is a new file drawn at the same shape
    assert edited["background_media_id"] == slide["media_id"] and edited["media_id"] != slide["media_id"]
    assert [layer["text"] for layer in edited["layers"]] == ["Hello"]
    assert Image.open(io.BytesIO(stubs["uploads"][-1])).size == (300, 200)

    # saving again draws over the original, never over the finished picture (so nothing is drawn twice)
    again = await client.put(f"/api/v1/image-assets/{asset['id']}/slides/1/layers", json={"layers": layers}, headers=_h(ws_id))
    assert again.json()["slides"][0]["background_media_id"] == slide["media_id"]


async def test_starting_again_on_an_uploaded_picture_takes_every_edit_off(signup_user, stubs, served):  # noqa: F811
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = await _upload(client, ws_id, brand_id)
    base = f"/api/v1/image-assets/{asset['id']}/slides/1/layers"
    assert (await client.post(f"{base}/reset", headers=_h(ws_id))).status_code == 400  # nothing edited yet
    await client.put(base, json={"layers": [{"id": "t1", "type": "text", "text": "Hi"}]}, headers=_h(ws_id))
    res = await client.post(f"{base}/reset", headers=_h(ws_id))
    assert res.status_code == 200, res.text
    assert res.json()["slides"][0]["layers"] == []


async def test_a_picture_made_before_the_editor_is_not_drawn_over_twice(signup_user, stubs, served):  # noqa: F811
    from app.db.mongo import image_assets

    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()
    finished = asset["slides"][0]["media_id"]
    # make it look like a picture from before the editor: a finished picture only, no clean backdrop, no layers
    await image_assets.update_one({"id": asset["id"]}, {"$set": {"slides.0.background_media_id": None, "slides.0.layers": []}})
    base = f"/api/v1/image-assets/{asset['id']}/slides/1/layers"
    layers = [{"id": "t1", "type": "text", "text": "Added"}]

    first = (await client.put(base, json={"layers": layers}, headers=_h(ws_id))).json()["slides"][0]
    assert first["background_media_id"] == finished and first["baked_text"] is True and first["media_id"] != finished
    second = (await client.put(base, json={"layers": layers}, headers=_h(ws_id))).json()["slides"][0]
    assert second["background_media_id"] == finished, "a second save must draw over the original, not over the first edit"

    reset = await client.post(f"{base}/reset", headers=_h(ws_id))
    assert reset.status_code == 200 and reset.json()["slides"][0]["layers"] == [], "no second headline stacked on the old words"


# ---- looks on every picture of a set -------------------------------------------------------------------------------------------------------
async def _pack(client, ws_id, brand_id, count=3):
    res = await _generate(client, ws_id, brand_id, count=count)
    assert res.status_code in (200, 201), res.text
    return res.json()


def _looks_form(effects: dict, **extra):
    import json

    return {"effects": json.dumps(effects), **{k: json.dumps(v) if not isinstance(v, str) else v for k, v in extra.items()}}


EFFECTS = {"glow": True, "dual_tone": False, "grid": True, "remove_background": False, "corner_radius": 0.05}


async def test_looks_are_applied_to_every_picture_of_a_set_in_one_new_version(signup_user, stubs, served):  # noqa: F811
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = await _pack(client, ws_id, brand_id, 3)
    before = {sl["slide_number"]: sl["media_id"] for sl in asset["slides"]}
    assert len(before) == 3

    res = await client.post(f"/api/v1/image-assets/{asset['id']}/looks", data=_looks_form(EFFECTS), headers=_h(ws_id))
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["version_count"] == asset["version_count"] + 1, "one version for the whole set, not one per picture"
    for sl in out["slides"]:
        assert sl["effects"]["glow"] is True and sl["effects"]["grid"] is True
        assert sl["media_id"] != before[sl["slide_number"]]
        assert sl["background_media_id"], "the original is kept so the looks can be switched off again"


async def test_apply_to_all_replaces_a_look_that_was_already_there(signup_user, stubs, served):  # noqa: F811
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = await _pack(client, ws_id, brand_id, 2)
    await client.post(f"/api/v1/image-assets/{asset['id']}/slides/1/looks", json={"effects": {**EFFECTS, "glow": False, "grid": False, "dual_tone": True}}, headers=_h(ws_id))
    res = await client.post(f"/api/v1/image-assets/{asset['id']}/looks", data=_looks_form(EFFECTS), headers=_h(ws_id))
    assert [sl["effects"]["dual_tone"] for sl in res.json()["slides"]] == [False, False]
    assert [sl["effects"]["glow"] for sl in res.json()["slides"]] == [True, True]


async def test_looks_can_name_the_pictures_and_bad_requests_change_nothing(signup_user, stubs, served):  # noqa: F811
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = await _pack(client, ws_id, brand_id, 3)
    url = f"/api/v1/image-assets/{asset['id']}/looks"
    some = await client.post(url, data=_looks_form(EFFECTS, slide_numbers=[1, 3]), headers=_h(ws_id))
    assert some.status_code == 200
    glow = {sl["slide_number"]: sl["effects"].get("glow") for sl in some.json()["slides"]}
    assert glow == {1: True, 2: None, 3: True} or glow[2] in (None, False)

    version = some.json()["version_count"]
    assert (await client.post(url, data={"effects": "not json"}, headers=_h(ws_id))).status_code == 422
    assert (await client.post(url, data=_looks_form(EFFECTS, slide_numbers=[99]), headers=_h(ws_id))).status_code == 404
    assert (await client.post(url, data=_looks_form(EFFECTS, cutout_slides=[1]), headers=_h(ws_id))).status_code == 422  # a cut-out number with no cut-out
    unchanged = (await client.get(f"/api/v1/image-assets/{asset['id']}", headers=_h(ws_id))).json()
    assert unchanged["version_count"] == version


async def test_a_failing_picture_stops_the_whole_set_and_says_which(signup_user, stubs, served, monkeypatch):  # noqa: F811
    from fastapi import HTTPException

    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = await _pack(client, ws_id, brand_id, 3)
    real = image_module._draw_and_store
    calls = {"n": 0}

    async def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise HTTPException(status_code=503, detail="Removing the background isn't available right now.")
        return await real(**kwargs)

    monkeypatch.setattr(image_module, "_draw_and_store", flaky)
    res = await client.post(f"/api/v1/image-assets/{asset['id']}/looks", data=_looks_form(EFFECTS), headers=_h(ws_id))
    assert res.status_code == 503 and res.json()["detail"].startswith("Picture 2:")
    after = (await client.get(f"/api/v1/image-assets/{asset['id']}", headers=_h(ws_id))).json()
    assert after["version_count"] == asset["version_count"], "nothing is saved when one picture fails"
    assert all(not sl["effects"].get("glow") for sl in after["slides"])
