"""The picture and video hand-edit runners: each reads the design, makes one named change and saves it with the version the caller saw.
The app's own routes are replaced by a recording stand-in, so no picture is drawn and no video is made."""

import pytest

from app.tools import runners_edit
from app.tools.gatekeeper import Admission, Credential, Estimate
from app.tools.contract import Scope
from app.tools.executor import ToolFailure
from app.tools.registry import get_registry
from app.video import edl

CRED = Credential("k1", "api", "ws1", "u1", frozenset({Scope.READ, Scope.CREATE}))


class Routes:
    """Stands in for the app: answers the reads it is given and records every write."""

    def __init__(self, answers: dict[tuple[str, str], object]):
        self.answers, self.writes = answers, []

    async def __call__(self, credential, method, path, query, body):
        if method != "GET":
            self.writes.append((method, path, body))
        found = self.answers.get((method, path))
        if callable(found):
            return found(body)
        return found if found is not None else {}


def _admit(tool: str, **arguments) -> Admission:
    spec = get_registry().get(tool)
    return Admission(spec, CRED, spec.input_model.model_validate(arguments), Estimate(0))


def _layer(layer_id, kind, name, **over):
    return {"id": layer_id, "type": kind, "name": name, "x": 0.05, "y": 0.1, "w": 0.9, "h": 0.1, "text": "", "color": "#FFFFFF", "fill": "#000000", **over}


def _picture(version=3, layers=None):
    layers = layers if layers is not None else [
        _layer("band", "shape", "Text band", fill="#111111", y=0.7, h=0.3), _layer("bar", "shape", "Accent bar", fill="#38bdf8"),
        _layer("head", "text", "Headline", text="Old headline", accent_word="old", accent_color="#38bdf8"),
        _layer("logo", "logo", "Logo", x=0.05, y=0.05, w=0.08, h=0.08)]
    return {"id": "img1", "version_count": version, "slides": [{"slide_number": 1, "layout": "square", "width": 1080, "height": 1080, "layers": layers, "effects": {}}]}


def _use(monkeypatch, routes: Routes):
    monkeypatch.setattr(runners_edit, "request_route", routes)


async def test_a_picture_edit_is_refused_when_the_picture_has_moved_on(monkeypatch):
    routes = Routes({("GET", "/api/v1/image-assets/img1"): _picture(version=4)})
    _use(monkeypatch, routes)
    with pytest.raises(ToolFailure) as caught:
        await runners_edit.set_headline(_admit("image.set_headline", image_asset_id="img1", base_version=3, text="New"))
    assert caught.value.code == "version_conflict" and caught.value.status == 409 and "version 4" in caught.value.message
    assert routes.writes == []                                      # nothing is written over newer work


async def test_set_headline_changes_only_the_headline_words_and_the_accent_word(monkeypatch):
    routes = Routes({("GET", "/api/v1/image-assets/img1"): _picture(), ("PUT", "/api/v1/image-assets/img1/slides/1/layers"): {"id": "img1", "version_count": 4}})
    _use(monkeypatch, routes)
    out = await runners_edit.set_headline(_admit("image.set_headline", image_asset_id="img1", base_version=3, text="Clarity beats scale", accent_word="Clarity"))
    layers = routes.writes[0][2]["layers"]
    head = next(l for l in layers if l["name"] == "Headline")
    assert head["text"] == "Clarity beats scale" and head["accent_word"] == "Clarity" and len(layers) == 4
    assert out["version"] == 4 and out["image_asset_id"] == "img1"


async def test_set_colors_changes_the_named_parts_and_refuses_a_picture_that_has_none_of_them(monkeypatch):
    routes = Routes({("GET", "/api/v1/image-assets/img1"): _picture(), ("PUT", "/api/v1/image-assets/img1/slides/1/layers"): {"id": "img1", "version_count": 4}})
    _use(monkeypatch, routes)
    await runners_edit.set_colors(_admit("image.set_colors", image_asset_id="img1", base_version=3, text_color="#112233", accent_color="#ff8800", band_color="#000000"))
    layers = {l["name"]: l for l in routes.writes[0][2]["layers"]}
    assert layers["Headline"]["color"] == "#112233" and layers["Headline"]["accent_color"] == "#ff8800" and layers["Accent bar"]["fill"] == "#ff8800" and layers["Text band"]["fill"] == "#000000"

    bare = Routes({("GET", "/api/v1/image-assets/img1"): _picture(layers=[_layer("x", "image", "Photo")])})
    _use(monkeypatch, bare)
    with pytest.raises(ToolFailure) as caught:
        await runners_edit.set_colors(_admit("image.set_colors", image_asset_id="img1", base_version=3, text_color="#112233"))
    assert caught.value.code == "not_editable"


