"""4K picture shapes and the H.265 option: the edit list accepts the 4K sizes (even, inside the worker's limit), and the codec travels in the
output the worker receives, only when it was asked for."""
from app.video import edl, jobs


def test_the_two_4k_shapes_are_valid_edit_lists_with_even_sides():
    for size, (w, h) in {"vertical_4k": (2160, 3840), "landscape_4k": (3840, 2160)}.items():
        blank = edl.blank(size)
        assert (blank["width"], blank["height"]) == (w, h)
        assert edl.validate(blank)["width"] == w
        assert w <= edl.MAX_SIDE and h <= edl.MAX_SIDE and w % 2 == 0 and h % 2 == 0


def test_the_720p_shapes_are_valid_too():
    for size, (w, h) in {"vertical_720": (720, 1280), "landscape_720": (1280, 720), "square_720": (720, 720), "portrait_720": (720, 900)}.items():
        blank = edl.blank(size)
        assert (blank["width"], blank["height"]) == (w, h)
        assert edl.validate(blank)["height"] == h


def test_the_codec_is_an_allowed_output_key_and_only_h265_is_sent():
    assert "codec" in jobs.OUTPUT_KEYS
    clean = {k: v for k, v in {"crf": 21, "codec": "h265", "evil": "x"}.items() if k in jobs.OUTPUT_KEYS}
    assert clean == {"crf": 21, "codec": "h265"}


def test_the_emoji_setting_is_kept_checked_and_defaults_to_remove():
    import pytest

    base = edl.blank("vertical")
    assert edl.validate(base)["captions"]["emoji"] == "remove"
    shown = {**base, "captions": {**base["captions"], "emoji": "show"}}
    assert edl.validate(shown)["captions"]["emoji"] == "show"
    with pytest.raises(Exception):
        edl.validate({**base, "captions": {**base["captions"], "emoji": "colour"}})


def test_the_spoken_word_highlight_defaults_on_and_can_be_turned_off():
    base = edl.blank("vertical")
    assert edl.validate(base)["captions"]["highlight"] is True
    off = {**base, "captions": {**base["captions"], "highlight": False}}
    assert edl.validate(off)["captions"]["highlight"] is False
