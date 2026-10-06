from datetime import datetime

from app.models.media import MediaAsset
from app.pipelines.publish.media_limits import MB, media_problem


def asset(kind, **fields):
    return MediaAsset(
        id="m1", kind=kind, mime_type="image/jpeg" if kind == "image" else "video/mp4", url="https://x/y",
        workspace_id="w", source="uploaded", created_by="u", created_at=datetime(2026, 1, 1), **fields,
    )


def test_big_facebook_picture_is_stopped_with_the_sizes():
    message = media_problem("facebook", [asset("image", size_bytes=12 * MB)])
    assert "12 MB" in message and "10 MB" in message


def test_picture_under_the_limit_passes():
    assert media_problem("facebook", [asset("image", size_bytes=3 * MB)]) is None


def test_unknown_size_is_never_held_against_a_file():
    assert media_problem("threads", [asset("image"), asset("video")]) is None


def test_threads_video_longer_than_five_minutes_is_stopped():
    assert "shorter" in media_problem("threads", [asset("video", duration_s=301)])
    assert media_problem("threads", [asset("video", duration_s=300)]) is None


def test_bluesky_video_over_300_mb_is_stopped():
    assert "300 MB" in media_problem("bluesky", [asset("video", size_bytes=301 * MB)])


def test_instagram_shape_must_be_between_4_5_and_1_91():
    assert media_problem("instagram", [asset("image", width=1080, height=1350)]) is None
    assert media_problem("instagram", [asset("image", width=1080, height=1920)]) is not None
    assert media_problem("instagram", [asset("image", width=2000, height=800)]) is not None


def test_platforms_without_a_confirmed_limit_are_left_alone():
    assert media_problem("linkedin", [asset("video", size_bytes=10_000 * MB)]) is None
