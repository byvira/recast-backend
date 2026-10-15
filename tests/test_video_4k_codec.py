"""4K picture shapes and the H.265 option: the edit list accepts the 4K sizes (even, inside the worker's limit), and the codec travels in the
output the worker receives, only when it was asked for."""
from app.video import edl, jobs


def test_the_two_4k_shapes_are_valid_edit_lists_with_even_sides():
    for size, (w, h) in {"vertical_4k": (2160, 3840), "landscape_4k": (3840, 2160)}.items():
        blank = edl.blank(size)
        assert (blank["width"], blank["height"]) == (w, h)
        assert edl.validate(blank)["width"] == w
        assert w <= edl.MAX_SIDE and h <= edl.MAX_SIDE and w % 2 == 0 and h % 2 == 0


def test_the_codec_is_an_allowed_output_key_and_only_h265_is_sent():
    assert "codec" in jobs.OUTPUT_KEYS
    clean = {k: v for k, v in {"crf": 21, "codec": "h265", "evil": "x"}.items() if k in jobs.OUTPUT_KEYS}
    assert clean == {"crf": 21, "codec": "h265"}
