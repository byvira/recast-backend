"""A weak audio or video draft is retried with the lowest scoring part named, not a generic "improve depth"."""
from app.agents.base import weakest_dimension
from app.prompts.registry import load_prompt


def test_the_lowest_scoring_part_is_named_in_plain_words():
    assert weakest_dimension({"completeness": 0.9, "brand_alignment": 0.4, "accuracy": 0.8, "content": 0.7}).startswith("brand voice")
    assert weakest_dimension({"platform_fit": 0.5, "engagement_potential": 0.3, "brand_alignment": 0.9}).startswith("engagement")
    assert weakest_dimension({}) == ""
    assert weakest_dimension({"content": 0.2}) == ""  # the overall score is not a part of the draft


def _audio(**extra):
    return load_prompt(
        "media/shared/generate", domain="audio", output_type="show_notes", topics=["focus"], key_quotes=["x"],
        transcript_excerpt="We talked about focus.", **extra,
    )


def _video(**extra):
    return load_prompt(
        "media/shared/generate", domain="video", output_type="short", platform="LinkedIn", topics=["focus"],
        key_moments=[], repurpose_angles=[], transcript_excerpt="We talked about focus.", **extra,
    )


def test_the_retry_prompt_carries_the_weak_part_only_on_a_retry():
    first = _audio(retry=0, retry_focus="accuracy, staying true to what was actually said")
    assert "weakest part" not in first
    retried = _audio(retry=1, retry_focus="accuracy, staying true to what was actually said")
    assert "The weakest part last time was accuracy" in retried and "Fix that first." in retried
    assert "weakest part" not in _audio(retry=1)  # no focus known
    assert "The weakest part last time was engagement" in _video(retry=1, retry_focus="engagement, the opening")
