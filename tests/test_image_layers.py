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


async def test_an_uploaded_picture_has_no_layers_to_edit(signup_user, stubs, served):  # noqa: F811
    client, _, ws_id, brand_id = await _setup(signup_user)
    up = await client.post("/api/v1/image-assets/upload", data={"title": "Photo", "brand_id": brand_id}, files={"file": ("p.png", _png(), "image/png")}, headers=_h(ws_id))
    assert up.status_code == 201, up.text
    res = await client.put(f"/api/v1/image-assets/{up.json()['id']}/slides/1/layers", json={"layers": []}, headers=_h(ws_id))
    assert res.status_code == 400 and "uploaded" in res.json()["detail"]
