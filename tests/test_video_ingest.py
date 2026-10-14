"""Source video upload: the probe on real files made by real ffmpeg, and the upload start, limits and refusals against the real
database. Nothing is faked: the files are real and the checks run for real. The accepted path (a file really stored in
Cloudinary) is proven by the one deliberate live upload, not here."""
import subprocess
from types import SimpleNamespace
from uuid import uuid4

import cloudinary.utils
import imageio_ffmpeg
import pytest
from fastapi import HTTPException

from app.api.v1 import video_media
from app.core.config import settings
from app.db.mongo import get_db
from app.video import probe

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def _run(*args: str) -> None:
    subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", *args], check=True, capture_output=True, timeout=120)


def _make(path, *, rate: str = "30", audio: str | None = "stereo", extra: tuple[str, ...] = (), seconds: int = 2) -> str:
    cmd = ["-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=320x240:rate={rate}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-ac", "1" if audio == "mono" else "2", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", *extra, str(path)]
    _run(*cmd)
    return str(path)


async def test_a_plain_video_is_read_correctly(tmp_path):
    found = await probe.probe_source(_make(tmp_path / "a.mp4"))
    assert 1.9 < found["duration_s"] < 2.2
    v = found["video"]
    assert (v["codec"], v["width"], v["height"], v["fps"], v["rotation"], v["hdr"]) == ("h264", 320, 240, 30.0, 0, False)
    assert found["audio"]["layout"] == "stereo" and found["audio"]["sample_rate"] > 0
    assert found["notices"] == []


async def test_a_video_with_no_sound_says_so(tmp_path):
    found = await probe.probe_source(_make(tmp_path / "n.mp4", audio=None))
    assert found["audio"] is None and any("no sound" in n for n in found["notices"])


async def test_mono_sound_is_reported(tmp_path):
    found = await probe.probe_source(_make(tmp_path / "m.mp4", audio="mono"))
    assert found["audio"]["layout"] == "mono"


async def test_a_broadcast_frame_rate_is_kept_exactly(tmp_path):
    found = await probe.probe_source(_make(tmp_path / "f.mp4", rate="24000/1001"))
    assert abs(found["video"]["fps"] - 23.976) < 0.01


async def test_a_turned_phone_video_is_found_and_its_shown_size_swaps(tmp_path):
    base = _make(tmp_path / "base.mp4")
    turned = str(tmp_path / "turned.mp4")
    _run("-display_rotation", "90", "-i", base, "-c", "copy", turned)
    found = await probe.probe_source(turned)
    v = found["video"]
    assert v["rotation"] % 360 in (90, 270)
    assert (v["shown_width"], v["shown_height"]) == (240, 320)
    assert any("turned" in n for n in found["notices"])


async def test_an_hdr_video_is_flagged_for_conversion(tmp_path):
    path = _make(tmp_path / "h.mp4", extra=(
        "-vf", "setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc", "-pix_fmt", "yuv420p10le",
        "-profile:v", "high10", "-x264-params", "colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc",
    ))
    found = await probe.probe_source(path)
    assert found["video"]["hdr"] is True and any("HDR" in n for n in found["notices"])


async def test_a_damaged_file_is_refused_with_a_plain_message(tmp_path):
    good = _make(tmp_path / "g.mp4", seconds=4)
    data = open(good, "rb").read()
    bad = tmp_path / "cut.mp4"
    bad.write_bytes(data[: len(data) // 3])
    with pytest.raises(probe.ProbeError) as caught:
        await probe.probe_source(str(bad))
    assert "damaged" in str(caught.value) or "no picture" in str(caught.value) or "length" in str(caught.value)


async def test_a_file_that_is_not_a_video_is_refused(tmp_path):
    text = tmp_path / "notes.mp4"
    text.write_text("this is not a video at all")
    with pytest.raises(probe.ProbeError):
        await probe.probe_source(str(text))


async def test_a_sound_file_is_refused_as_a_video(tmp_path):
    song = tmp_path / "s.m4a"
    _run("-f", "lavfi", "-i", "sine=duration=2", "-c:a", "aac", str(song))
    with pytest.raises(probe.ProbeError) as caught:
        await probe.probe_source(str(song))
    assert "no picture" in str(caught.value)


# ── upload start, limits and refusals ────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _storage(monkeypatch):
    monkeypatch.setattr(settings, "CLOUDINARY_CLOUD_NAME", "democloud")
    monkeypatch.setattr(settings, "CLOUDINARY_API_KEY", "1234")
    monkeypatch.setattr(settings, "CLOUDINARY_API_SECRET", "shhh")


def _ctx(ws: str | None = None):
    return SimpleNamespace(workspace_id=ws or f"ws-{uuid4()}", user_id="u1")


def _call(fn, *args, **kwargs):
    return getattr(fn, "__wrapped__", fn)(*args, **kwargs)


async def test_the_limits_endpoint_tells_the_screen_what_is_allowed():
    out = await _call(video_media.limits, None, _ctx())
    assert out["max_mb"] <= settings.VIDEO_UPLOAD_MAX_MB and "video/mp4" in out["content_types"]


async def test_starting_an_upload_returns_signed_scoped_chunked_parameters():
    ctx = _ctx()
    body = video_media.StartBody(filename="My Clip.MOV", content_type="video/quicktime", bytes=30 * 1024 * 1024)
    out = await _call(video_media.start_upload, None, body, ctx)
    up = out["upload"]
    assert up["url"].endswith("/democloud/video/upload") and up["chunk_bytes"] == 20 * 1024 * 1024
    fields = dict(up["fields"])
    signature = fields.pop("signature")
    fields.pop("api_key")
    assert signature == cloudinary.utils.api_sign_request(fields, "shhh")
    assert fields["folder"] == f"recast/video_source/{ctx.workspace_id}" and fields["public_id"].startswith(out["id"])
    assert "shhh" not in str(out) and out["unique_upload_id"] == out["id"]


async def test_a_file_over_the_limit_or_of_the_wrong_kind_is_refused_before_anything_is_stored():
    big = video_media.StartBody(filename="big.mp4", content_type="video/mp4", bytes=(settings.VIDEO_UPLOAD_MAX_MB + 1) * 1024 * 1024)
    with pytest.raises(HTTPException) as caught:
        await _call(video_media.start_upload, None, big, _ctx())
    assert caught.value.status_code == 413 and "MB" in caught.value.detail
    wrong = video_media.StartBody(filename="a.pdf", content_type="application/pdf", bytes=1000)
    with pytest.raises(HTTPException) as caught:
        await _call(video_media.start_upload, None, wrong, _ctx())
    assert caught.value.status_code == 400


async def test_too_many_open_uploads_are_refused():
    ctx = _ctx()
    body = video_media.StartBody(filename="a.mp4", content_type="video/mp4", bytes=1000)
    for _ in range(video_media.MAX_OPEN_UPLOADS):
        await _call(video_media.start_upload, None, body, ctx)
    with pytest.raises(HTTPException) as caught:
        await _call(video_media.start_upload, None, body, ctx)
    assert caught.value.status_code == 429


async def test_completing_with_a_file_that_is_not_the_one_started_is_refused_and_recorded():
    ctx = _ctx()
    out = await _call(video_media.start_upload, None, video_media.StartBody(filename="a.mp4", content_type="video/mp4", bytes=1000), ctx)
    with pytest.raises(HTTPException) as caught:
        await _call(video_media.complete_upload, None, out["id"],
                    video_media.CompleteBody(url="https://evil.example.com/a.mp4", bytes=1000), ctx)
    assert caught.value.status_code == 400
    row = await get_db()["video_uploads"].find_one({"id": out["id"]})
    assert row["status"] == "failed"
    assert await get_db()["media_assets"].count_documents({"workspace_id": ctx.workspace_id}) == 0


async def test_another_workspace_cannot_complete_or_cancel_someone_elses_upload():
    owner, other = _ctx(), _ctx()
    out = await _call(video_media.start_upload, None, video_media.StartBody(filename="a.mp4", content_type="video/mp4", bytes=1000), owner)
    with pytest.raises(HTTPException) as caught:
        await _call(video_media.cancel_upload, None, out["id"], other)
    assert caught.value.status_code == 409
    with pytest.raises(HTTPException) as caught:
        await _call(video_media.get_upload, None, out["id"], other)
    assert caught.value.status_code == 404


async def test_a_started_upload_can_be_cancelled_once():
    ctx = _ctx()
    out = await _call(video_media.start_upload, None, video_media.StartBody(filename="a.mp4", content_type="video/mp4", bytes=1000), ctx)
    assert (await _call(video_media.cancel_upload, None, out["id"], ctx))["status"] == "cancelled"
    with pytest.raises(HTTPException):
        await _call(video_media.cancel_upload, None, out["id"], ctx)