async def test_logo_spot_moves_the_logo_to_the_chosen_corner_and_sets_the_plate(monkeypatch):
    routes = Routes({("GET", "/api/v1/image-assets/img1"): _picture(), ("PUT", "/api/v1/image-assets/img1/slides/1/layers"): {"id": "img1", "version_count": 4}})
    _use(monkeypatch, routes)
    await runners_edit.set_logo_spot(_admit("image.set_logo_spot", image_asset_id="img1", base_version=3, position="bottom_right", backing="auto"))
    logo = next(l for l in routes.writes[0][2]["layers"] if l["type"] == "logo")
    assert logo["x"] > 0.5 and 0.5 < logo["y"] < 0.7 and logo["backing"] == "auto"           # bottom right, kept above the headline band


async def test_big_number_and_chart_add_layers_and_leave_the_rest(monkeypatch):
    routes = Routes({("GET", "/api/v1/image-assets/img1"): _picture(), ("PUT", "/api/v1/image-assets/img1/slides/1/layers"): {"id": "img1", "version_count": 4}})
    _use(monkeypatch, routes)
    await runners_edit.add_big_number(_admit("image.add_big_number", image_asset_id="img1", base_version=3, figure="40%", caption="faster setup"))
    assert [l["name"] for l in routes.writes[0][2]["layers"]][-2:] == ["Big number", "Big number caption"]
    await runners_edit.add_chart(_admit("image.add_chart", image_asset_id="img1", base_version=3, title="Sales", bars=[{"label": "Jan", "value": 4}, {"label": "Feb", "value": 7}]))
    chart = routes.writes[1][2]["layers"][-1]
    assert chart["type"] == "chart" and [b["value"] for b in chart["bars"]] == [4, 7] and chart["name"] == "Sales"


async def test_the_other_picture_edits_call_the_apps_own_routes_with_the_right_body(monkeypatch):
    saved = {"id": "img1", "version_count": 4}
    routes = Routes({("GET", "/api/v1/image-assets/img1"): _picture(), ("POST", "/api/v1/image-assets/img1/slides/1/looks"): saved,
                     ("POST", "/api/v1/image-assets/img1/slides/1/reshape"): saved, ("POST", "/api/v1/image-assets/img1/slides/1/layers/reset"): saved,
                     ("POST", "/api/v1/image-assets/img1/restore/2"): saved})
    _use(monkeypatch, routes)
    await runners_edit.apply_look(_admit("image.apply_look", image_asset_id="img1", base_version=3, glow=True))
    assert routes.writes[0][2] == {"effects": {"glow": True, "dual_tone": False, "grid": False, "remove_background": False, "corner_radius": 0.0}}
    await runners_edit.reshape(_admit("image.reshape", image_asset_id="img1", base_version=3, layouts=[_first_layout()]))
    assert routes.writes[1][2]["layouts"] == [_first_layout()]
    await runners_edit.reset(_admit("image.reset", image_asset_id="img1", base_version=3))
    await runners_edit.restore_image_version(_admit("image.restore_version", image_asset_id="img1", version_number=2, base_version=3))
    assert [w[0] + " " + w[1].split("/api/v1/image-assets/img1")[1] for w in routes.writes] == [
        "POST /slides/1/looks", "POST /slides/1/reshape", "POST /slides/1/layers/reset", "POST /restore/2"]


def _first_layout():
    from app.pipelines.media.image_render import LAYOUT_DIMS

    return next(iter(LAYOUT_DIMS))


# ── video ────────────────────────────────────────────────────────────────

def _project(version=5):
    edit = edl.blank("landscape")
    edit.update({"video": [
        {"id": "a", "media_id": "m1", "at_f": 0, "dur_f": 90, "src_in_f": 0, "speed": 1.0, "fit": "cover", "fade_in_f": 0, "fade_out_f": 0, "audio": True, "gain_db": 0.0},
        {"id": "b", "media_id": "m1", "at_f": 90, "dur_f": 60, "src_in_f": 90, "speed": 1.0, "fit": "cover", "fade_in_f": 0, "fade_out_f": 0, "audio": True, "gain_db": 0.0}],
        "duration_f": 150})
    return {"id": "p1", "version": version, "edit": edl.validate(edit)}


def _put_answer(body):
    return {"id": "p1", "version": 6}


def _video_routes(version=5, extra=None):
    return Routes({("GET", "/api/v1/video-projects/p1"): _project(version), ("PUT", "/api/v1/video-projects/p1"): _put_answer, **(extra or {})})


def _saved_edit(routes: Routes, index=0) -> dict:
    """What was saved, run through the editor's own validator: the proof that every edit leaves a valid edit list."""
    return edl.validate(routes.writes[index][2]["edit"])


async def test_a_video_edit_is_refused_when_the_project_has_moved_on(monkeypatch):
    routes = _video_routes(version=7)
    _use(monkeypatch, routes)
    with pytest.raises(ToolFailure) as caught:
        await runners_edit.trim_clip(_admit("video.trim_clip", project_id="p1", base_version=5, clip_id="a", src_in_s=1, duration_s=2))
    assert caught.value.code == "version_conflict" and routes.writes == []


