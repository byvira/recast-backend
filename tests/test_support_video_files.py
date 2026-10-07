import pytest

from app.shared import support_files as files


def _mp4(size: int = 64) -> bytes:
    return b"\x00\x00\x00\x18ftypmp42" + b"\x00" * size


def test_mp4_video_is_accepted():
    name, mime = files.validate("clip.mp4", "video/mp4", _mp4())
    assert (name, mime) == ("clip.mp4", "video/mp4")


def test_quicktime_and_webm_are_accepted():
    assert files.validate("screen.mov", "video/quicktime", _mp4())[1] == "video/quicktime"
    assert files.validate("screen.webm", "video/webm", b"\x1a\x45\xdf\xa3" + b"\x00" * 32)[1] == "video/webm"


def test_video_that_is_not_a_video_is_refused():
    with pytest.raises(files.AttachmentRejected):
        files.validate("fake.mp4", "video/mp4", b"just some text, not a video at all")


def test_video_over_its_limit_is_refused(monkeypatch):
    monkeypatch.setattr(files, "MAX_VIDEO_BYTES", 100)
    with pytest.raises(files.AttachmentRejected, match="too large"):
        files.validate("big.mp4", "video/mp4", _mp4(500))


def test_an_image_still_uses_the_smaller_limit(monkeypatch):
    monkeypatch.setattr(files, "MAX_ATTACHMENT_BYTES", 100)
    with pytest.raises(files.AttachmentRejected, match="too large"):
        files.validate("big.png", "image/png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 500)
