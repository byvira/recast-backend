"""Generated pictures must never ask the model for writing: image models garble it. Text and the logo are
added afterwards as real text and a real file. Pure tests, no network."""
from io import BytesIO

from PIL import Image

from app.pipelines.media.image_generation import (
    NO_TEXT_SUFFIX,
    _build_mascot_raw_prompt,
    _build_raw_prompt,
    enforce_no_text,
    stamp_logo,
)

PROFILE = {"identity": {"name": "Recast", "bio": "Turns one idea into posts"}, "visual_identity": {}}


def test_brand_name_is_not_in_the_picture_prompt():
    assert "Recast" not in _build_raw_prompt("consistent voice", PROFILE)
    assert "Recast" not in _build_mascot_raw_prompt(PROFILE)


def test_enforce_no_text_strips_writing_words_and_adds_the_rule():
    out = enforce_no_text("Two phones, one with the Recast logo and a caption on a poster, soft light")
    low = out.lower()
    assert "logo" not in low.replace("no logos", "")
    assert "caption" not in low
    assert "poster" not in low
    assert NO_TEXT_SUFFIX.strip() in out  # a prompt that mentions a screen also gets the soft-screen sentence after it
    assert len(out) <= 2048


def test_stamp_logo_puts_the_real_file_on_the_picture():
    def png(color, size):
        buf = BytesIO()
        Image.new("RGBA", size, color).save(buf, format="PNG")
        return buf.getvalue()

    stamped = stamp_logo(png((255, 255, 255, 255), (1024, 1024)), png((255, 0, 0, 255), (200, 200)))
    img = Image.open(BytesIO(stamped)).convert("RGB")
    r, g, b = img.getpixel((1024 - 60, 1024 - 60))
    assert r > 200 and g < 80 and b < 80
    assert img.getpixel((10, 10)) == (255, 255, 255)


def test_stamp_logo_keeps_the_picture_when_the_logo_is_unreadable():
    base = BytesIO()
    Image.new("RGB", (64, 64), "white").save(base, format="JPEG")
    assert stamp_logo(base.getvalue(), b"not an image") == base.getvalue()
