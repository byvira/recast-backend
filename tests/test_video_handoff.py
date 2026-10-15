"""Hand-off to editing programs: one timeline structure, three formats that agree with it, captions and markers, the package, and the route.
Timecodes, frame math, track assignment and well-formedness are checked here; opening the files in an editing program is done by editors."""
import io
import json
import zipfile
from uuid import uuid4
from xml.etree import ElementTree as ET

import pytest

from app.db.mongo import get_db
from app.video.handoff import formats, package
from app.video.handoff.timeline import Event, build, original_name, timecode

UPLOAD = "https://res.cloudinary.com/x/video/upload/v1/recast/video_source/ws/3f2b1c9e-1111-4222-8333-444455556666_My%20Talk.mp4"
MEDIA = {"m1": {"id": "m1", "url": UPLOAD}, "m2": {"id": "m2", "url": "https://res.cloudinary.com/x/video/upload/v1/abc.mp3"}}


def _edit(**over):
    e = {
        "fps": {"num": 30, "den": 1}, "width": 1920, "height": 1080, "duration_f": 600, "tracks": {},
        "video": [{"id": "a", "media_id": "m1", "at_f": 0, "dur_f": 300, "src_in_f": 90, "speed": 1.0, "audio": True, "gain_db": 0},
                  {"id": "b", "media_id": "m1", "at_f": 300, "dur_f": 150, "src_in_f": 600, "speed": 2.0, "audio": False}],
        "audio": [{"id": "c", "media_id": "m2", "at_f": 0, "dur_f": 600, "src_in_f": 0, "speed": 1.0, "role": "music", "gain_db": -12}],
        "markers": [{"id": "k", "at_f": 150, "label": "Hook"}],
        "captions": {"words": [{"w": "Hello", "s_ms": 0, "e_ms": 400}, {"w": "there.", "s_ms": 450, "e_ms": 900}, {"w": "Next", "s_ms": 1000, "e_ms": 1300}]},
    }
    e.update(over)
    return e


def test_the_original_file_name_is_recovered_from_our_storage_name():
    assert original_name(MEDIA["m1"], "m1") == "My Talk.mp4"
    assert original_name(None, "abcd1234") == "abcd1234.mp4"
    assert original_name({"url": "https://x/y/plain.mov"}, "z") == "plain.mov"


def test_timecode_counts_whole_frames_and_starts_where_asked():
    assert timecode(0, 30) == "00:00:00:00"
    assert timecode(45, 30) == "00:00:01:15"
    assert timecode(0, 30, start_hour=1) == "01:00:00:00"
    assert timecode(30 * 3600 + 5, 30) == "01:00:00:05"
    assert timecode(10, 25) == "00:00:00:10" and timecode(26, 25) == "00:00:01:01"


def test_the_timeline_keeps_every_clip_its_place_its_source_in_point_and_its_speed():
    tl = build("My talk", _edit(), MEDIA)

    video = [e for e in tl.events if e.kind == "video"]
    assert [(e.at_f, e.dur_f, e.src_in_f, e.speed, e.track) for e in video] == [(0, 300, 90, 1.0, 1), (300, 150, 600, 2.0, 1)]
    assert {e.name for e in video} == {"My Talk.mp4"}
    sound = [e for e in tl.events if e.kind == "audio"]
    assert [(e.role, e.track) for e in sound] == [("clip", 1), ("music", 2)]        # the clip's own sound first, then the music on the next track
    assert tl.markers == [{"at_f": 150, "label": "Hook"}] and tl.duration_f == 600


def test_overlapping_clips_go_to_different_tracks_and_a_hidden_track_is_left_out():
    overlap = _edit(video=[{"id": "a", "media_id": "m1", "at_f": 0, "dur_f": 200}, {"id": "b", "media_id": "m1", "at_f": 100, "dur_f": 200}])
    assert [e.track for e in build("t", overlap, MEDIA).events if e.kind == "video"] == [1, 2]
    hidden = _edit(tracks={"hide": ["audio"]})
    assert not [e for e in build("t", hidden, MEDIA).events if e.role == "music"]


