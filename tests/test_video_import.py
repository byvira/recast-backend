"""Reading a timeline back in: an EDL, Final Cut Pro 7 XML or OpenTimelineIO file. What the exporters wrote must read back as the same
clips (a round trip is stable), names are matched to the library and never guessed, and applying is one undoable version."""
import io
import json
from uuid import uuid4

import pytest

from app.db.mongo import get_db
from app.video.handoff import formats, importer
from app.video.handoff.timeline import build
from tests.test_video_handoff import MEDIA, UPLOAD, _edit


def _shape(tl):
    return sorted((e.kind, e.track, e.at_f, e.dur_f, e.src_in_f, round(e.speed, 3), e.name) for e in tl.events)


@pytest.mark.parametrize("writer,reader", [
    (formats.to_cmx3600, lambda t: importer.parse_edl(t, 30)),
    (formats.to_fcp7_xml, importer.parse_fcp7),
    (importer.to_otio, importer.parse_otio),
])
def test_what_each_exporter_writes_reads_back_as_the_same_clips_and_markers(writer, reader):
    original = build("My talk", _edit(), MEDIA)

    again = reader(writer(original))

    assert _shape(again) == _shape(original)
    assert again.markers == original.markers and again.duration_f == original.duration_f


def test_reading_then_writing_again_is_stable():
    first = importer.parse_edl(formats.to_cmx3600(build("t", _edit(), MEDIA)), 30)
    second = importer.parse_edl(formats.to_cmx3600(first), 30)
    assert _shape(first) == _shape(second)


def test_the_file_kind_is_found_from_its_content_and_a_final_cut_x_file_is_refused_with_advice():
    tl = build("t", _edit(), MEDIA)
    assert importer.parse("whatever.txt", formats.to_cmx3600(tl), 30).events
    assert importer.parse("x.xml", formats.to_fcp7_xml(tl), 30).events
    assert importer.parse("x.otio", importer.to_otio(tl), 30).events
    with pytest.raises(importer.ImportError_, match="Final Cut Pro X"):
        importer.parse("x.xml", formats.to_fcpxml(tl), 30)
    with pytest.raises(importer.ImportError_):
        importer.parse("x.edl", "nothing useful here", 30)
    with pytest.raises(importer.ImportError_, match="not valid"):
        importer.parse("x.xml", "<xmeml><sequence>", 30)
    with pytest.raises(importer.ImportError_, match="OpenTimelineIO"):
        importer.parse("x.otio", json.dumps({"OTIO_SCHEMA": "Other.1"}), 30)


def test_a_time_that_is_not_a_timecode_is_refused_in_plain_words():
    with pytest.raises(importer.ImportError_):
        importer._tc_to_frames("1:2:3", 30)
    assert importer._tc_to_frames("00:00:01:15", 30) == 45


def test_names_are_matched_to_the_library_by_original_name_and_two_files_with_one_name_are_not_guessed():
    tl = build("t", _edit(), MEDIA)
    library = {"lib1": {"id": "lib1", "url": UPLOAD}, "lib2": {"id": "lib2", "url": "https://x/abc.mp3"}}
    matched, missing = importer.match_clips(tl, library)
    assert matched == {"My Talk.mp4": "lib1", "abc.mp3": "lib2"} and missing == []

    twin = {**library, "lib3": {"id": "lib3", "url": "https://x/other/My%20Talk.mp4"}}
    matched, missing = importer.match_clips(tl, twin)
    assert "My Talk.mp4" not in matched and missing == ["My Talk.mp4"]


def test_matched_clips_become_edit_clips_and_unmatched_ones_are_left_out():
    tl = build("t", _edit(), MEDIA)
    parts = importer.to_edit_parts(tl, {"My Talk.mp4": "lib1"})
    assert [(c["media_id"], c["at_f"], c["dur_f"], c["src_in_f"], c["speed"], c["audio"]) for c in parts["video"]] == [
        ("lib1", 0, 300, 90, 1.0, True), ("lib1", 300, 150, 600, 2.0, False)]
    assert parts["audio"] == [] and parts["markers"][0]["label"] == "Hook" and parts["duration_f"] == 450


# ── the route ────────────────────────────────────────────────────────────────────

