"""Reading a stored video: the backend fetches it and reads it from disk, and a file that could not be opened is told apart from one with no
picture. Real files made with the bundled ffmpeg, served from a small local web server."""
import functools
import http.server
import os
import subprocess
import tempfile
import threading

import imageio_ffmpeg
import pytest

from app.video import probe


def _make(path, with_video=True, seconds=2):
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    base = [exe, "-y", "-hide_banner", "-loglevel", "error"]
    if with_video:
        cmd = base + ["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30", "-f", "lavfi", "-i", "sine=frequency=440", "-t", str(seconds),
                      "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", path]
    else:
        cmd = base + ["-f", "lavfi", "-i", "sine=frequency=440", "-t", str(seconds), "-c:a", "aac", path]
    subprocess.run(cmd, check=True)


@pytest.fixture()
def server():
    folder = tempfile.mkdtemp()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=folder)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield folder, f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def _leftovers():
    return {f for f in os.listdir(tempfile.gettempdir()) if f.endswith(".video")}


async def test_a_stored_video_is_fetched_read_and_the_temporary_copy_is_removed(server):
    folder, base = server
    _make(os.path.join(folder, "ok.mp4"))
    before = _leftovers()
    found = await probe.probe_source(f"{base}/ok.mp4")
    assert found["video"]["width"] == 640 and found["video"]["height"] == 360 and 1.5 < found["duration_s"] < 2.5
    assert found["audio"] is not None
    assert _leftovers() == before


async def test_a_file_with_sound_only_is_said_to_have_no_picture(server):
    folder, base = server
    _make(os.path.join(folder, "sound.m4a"), with_video=False)
    with pytest.raises(probe.ProbeError, match="no picture"):
        await probe.probe_source(f"{base}/sound.m4a")


async def test_a_file_too_large_to_fetch_is_read_over_the_link_instead(server, monkeypatch):
    folder, base = server
    _make(os.path.join(folder, "big.mp4"))
    monkeypatch.setattr(probe, "FETCH_CAP_BYTES", 1000)
    seen = []

    async def spy(source):
        seen.append(source)
        return {"read": source}

    monkeypatch.setattr(probe, "_probe_command", spy)
    before = _leftovers()
    await probe.probe_source(f"{base}/big.mp4")
    assert seen == [f"{base}/big.mp4"]  # the link itself, not a copy on disk
    assert _leftovers() == before


async def test_a_file_small_enough_is_read_from_a_copy_on_disk(server, monkeypatch):
    folder, base = server
    _make(os.path.join(folder, "small.mp4"))
    seen = []
    real = probe._probe_command

    async def spy(source):
        seen.append(source)
        return await real(source)

    monkeypatch.setattr(probe, "_probe_command", spy)
    await probe.probe_source(f"{base}/small.mp4")
    assert len(seen) == 1 and not seen[0].startswith("http") and seen[0].endswith(".video")


async def test_a_link_that_cannot_be_opened_is_not_called_a_file_with_no_picture(server):
    _, base = server
    with pytest.raises(probe.ProbeError) as raised:
        await probe.probe_source(f"{base}/missing.mp4")
    assert "no picture" not in str(raised.value)
    assert "could not be opened" in str(raised.value) or "damaged" in str(raised.value)


def test_ffmpeg_output_about_a_failed_open_is_told_apart_from_a_file_with_no_picture():
    failed = "[https @ 0x1] HTTP error 403 Forbidden\nhttps://x/y.mp4: Server returned 403 Forbidden (access denied)"
    with pytest.raises(probe.ProbeError, match="could not be opened"):
        probe.parse(failed)
    only_audio = "Input #0, mov,mp4\n  Duration: 00:00:02.00, start: 0.0\n  Stream #0:0[0x1](und): Audio: aac (LC), 44100 Hz, mono, fltp, 69 kb/s"
    with pytest.raises(probe.ProbeError, match="no picture"):
        probe.parse(only_audio)
