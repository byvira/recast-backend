"""Runners for the hand-edit tools: the nine picture edits (pictures.py) and the eleven video edits (videos.py).

A hand edit is a small, named change to a saved design. The runner reads the design through the app's own route, makes the change, and saves
it through the app's own route, so the app's checks, permissions, plan limits and versioning all apply. Every edit names the version the
caller last saw (`base_version`); a design that has moved on since is refused with the current version, and nothing is written over newer
work. Every save is a new version that `restore_version` can bring back."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from app.tools.executor import ToolFailure, handler, request_route
from app.tools.gatekeeper import Admission

IMG = "/api/v1/image-assets"
VID = "/api/v1/video-projects"


def _a(admission: Admission) -> dict[str, Any]:
    return admission.arguments.model_dump(mode="json", exclude_none=True)


def _id() -> str:
    return uuid4().hex[:12]


def _conflict(current: int, wanted: int, what: str) -> ToolFailure:
    return ToolFailure("version_conflict", f"This {what} has changed since you last looked (it is on version {current}, you edited version {wanted}). Look again, then repeat your change.", 409)


# ── pictures ─────────────────────────────────────────────────────────────

async def _slide(admission: Admission, a: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    asset = await request_route(admission.credential, "GET", f"{IMG}/{a['image_asset_id']}", None, None)
    if int(asset.get("version_count") or 1) != a["base_version"]:
        raise _conflict(int(asset.get("version_count") or 1), a["base_version"], "picture")
    slide = next((s for s in asset.get("slides", []) if s.get("slide_number") == a.get("slide_number", 1)), None)
    if slide is None:
        raise ToolFailure("upstream_404", "That slide was not found.", 404)
    layers = [dict(layer) for layer in slide.get("layers") or []]
    if not layers:
        raise ToolFailure("not_editable", "This picture has no editable design yet. Open it in Ekavo once, or make it again, then edit it here.", 409)
    return asset, slide, layers


def _named(layers: list[dict[str, Any]], name: str, kind: str) -> dict[str, Any] | None:
    return next((l for l in layers if l.get("type") == kind and l.get("name") == name), None)


async def _save(admission: Admission, a: dict[str, Any], layers: list[dict[str, Any]]) -> dict[str, Any]:
    saved = await request_route(admission.credential, "PUT", f"{IMG}/{a['image_asset_id']}/slides/{a.get('slide_number', 1)}/layers", None, {"layers": layers})
    return _picture_out(saved, a)


def _picture_out(asset: dict[str, Any], a: dict[str, Any]) -> dict[str, Any]:
    return {"image_asset_id": asset["id"], "slide_number": a.get("slide_number", 1), "version": int(asset.get("version_count") or 1),
            "href": f"/dashboard/pipelines/image?asset={asset['id']}"}


@handler("image.set_headline")
async def set_headline(admission: Admission) -> dict[str, Any]:
    a = _a(admission)
    _, _, layers = await _slide(admission, a)
    headline = _named(layers, "Headline", "text")
    if headline is None:
        raise ToolFailure("not_editable", "This picture has no headline to change.", 409)
    headline["text"] = a["text"]
    headline["accent_word"] = a.get("accent_word", "")
    return await _save(admission, a, layers)


@handler("image.set_colors")
async def set_colors(admission: Admission) -> dict[str, Any]:
    a = _a(admission)
    _, _, layers = await _slide(admission, a)
    headline, band, bar = _named(layers, "Headline", "text"), _named(layers, "Text band", "shape"), _named(layers, "Accent bar", "shape")
    if a.get("text_color") and headline:
        headline["color"] = a["text_color"]
    if a.get("accent_color"):
        if headline:
            headline["accent_color"] = a["accent_color"]
        if bar:
            bar["fill"] = a["accent_color"]
    if a.get("band_color") and band:
        band["fill"] = a["band_color"]
    if not any((a.get("text_color") and headline, a.get("accent_color") and (headline or bar), a.get("band_color") and band)):
        raise ToolFailure("not_editable", "This picture has none of the parts those colours belong to.", 409)
    return await _save(admission, a, layers)


@handler("image.set_logo_spot")
async def set_logo_spot(admission: Admission) -> dict[str, Any]:
    from app.pipelines.media import logo_placement

    a = _a(admission)
    asset, slide, layers = await _slide(admission, a)
    logo = next((l for l in layers if l.get("type") == "logo"), None)
    if logo is None:
        raise ToolFailure("not_editable", "This picture has no logo layer. Add the brand logo in Ekavo first.", 409)
    width, height = _canvas(slide)
    edge_w, edge_h = float(logo["w"]) * width, float(logo["h"]) * height
    band = _named(layers, "Text band", "shape")
    floor = int(float(band["y"]) * height) if band and a["position"].startswith("bottom") else None
    margin = (int(0.04 * width), int(0.04 * height))
    x, y = logo_placement.place(a["position"], (width, height), (int(edge_w), int(edge_h)), margin, floor)
    logo["x"], logo["y"] = x / width, y / height
    if a.get("backing"):
        logo["backing"] = a["backing"]
    return await _save(admission, a, layers)


def _canvas(slide: dict[str, Any]) -> tuple[int, int]:
    if slide.get("width") and slide.get("height"):
        return int(slide["width"]), int(slide["height"])
    from app.pipelines.media.image_render import LAYOUT_DIMS

    return LAYOUT_DIMS[slide["layout"]]


@handler("image.add_big_number")
async def add_big_number(admission: Admission) -> dict[str, Any]:
    a = _a(admission)
    _, slide, layers = await _slide(admission, a)
    layers.append({"id": _id(), "type": "text", "name": "Big number", "text": a["figure"], "x": 0.08, "y": 0.28, "w": 0.84, "size": 0.2, "bold": True, "color": "#FFFFFF", "shadow": True})
    if a.get("caption"):
        layers.append({"id": _id(), "type": "text", "name": "Big number caption", "text": a["caption"], "x": 0.08, "y": 0.5, "w": 0.84, "size": 0.05, "bold": False, "color": "#FFFFFF", "shadow": True})
    return await _save(admission, a, layers)


@handler("image.add_chart")
async def add_chart(admission: Admission) -> dict[str, Any]:
    a = _a(admission)
    _, _, layers = await _slide(admission, a)
    headline = _named(layers, "Headline", "text")
    accent = (headline or {}).get("accent_color") or "#38bdf8"
    layers.append({"id": _id(), "type": "chart", "name": a.get("title") or "Chart", "x": 0.08, "y": 0.22, "w": 0.84, "h": 0.4, "fill": accent, "color": "#FFFFFF",
                   "bars": [{"label": b["label"], "value": b["value"]} for b in a["bars"]]})
    return await _save(admission, a, layers)


@handler("image.apply_look")
async def apply_look(admission: Admission) -> dict[str, Any]:
    a = _a(admission)
    current = await request_route(admission.credential, "GET", f"{IMG}/{a['image_asset_id']}", None, None)
    if int(current.get("version_count") or 1) != a["base_version"]:
        raise _conflict(int(current.get("version_count") or 1), a["base_version"], "picture")
    effects = next((s.get("effects") or {} for s in current.get("slides", []) if s.get("slide_number") == a.get("slide_number", 1)), {})
    merged = {"glow": False, "dual_tone": False, "grid": False, "remove_background": False, "corner_radius": 0.0, **effects}
    merged.update({k: a[k] for k in ("glow", "dual_tone", "grid", "remove_background", "corner_radius") if k in a})
    saved = await request_route(admission.credential, "POST", f"{IMG}/{a['image_asset_id']}/slides/{a.get('slide_number', 1)}/looks", None, {"effects": merged})
    return _picture_out(saved, a)


@handler("image.reshape")
async def reshape(admission: Admission) -> dict[str, Any]:
    a = _a(admission)
    current = await request_route(admission.credential, "GET", f"{IMG}/{a['image_asset_id']}", None, None)
    if int(current.get("version_count") or 1) != a["base_version"]:
        raise _conflict(int(current.get("version_count") or 1), a["base_version"], "picture")
    body = {k: a[k] for k in ("layouts", "focus_x", "focus_y") if k in a}
    saved = await request_route(admission.credential, "POST", f"{IMG}/{a['image_asset_id']}/slides/{a.get('slide_number', 1)}/reshape", None, body)
    return _picture_out(saved, a)


@handler("image.reset")
async def reset(admission: Admission) -> dict[str, Any]:
    a = _a(admission)
    current = await request_route(admission.credential, "GET", f"{IMG}/{a['image_asset_id']}", None, None)
    if int(current.get("version_count") or 1) != a["base_version"]:
        raise _conflict(int(current.get("version_count") or 1), a["base_version"], "picture")
    saved = await request_route(admission.credential, "POST", f"{IMG}/{a['image_asset_id']}/slides/{a.get('slide_number', 1)}/layers/reset", None, None)
    return _picture_out(saved, a)


@handler("image.restore_version")
async def restore_image_version(admission: Admission) -> dict[str, Any]:
    a = _a(admission)
    current = await request_route(admission.credential, "GET", f"{IMG}/{a['image_asset_id']}", None, None)
    if int(current.get("version_count") or 1) != a["base_version"]:
        raise _conflict(int(current.get("version_count") or 1), a["base_version"], "picture")
    saved = await request_route(admission.credential, "POST", f"{IMG}/{a['image_asset_id']}/restore/{a['version_number']}", None, None)
    return _picture_out(saved, {"slide_number": 1})


# ── videos ───────────────────────────────────────────────────────────────

async def _open(admission: Admission, a: dict[str, Any]) -> tuple[dict[str, Any], int]:
    project = await request_route(admission.credential, "GET", f"{VID}/{a['project_id']}", None, None)
    version = int(project.get("version") or 1)
    if version != a["base_version"]:
        raise _conflict(version, a["base_version"], "video")
    return project["edit"], version


async def _put(admission: Admission, a: dict[str, Any], edit: dict[str, Any]) -> dict[str, Any]:
    saved = await request_route(admission.credential, "PUT", f"{VID}/{a['project_id']}", None, {"edit": edit, "base_version": a["base_version"]})
    return {"project_id": a["project_id"], "version": int(saved.get("version") or a["base_version"] + 1), "href": f"/dashboard/pipelines/video?project={a['project_id']}"}


def _frames(edit: dict[str, Any], seconds: float) -> int:
    from app.video import edl

    return edl.seconds_to_frames(seconds, edit["fps"])


def _clip(edit: dict[str, Any], clip_id: str, tracks: tuple[str, ...] = ("video", "audio")) -> dict[str, Any]:
    for track in tracks:
        for clip in edit.get(track, []):
            if clip.get("id") == clip_id:
                return clip
    raise ToolFailure("upstream_404", f"There is no clip {clip_id!r} in this project.", 404)


@handler("video.trim_clip")
async def trim_clip(admission: Admission) -> dict[str, Any]:
    a = _a(admission)
    edit, _ = await _open(admission, a)
    clip = _clip(edit, a["clip_id"])
    clip["src_in_f"], clip["dur_f"] = _frames(edit, a["src_in_s"]), max(1, _frames(edit, a["duration_s"]))
    clip["fade_in_f"] = min(int(clip.get("fade_in_f") or 0), clip["dur_f"])
    clip["fade_out_f"] = min(int(clip.get("fade_out_f") or 0), clip["dur_f"] - clip["fade_in_f"])
    if clip.get("transition_in", {}).get("dur_f", 0) > clip["dur_f"]:
        clip["transition_in"] = {"kind": "none", "dur_f": 0}
    return await _put(admission, a, edit)


@handler("video.reorder_clips")
async def reorder_clips(admission: Admission) -> dict[str, Any]:
    a = _a(admission)
    edit, _ = await _open(admission, a)
    clips = {c["id"]: c for c in edit.get("video", [])}
    if set(a["clip_ids"]) != set(clips):
        raise ToolFailure("input_invalid", "Name every video clip in the project once, in the order you want.", 422)
    position = min((c["at_f"] for c in clips.values()), default=0)
    for clip_id in a["clip_ids"]:
        clips[clip_id]["at_f"] = position
        position += clips[clip_id]["dur_f"]
    edit["video"] = [clips[i] for i in a["clip_ids"]]
    return await _put(admission, a, edit)


@handler("video.add_text")
async def add_text(admission: Admission) -> dict[str, Any]:
    a = _a(admission)
    edit, _ = await _open(admission, a)
    item = {"id": _id(), "text": a["text"], "at_f": _frames(edit, a["start_s"]), "dur_f": max(1, _frames(edit, a["duration_s"])), "x": a.get("x", 0.5), "y": a.get("y", 0.5),
            "size": a.get("size", 64), "color": a.get("color", "#FFFFFF"), "bold": a.get("bold", True), "anim_in": a.get("anim_in", "none"), "anim_out": a.get("anim_out", "none")}
    if a.get("font"):
        item["font"] = a["font"]
    edit.setdefault("text", []).append(item)
    return await _put(admission, a, edit)


@handler("video.set_captions_style")
async def set_captions_style(admission: Admission) -> dict[str, Any]:
    a = _a(admission)
    edit, _ = await _open(admission, a)
    captions = edit.setdefault("captions", {})
    for key in ("enabled", "font", "size", "color", "accent", "outline", "shadow", "box", "y", "per_line", "upper", "highlight", "emoji"):
        if key in a:
            captions[key] = a[key]
    return await _put(admission, a, edit)


@handler("video.set_transition")
async def set_transition(admission: Admission) -> dict[str, Any]:
    a = _a(admission)
    edit, _ = await _open(admission, a)
    clip = _clip(edit, a["clip_id"], ("video",))
    clip["transition_in"] = {"kind": "none", "dur_f": 0} if a["kind"] == "none" else {"kind": a["kind"], "dur_f": max(1, _frames(edit, a.get("duration_s", 0.5)))}
    return await _put(admission, a, edit)


@handler("video.add_music")
async def add_music(admission: Admission) -> dict[str, Any]:
    a = _a(admission)
    edit, _ = await _open(admission, a)
    start = _frames(edit, a.get("start_s", 0))
    total = int(edit.get("duration_f") or 0)
    dur = _frames(edit, a["duration_s"]) if a.get("duration_s") else max(1, total - start)
    edit.setdefault("audio", []).append({"id": _id(), "media_id": a["media_id"], "at_f": start, "dur_f": max(1, dur), "src_in_f": 0, "speed": 1.0,
                                         "gain_db": a.get("gain_db", -12), "role": "music", "duck": a.get("duck", True), "duck_amount": a.get("duck_amount", "medium")})
    return await _put(admission, a, edit)


@handler("video.set_look")
async def set_look(admission: Admission) -> dict[str, Any]:
    a = _a(admission)
    edit, _ = await _open(admission, a)
    clip = _clip(edit, a["clip_id"], ("video",))
    look = dict(clip.get("look") or {"brightness": 0.0, "contrast": 1.0, "saturation": 1.0})
    look.update({k: a[k] for k in ("brightness", "contrast", "saturation") if k in a})
    clip["look"] = look
    return await _put(admission, a, edit)


@handler("video.set_canvas")
async def set_canvas(admission: Admission) -> dict[str, Any]:
    from app.video import edl

    a = _a(admission)
    edit, _ = await _open(admission, a)
    edit["width"], edit["height"] = edl.CANVAS_SIZES[a["size"]]
    return await _put(admission, a, edit)


@handler("video.add_bleeps")
async def add_bleeps(admission: Admission) -> dict[str, Any]:
    a = _a(admission)
    edit, _ = await _open(admission, a)
    edit["bleeps"] = [*edit.get("bleeps", []), *({"s_ms": s["start_ms"], "e_ms": s["end_ms"]} for s in a["stretches"])]
    return await _put(admission, a, edit)


@handler("video.set_emphasis")
async def set_emphasis(admission: Admission) -> dict[str, Any]:
    a = _a(admission)
    edit, _ = await _open(admission, a)
    edit.setdefault("captions", {})["emphasis"] = [{"s_ms": s["start_ms"], "e_ms": s["end_ms"]} for s in a.get("stretches", [])]
    return await _put(admission, a, edit)


@handler("video.restore_version")
async def restore_video_version(admission: Admission) -> dict[str, Any]:
    a = _a(admission)
    await _open(admission, a)
    saved = await request_route(admission.credential, "POST", f"{VID}/{a['project_id']}/versions/{a['version']}/restore", None, None)
    return {"project_id": a["project_id"], "version": int(saved.get("version") or a["base_version"] + 1), "href": f"/dashboard/pipelines/video?project={a['project_id']}"}
