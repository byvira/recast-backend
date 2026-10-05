"""Which kinds of content each platform really takes, and that a campaign makes no media a post could never carry."""
from app.pipelines.campaigns.media import _kinds_this_platform_takes
from app.platforms.base import PLATFORM_REGISTRY, import_all, list_platforms

import_all()


def test_each_platform_says_how_it_takes_each_kind():
    linkedin = PLATFORM_REGISTRY["linkedin"]
    assert linkedin.modalities == {"text": "native", "image": "native", "audio": "via_video", "video": "native"}
    assert PLATFORM_REGISTRY["youtube"].modality("image") == "native"  # as the video's thumbnail
    assert PLATFORM_REGISTRY["youtube"].modality("audio") == "via_video"
    assert PLATFORM_REGISTRY["soundcloud"].modalities["audio"] == "native"
    assert PLATFORM_REGISTRY["patreon"].modality("text") == "link"  # a hand-off, nothing is attached for the member


def test_a_platform_that_cannot_carry_a_kind_says_none():
    assert PLATFORM_REGISTRY["blog"].modality("image") == "none"
    assert PLATFORM_REGISTRY["blog"].modality("audio") == "none"
    assert PLATFORM_REGISTRY["bluesky"].modality("audio") == "none"
    assert PLATFORM_REGISTRY["vimeo"].modality("text") == "none"


def test_listing_by_modality_keeps_only_platforms_that_can_take_it():
    audio = {p.key for p in list_platforms(modality="audio")}
    assert {"soundcloud", "linkedin", "youtube"} <= audio
    assert "blog" not in audio and "bluesky" not in audio and "vimeo" not in audio
    video = {p.key for p in list_platforms(modality="video")}
    assert {"youtube", "vimeo", "tiktok"} <= video and "newsletter" not in video


def test_a_post_on_a_text_only_platform_gets_no_media_made():
    usable, skipped = _kinds_this_platform_takes({"platform": "Blog"}, ["image", "audio"])
    assert usable == [] and skipped == {"image": "skipped", "audio": "skipped"}
    usable, skipped = _kinds_this_platform_takes({"platform": "LinkedIn"}, ["image", "audio"])
    assert usable == ["image", "audio"] and skipped == {}
    usable, skipped = _kinds_this_platform_takes({"platform": "Not A Platform"}, ["image"])
    assert usable == ["image"] and skipped == {}


def test_every_planned_platform_has_a_documentation_check_and_hidden_ones_stay_in_ops():
    from app.platforms.verification_data import VERDICTS

    planned = {k for k, p in PLATFORM_REGISTRY.items() if p.status == "planned"}
    assert planned <= set(VERDICTS)
    assert PLATFORM_REGISTRY["medium"].visibility == "staff_only" and PLATFORM_REGISTRY["medium"].has_official_post_api is False
    assert PLATFORM_REGISTRY["mastodon"].visibility == "member" and PLATFORM_REGISTRY["mastodon"].verified_at == "2026-10-04"
    assert PLATFORM_REGISTRY["reddit"].has_official_post_api is None  # the official pages could not be opened
    assert PLATFORM_REGISTRY["nostr"].modality("image") == "link"
    assert "medium" in {p.key for p in list_platforms()}  # Ops still lists it