async def test_trim_reorder_and_transition_leave_a_valid_edit_list(monkeypatch):
    routes = _video_routes()
    _use(monkeypatch, routes)
    out = await runners_edit.trim_clip(_admit("video.trim_clip", project_id="p1", base_version=5, clip_id="a", src_in_s=1, duration_s=2))
    clip = next(c for c in _saved_edit(routes)["video"] if c["id"] == "a")
    assert clip["src_in_f"] == 30 and clip["dur_f"] == 60 and out["version"] == 6 and routes.writes[0][2]["base_version"] == 5

    await runners_edit.reorder_clips(_admit("video.reorder_clips", project_id="p1", base_version=5, clip_ids=["b", "a"]))
    order = _saved_edit(routes, 1)["video"]
    assert [c["id"] for c in order] == ["b", "a"] and order[0]["at_f"] == 0 and order[1]["at_f"] == order[0]["dur_f"]
    with pytest.raises(ToolFailure):
        await runners_edit.reorder_clips(_admit("video.reorder_clips", project_id="p1", base_version=5, clip_ids=["b"]))       # every clip named once

    await runners_edit.set_transition(_admit("video.set_transition", project_id="p1", base_version=5, clip_id="b", kind="dissolve", duration_s=1))
    assert next(c for c in _saved_edit(routes, 2)["video"] if c["id"] == "b")["transition_in"] == {"kind": "dissolve", "dur_f": 30}


async def test_text_music_captions_look_canvas_bleeps_and_emphasis_leave_a_valid_edit_list(monkeypatch):
    routes = _video_routes()
    _use(monkeypatch, routes)
    await runners_edit.add_text(_admit("video.add_text", project_id="p1", base_version=5, text="Launch day", start_s=1, duration_s=3, anim_in="fade"))
    text = _saved_edit(routes, 0)["text"][0]
    assert text["text"] == "Launch day" and text["at_f"] == 30 and text["dur_f"] == 90 and text["anim_in"] == "fade"

    await runners_edit.add_music(_admit("video.add_music", project_id="p1", base_version=5, media_id="song", gain_db=-14))
    music = _saved_edit(routes, 1)["audio"][0]
    assert music["role"] == "music" and music["dur_f"] == 150 and music["duck"] is True and music["gain_db"] == -14

    await runners_edit.set_captions_style(_admit("video.set_captions_style", project_id="p1", base_version=5, size=1.5, color="#ffff00", upper=True))
    caps = _saved_edit(routes, 2)["captions"]
    assert caps["size"] == 1.5 and caps["color"] == "#ffff00" and caps["upper"] is True

    await runners_edit.set_look(_admit("video.set_look", project_id="p1", base_version=5, clip_id="a", brightness=0.2))
    assert next(c for c in _saved_edit(routes, 3)["video"] if c["id"] == "a")["look"]["brightness"] == 0.2

    await runners_edit.set_canvas(_admit("video.set_canvas", project_id="p1", base_version=5, size="vertical"))
    edit = _saved_edit(routes, 4)
    assert (edit["width"], edit["height"]) == (1080, 1920)

    await runners_edit.add_bleeps(_admit("video.add_bleeps", project_id="p1", base_version=5, stretches=[{"start_ms": 1000, "end_ms": 1500}]))
    assert _saved_edit(routes, 5)["bleeps"] == [{"s_ms": 1000, "e_ms": 1500}]
    await runners_edit.set_emphasis(_admit("video.set_emphasis", project_id="p1", base_version=5, stretches=[{"start_ms": 200, "end_ms": 400}]))
    assert _saved_edit(routes, 6)["captions"]["emphasis"] == [{"s_ms": 200, "e_ms": 400}]


async def test_an_unknown_clip_is_refused_in_plain_words_and_restore_calls_the_apps_own_route(monkeypatch):
    routes = _video_routes(extra={("POST", "/api/v1/video-projects/p1/versions/2/restore"): {"id": "p1", "version": 6}})
    _use(monkeypatch, routes)
    with pytest.raises(ToolFailure) as caught:
        await runners_edit.trim_clip(_admit("video.trim_clip", project_id="p1", base_version=5, clip_id="nope", src_in_s=0, duration_s=1))
    assert caught.value.status == 404 and "nope" in caught.value.message
    out = await runners_edit.restore_video_version(_admit("video.restore_version", project_id="p1", version=2, base_version=5))
    assert routes.writes[-1][:2] == ("POST", "/api/v1/video-projects/p1/versions/2/restore") and out["version"] == 6


def test_every_picture_and_video_edit_tool_has_a_runner():
    from app.tools import executor

    executor.load_handlers()
    for tool in get_registry().all():
        if tool.coarse:
            assert tool.name in executor.HANDLERS, tool.name
