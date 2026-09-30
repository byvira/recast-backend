"""The voice calibration and the optional identity details really reach the generation prompt.

Before this, the calibration sliders and choices were saved but nothing in post generation read
them. Template rendering only: no model call, no database, no network.
"""

from app.agents.text.nodes import _build_brand_context_string
from app.pipelines.text.brand_context import build_brand_context

BASE = {"brand_type": "Person", "identity": {"name": "Asha"}, "voice_tone": {"tones": ["warm"]}}


def both(profile: dict) -> list[str]:
    return [build_brand_context(profile), _build_brand_context_string(profile)]


def test_a_profile_left_at_its_defaults_adds_nothing():
    plain = both(BASE)
    with_defaults = both({**BASE, "calibration": {
        "formality": 50, "directness": 50, "humor": 50, "optimism": 50, "energy": 50,
        "sentence_length": "balanced", "paragraph_spacing": "single", "vocabulary_level": "simple",
        "hook_aggressiveness": 50, "emoji_usage": "minimal", "channel_rules": {}, "signature_phrases": [],
    }})
    for a, b in zip(plain, with_defaults):
        assert "Writing style:" not in b
        assert a.split() == b.split()


def test_sliders_moved_to_an_end_are_written_in_both_generation_paths():
    for out in both({**BASE, "calibration": {"formality": 90, "directness": 10, "energy": 80, "optimism": 20, "hook_aggressiveness": 85}}):
        assert "Writing style:" in out
        assert "Formal and polished." in out
        assert "Gentle and indirect." in out
        assert "High energy" in out and "Measured and realistic" in out and "bold, attention-grabbing" in out


def test_the_middle_of_a_slider_says_nothing():
    for out in both({**BASE, "calibration": {"formality": 55, "directness": 45}}):
        assert "Formal and polished." not in out and "Casual and relaxed" not in out
        assert "Writing style:" not in out


def test_choices_phrases_and_channel_rules_are_written():
    for out in both({**BASE, "calibration": {
        "sentence_length": "short", "paragraph_spacing": "double", "vocabulary_level": "technical",
        "signature_phrases": ["Let's build", "Small steps"], "channel_rules": {"LinkedIn": "No hashtags", "X": ""},
    }}):
        assert "Short sentences." in out and "A blank line between paragraphs." in out and "precise technical vocabulary" in out
        assert "Let's build, Small steps" in out
        assert "For LinkedIn: No hashtags" in out and "For X:" not in out


def test_the_calibration_is_ignored_when_the_voice_is_switched_off():
    for out in both({**BASE, "is_active": False, "calibration": {"formality": 95}}):
        assert "Formal and polished." not in out


def test_optional_identity_details_are_written_for_each_type():
    person = {"brand_type": "Person", "identity": {"name": "Asha", "headline": "Sleep coach", "location": "Pune", "goals": ["Write a book", "Speak more"]}}
    for out in both(person):
        assert "More about the brand:" in out
        assert "Headline: Sleep coach" in out and "Based in: Pune" in out and "Goals: Write a book, Speak more" in out
    product = {"brand_type": "Product", "identity": {"product_name": "Nap", "one_liner": "Sleep, scheduled", "key_features": ["Alarms", "Reports"], "stage": "Beta"}}
    for out in both(product):
        assert "In one line: Sleep, scheduled" in out and "Key features: Alarms, Reports" in out and "Stage: Beta" in out


def test_shop_and_entertainment_details_are_written():
    for kind in ("Shop", "Entertainment"):
        for out in both({"brand_type": kind, "identity": {"name": "Blue Door", "tagline": "Made slowly", "description": "Ceramics"}}):
            assert "Name: Blue Door" in out and "Tagline: Made slowly" in out and "Description: Ceramics" in out


def test_no_extra_details_means_no_extra_heading():
    for out in both({"brand_type": "Person", "identity": {"name": "Asha"}}):
        assert "More about the brand:" not in out