def test_the_edl_has_the_right_header_times_speed_change_and_clip_names():
    text = formats.to_cmx3600(build("My talk", _edit(), MEDIA))

    lines = text.splitlines()
    assert lines[0] == "TITLE: My talk" and lines[1] == "FCM: NON-DROP FRAME"
    first = next(l for l in lines if l.startswith("001"))
    assert first.split()[-4:] == ["00:00:03:00", "00:00:13:00", "01:00:00:00", "01:00:10:00"] and " V " in first
    second = next(l for l in lines if l.startswith("002"))
    assert second.split()[-4:] == ["00:00:20:00", "00:00:30:00", "01:00:10:00", "01:00:15:00"]          # 150 frames at 2x uses 300 source frames
    assert any(l.startswith("M2") and "60.0" in l for l in lines)                                      # the speed change line: 30 fps x 2
    assert "* FROM CLIP NAME: My Talk.mp4" in lines and "* LOC: 01:00:05:00 RED     Hook" in lines
    assert any(l.split()[2] == "A" for l in lines if l[:3].isdigit()) and any(l.split()[2] == "A2" for l in lines if l[:3].isdigit())


def test_the_premiere_xml_is_well_formed_and_agrees_with_the_timeline():
    root = ET.fromstring(formats.to_fcp7_xml(build("My talk", _edit(), MEDIA)).split("\n", 2)[2])

    seq = root.find("sequence")
    assert seq.findtext("name") == "My talk" and seq.findtext("duration") == "600" and seq.findtext("rate/timebase") == "30"
    assert seq.findtext("media/video/format/samplecharacteristics/width") == "1920"
    v = root.findall("sequence/media/video/track/clipitem")
    assert [(c.findtext("start"), c.findtext("end"), c.findtext("in"), c.findtext("out")) for c in v] == [("0", "300", "90", "390"), ("300", "450", "600", "900")]
    assert v[1].findtext("filter/effect/parameter/value") == "200.0"                                    # a 2x speed change
    files = [f for f in root.iter("file") if f.findtext("name")]
    assert len(files) == 2 and {f.findtext("name") for f in files} == {"My Talk.mp4", "abc.mp3"}      # one full file entry per source, the rest refer to it
    assert seq.find("marker/name").text == "Hook" and seq.findtext("marker/in") == "150"
    assert len(root.findall("sequence/media/audio/track")) == 2


def test_the_final_cut_xml_is_well_formed_with_exact_rational_times():
    text = formats.to_fcpxml(build("My talk", _edit(), MEDIA))
    root = ET.fromstring(text.split("\n", 2)[2])

    assert root.get("version") == "1.10"
    assert root.find("resources/format").get("frameDuration") == "1/30s"
    seq = root.find("library/event/project/sequence")
    assert seq.get("duration") == "600/30s" and seq.get("tcStart") == "108000/30s"
    clips = root.findall("library/event/project/sequence/spine/gap/asset-clip")
    first = clips[0]
    assert first.get("offset") == "0/30s" and first.get("start") == "90/30s" and first.get("duration") == "300/30s" and first.get("lane") == "1"
    assert any(c.get("lane") == "-2" and c.get("audioRole") == "music" for c in clips)
    assert root.find("library/event/project/sequence/spine/gap/marker").get("value") == "Hook"


def test_an_ntsc_project_is_flagged_and_counted_at_the_whole_frame_rate():
    edit = _edit(fps={"num": 30000, "den": 1001})
    tl = build("ntsc", edit, MEDIA)
    assert tl.ntsc and tl.timebase == 30
    assert ET.fromstring(formats.to_fcp7_xml(tl).split("\n", 2)[2]).findtext("sequence/rate/ntsc") == "TRUE"
    assert 'frameDuration="1001/30000s"' in formats.to_fcpxml(tl)


