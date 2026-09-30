"""Campaign media choices: off by default, per-type options validated, estimates. Pure."""

import pytest
from pydantic import ValidationError

from app.api.v1.campaigns import _content_types
from app.models.campaign import CampaignMediaPlan
from app.pipelines.campaigns import media


def test_media_is_off_by_default():
    plan = CampaignMediaPlan()
    assert plan.enabled is False and _content_types(plan.model_dump()) == ["text"]


def test_choosing_no_kind_switches_it_off():
    assert CampaignMediaPlan(enabled=True, kinds=[]).enabled is False


def test_kinds_are_cleaned_and_unknown_kinds_rejected():
    assert CampaignMediaPlan(enabled=True, kinds=["Image", "image", "audio"]).kinds == ["image", "audio"]
    with pytest.raises(ValidationError):
        CampaignMediaPlan(enabled=True, kinds=["hologram"])


def test_video_is_kept_in_the_plan_but_not_generated():
    plan = CampaignMediaPlan(enabled=True, kinds=["video", "image"]).model_dump()
    assert media.wanted_kinds(plan) == ["image"]
    assert _content_types(plan) == ["text", "image"]
    assert plan["video"]["duration_seconds"] == 30


def test_type_options_are_validated():
    with pytest.raises(ValidationError):
        CampaignMediaPlan(image={"layout": "nope"})
    with pytest.raises(ValidationError):
        CampaignMediaPlan(audio={"max_seconds": 5})
    with pytest.raises(ValidationError):
        CampaignMediaPlan(video={"aspect": "4:3"})
    assert CampaignMediaPlan(image={"layout": "story_9_16"}, audio={"max_seconds": 60}).image.layout == "story_9_16"


def test_estimate_counts_what_a_run_will_make():
    plan = CampaignMediaPlan(enabled=True, kinds=["image", "audio"], count_per_post=3).model_dump()
    assert media.estimate(plan, 7) == {"images": 21, "audio": 7}
    assert media.estimate(CampaignMediaPlan().model_dump(), 7) == {"images": 0, "audio": 0}
