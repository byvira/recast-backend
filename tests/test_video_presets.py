"""Video advice for a platform is plain, comes from the preset list, and never blocks."""
from app.pipelines.media import video_presets as vp


def test_a_video_that_suits_the_platform_gets_no_advice():
    assert vp.advice("instagram_reels", "vertical", 30) == []


def test_a_video_that_is_too_long_for_the_platform_is_told_to_trim_it():
    notes = vp.advice("instagram_reels", "vertical", 240)
    assert any("accepts videos up to 3:00" in n and "trim it" in n for n in notes)
    assert notes[-1] == vp.DISCLAIMER


def test_the_wrong_shape_is_called_out_with_the_best_shapes():
    notes = vp.advice("youtube_shorts", "landscape", 30)
    assert any("vertical 9:16" in n and "landscape 16:9" in n for n in notes)


def test_a_long_but_allowed_video_gets_the_attention_hint_not_a_limit_warning():
    notes = vp.advice("linkedin", "square", 300)
    assert any("tend to hold attention" in n for n in notes)
    assert not any("accepts videos up to" in n for n in notes)


def test_no_platform_or_an_unknown_one_gives_no_advice():
    assert vp.advice(None, "square", 30) == []
    assert vp.advice("myspace", "square", 30) == []


def test_every_preset_names_a_real_content_platform_and_known_shapes():
    from app.models.text import Platform

    known_shapes = set(vp._SIZE_WORDS)
    names = {m.value for m in Platform}
    for key, preset in vp.PRESETS.items():
        assert set(preset["sizes"]) <= known_shapes, key
        assert preset["platform"] in names, f"{key} points at {preset['platform']}"
    assert len(vp.presets_payload()) == len(vp.PRESETS)