def test_captions_become_readable_subtitle_files_cut_at_full_stops():
    words = _edit()["captions"]["words"]
    srt = formats.to_srt(words)
    assert srt.startswith("1\n00:00:00,000 --> 00:00:00,900\nHello there.\n\n2\n00:00:01,000 --> 00:00:01,300\nNext\n")
    vtt = formats.to_vtt(words)
    assert vtt.startswith("WEBVTT\n\n00:00:00.000 --> 00:00:00.900\nHello there.")
    long = [{"w": f"w{i}", "s_ms": i * 100, "e_ms": i * 100 + 90} for i in range(20)]
    assert all(len(t.split()) <= 7 for _, _, t in formats.caption_cues(long))


def test_markers_come_as_a_sheet_with_timecodes():
    csv = formats.to_markers_csv(build("t", _edit(), MEDIA))
    assert csv == 'timecode,frame,label\n01:00:05:00,150,"Hook"\n'


def test_the_package_holds_every_file_a_manifest_by_original_name_and_says_what_is_not_carried():
    tl = build("My talk", _edit(), MEDIA)
    data = package.build_zip(tl, _edit()["captions"]["words"], "Hello there. Next")

    z = zipfile.ZipFile(io.BytesIO(data))
    names = sorted(n.split("/", 1)[1] for n in z.namelist())
    assert names == ["README.txt", "captions.srt", "captions.vtt", "manifest.json", "markers.csv", "timeline.edl", "timeline.otio",
                     "timeline_finalcut.fcpxml", "timeline_premiere.xml", "transcript.txt"]
    manifest = json.loads(z.read("My_talk/manifest.json"))
    assert {s["file"] for s in manifest["sources"]} == {"My Talk.mp4", "abc.mp3"} and manifest["frame_rate"] == "30/1"
    readme = z.read("My_talk/README.txt").decode()
    assert "01:00:00:00" in readme and "1920 x 1080" in readme and "Not carried across" in readme


# ── the route ────────────────────────────────────────────────────────────────────

async def _project_with_media(ws: str, client) -> str:
    from tests.test_video_projects import _media

    mid = await _media(ws, duration=20.0)
    r = await client.post("/api/v1/video-projects", headers={"X-Workspace-Id": ws}, json={"source": "video", "media_id": mid, "title": "Route talk"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_each_format_downloads_with_the_right_type_and_a_project_without_clips_or_captions_says_so(signup_user):
    from tests.conftest import create_workspace

    client, _ = await signup_user()
    ws = await create_workspace(client, f"Handoff {uuid4().hex[:6]}")
    pid = await _project_with_media(ws, client)
    h = {"X-Workspace-Id": ws}

    edl_resp = await client.get(f"/api/v1/video-projects/{pid}/handoff/edl", headers=h)
    assert edl_resp.status_code == 200 and edl_resp.text.startswith("TITLE: Route talk") and "timeline.edl" in edl_resp.headers["content-disposition"]
    xml = await client.get(f"/api/v1/video-projects/{pid}/handoff/premiere", headers=h)
    assert xml.status_code == 200 and "<xmeml" in xml.text and xml.headers["content-type"].startswith("application/xml")
    zipped = await client.get(f"/api/v1/video-projects/{pid}/handoff/package", headers=h)
    assert zipped.status_code == 200 and zipfile.ZipFile(io.BytesIO(zipped.content)).testzip() is None
    assert (await client.get(f"/api/v1/video-projects/{pid}/handoff/srt", headers=h)).status_code == 409      # no captions yet
    assert (await client.get(f"/api/v1/video-projects/{pid}/handoff/nonsense", headers=h)).status_code == 404
    other = await create_workspace(client, f"Other {uuid4().hex[:6]}")
    assert (await client.get(f"/api/v1/video-projects/{pid}/handoff/edl", headers={"X-Workspace-Id": other})).status_code == 404