async def _setup(client, ws):
    from tests.test_video_projects import _media

    mid = await _media(ws, duration=20.0)
    await get_db()["media_assets"].update_one(
        {"id": mid}, {"$set": {"url": "https://res.cloudinary.com/x/video/upload/v1/recast/video_source/ws/3f2b1c9e-1111-4222-8333-444455556666_Interview.mp4"}})
    r = await client.post("/api/v1/video-projects", headers={"X-Workspace-Id": ws}, json={"source": "video", "media_id": mid, "title": "Import me"})
    assert r.status_code == 201, r.text
    return r.json()["id"], mid


EDL = """TITLE: From Premiere
FCM: NON-DROP FRAME

001  AX       V     C        00:00:02:00 00:00:07:00 01:00:00:00 01:00:05:00
* FROM CLIP NAME: Interview.mp4
002  AX       V     C        00:00:10:00 00:00:12:00 01:00:05:00 01:00:07:00
* FROM CLIP NAME: Missing File.mov
* LOC: 01:00:03:00 RED     Beat
"""


def _file(text: str, name: str = "cut.edl"):
    return {"file": (name, io.BytesIO(text.encode()), "text/plain")}


async def test_a_preview_changes_nothing_and_applying_replaces_the_clips_as_one_version(signup_user):
    from tests.conftest import create_workspace

    client, _ = await signup_user()
    ws = await create_workspace(client, f"Import {uuid4().hex[:6]}")
    h = {"X-Workspace-Id": ws}
    pid, mid = await _setup(client, ws)
    before = (await client.get(f"/api/v1/video-projects/{pid}", headers=h)).json()

    preview = await client.post(f"/api/v1/video-projects/{pid}/import-timeline", headers=h, files=_file(EDL))
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["clips"] == 2 and body["matched"] == 1 and body["unmatched"] == ["Missing File.mov"] and body["applied"] is False and body["length"] == "00:00:07:00"
    same = (await client.get(f"/api/v1/video-projects/{pid}", headers=h)).json()
    assert same["version"] == before["version"]

    applied = await client.post(f"/api/v1/video-projects/{pid}/import-timeline?apply=true", headers=h, files=_file(EDL))
    assert applied.status_code == 200, applied.text
    assert applied.json()["applied"] is True
    after = (await client.get(f"/api/v1/video-projects/{pid}", headers=h)).json()
    clip = after["edit"]["video"][0]
    assert after["version"] == before["version"] + 1 and len(after["edit"]["video"]) == 1
    assert (clip["media_id"], clip["at_f"], clip["dur_f"], clip["src_in_f"]) == (mid, 0, 150, 60)
    assert after["edit"]["markers"][0]["label"] == "Beat" and after["edit"]["markers"][0]["at_f"] == 90

    restored = await client.post(f"/api/v1/video-projects/{pid}/versions/{before['version']}/restore", headers=h)
    assert restored.status_code == 200 and restored.json()["edit"]["video"][0]["dur_f"] == before["edit"]["video"][0]["dur_f"]


async def test_nothing_matching_is_refused_and_bad_files_say_why(signup_user):
    from tests.conftest import create_workspace

    client, _ = await signup_user()
    ws = await create_workspace(client, f"Import {uuid4().hex[:6]}")
    h = {"X-Workspace-Id": ws}
    pid, _ = await _setup(client, ws)
    url = f"/api/v1/video-projects/{pid}/import-timeline"

    refused = await client.post(url + "?apply=true", headers=h, files=_file(EDL.replace("Interview.mp4", "Nope.mp4")))
    assert refused.status_code == 409 and "match a file in your library" in refused.json()["detail"]
    junk = await client.post(url, headers=h, files=_file("hello", "a.edl"))
    assert junk.status_code == 400 and "No clips" in junk.json()["detail"]
    binary = await client.post(url, headers=h, files={"file": ("a.mov", io.BytesIO(bytes(range(200, 256))), "video/quicktime")})
    assert binary.status_code == 400 and "not a text timeline" in binary.json()["detail"]
    huge = await client.post(url, headers=h, files=_file("x" * (5 * 1024 * 1024 + 10), "a.edl"))
    assert huge.status_code == 413
