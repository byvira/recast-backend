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


# ── pictures with writing in them are made again, then not used ───────────────

import pytest  # noqa: E402
from unittest.mock import AsyncMock, patch  # noqa: E402

from app.pipelines.media import image_generation as imagegen  # noqa: E402


def test_speech_bubbles_and_conversations_are_removed_from_the_prompt():
    out = enforce_no_text("Two overlapping speech bubbles and a chat window above a desk, a quote about conversations")
    described = out.split(NO_TEXT_SUFFIX.strip())[0].lower()  # what the picture is asked to show, before the no-writing rule
    for word in ("speech bubble", "chat window", "quote", "conversation"):
        assert word not in described, word


async def test_a_picture_with_writing_is_made_again_with_a_stricter_prompt():
    made = AsyncMock(side_effect=[b"first", b"second"])
    checks = AsyncMock(side_effect=[True, False])
    with patch.object(imagegen, "_generate_image_bytes", made), patch("app.agents.content_guard.media.picture_has_writing", checks):
        result = await imagegen._generate_text_free("a calm desk")
    assert result == b"second" and made.call_count == 2
    assert "no speech bubbles" in made.call_args_list[1].args[0] and "no speech bubbles" not in made.call_args_list[0].args[0]


async def test_a_picture_that_still_has_writing_is_not_used():
    made = AsyncMock(side_effect=[b"first", b"second"])
    with patch.object(imagegen, "_generate_image_bytes", made), patch("app.agents.content_guard.media.picture_has_writing", AsyncMock(return_value=True)):
        assert await imagegen._generate_text_free("a calm desk") is None
    assert made.call_count == 2
    assert "writing" in (imagegen.last_failure_reason() or "")


async def test_a_clean_picture_is_used_straight_away_and_an_unchecked_one_passes():
    for verdict in (False, None):
        made = AsyncMock(return_value=b"picture")
        with patch.object(imagegen, "_generate_image_bytes", made), patch("app.agents.content_guard.media.picture_has_writing", AsyncMock(return_value=verdict)):
            assert await imagegen._generate_text_free("a calm desk") == b"picture"
        assert made.call_count == 1


async def test_no_provider_picture_means_no_check_and_no_retry():
    made = AsyncMock(return_value=None)
    checks = AsyncMock()
    with patch.object(imagegen, "_generate_image_bytes", made), patch("app.agents.content_guard.media.picture_has_writing", checks):
        assert await imagegen._generate_text_free("a calm desk") is None
    assert made.call_count == 1 and not checks.called


async def test_after_writing_the_second_try_goes_to_the_backup_models_not_the_main_one_again():
    made = AsyncMock(side_effect=[b"first", b"from-backup"])
    checks = AsyncMock(side_effect=[True, False])
    with patch.object(imagegen, "_generate_image_bytes", made), patch("app.agents.content_guard.media.picture_has_writing", checks):
        result = await imagegen._generate_text_free("a calm desk")
    assert result == b"from-backup"
    assert made.call_args_list[0].kwargs["skip_primary"] is False
    assert made.call_args_list[1].kwargs["skip_primary"] is True


async def test_when_no_backup_can_make_a_picture_the_main_model_gets_one_more_try_with_the_stricter_prompt():
    made = AsyncMock(side_effect=[b"first", None, b"third"])
    checks = AsyncMock(side_effect=[True, False])
    with patch.object(imagegen, "_generate_image_bytes", made), patch("app.agents.content_guard.media.picture_has_writing", checks):
        result = await imagegen._generate_text_free("a calm desk")
    assert result == b"third" and made.call_count == 3
    assert [c.kwargs["skip_primary"] for c in made.call_args_list] == [False, True, False]
    assert "no speech bubbles" in made.call_args_list[2].args[0]


async def test_asking_to_skip_the_main_model_never_calls_it(monkeypatch):
    monkeypatch.setattr(imagegen.settings, "CLOUDFLARE_API_TOKEN", "token")
    monkeypatch.setattr(imagegen.settings, "CLOUDFLARE_ACCOUNT_ID", "account")
    main = AsyncMock(side_effect=AssertionError("the main model must not be called"))
    backup = AsyncMock(return_value=b"backup-picture")
    with patch.object(imagegen, "_call_cloudflare", main), patch("app.shared.open_fallbacks.open_image_fallback", backup):
        assert await imagegen._generate_image_bytes("a calm desk", skip_primary=True) == b"backup-picture"
    assert not main.called


async def test_without_the_skip_the_main_model_is_used_first(monkeypatch):
    monkeypatch.setattr(imagegen.settings, "CLOUDFLARE_API_TOKEN", "token")
    monkeypatch.setattr(imagegen.settings, "CLOUDFLARE_ACCOUNT_ID", "account")
    main = AsyncMock(return_value=b"main-picture")
    with patch.object(imagegen, "_call_cloudflare", main):
        assert await imagegen._generate_image_bytes("a calm desk") == b"main-picture"


# ── the words on a picture, and the rule on every prompt from the first try ───

def test_every_picture_prompt_carries_the_strong_no_writing_rule_from_the_first_try():
    out = enforce_no_text("A calm desk in soft morning light").lower()
    for phrase in ("no speech bubbles", "no chat windows", "no handwriting", "no text"):
        assert phrase in out, phrase


def test_picture_words_are_cleaned_and_unsafe_words_refused_with_a_friendly_message():
    from app.agents.content_guard.media import ContentRejected, tidy_picture_text

    assert tidy_picture_text("Fast \u2014 and simple") == "Fast, and simple"
    assert tidy_picture_text("") == "" and tidy_picture_text(None) is None
    with pytest.raises(ContentRejected) as caught:
        tidy_picture_text("Buy porn now")
    assert "picture text" in caught.value.detail["message"]


def test_the_picture_requests_apply_it_to_the_headline_and_the_author():
    from app.api.v1.image_assets import AddSlideRequest, GenerateImageAssetRequest
    from app.agents.content_guard.media import ContentRejected

    ok = GenerateImageAssetRequest(title="t", brand_id="b", headline="Fast \u2014 simple", author="Ana")
    assert ok.headline == "Fast, simple"
    for build in (
        lambda: GenerateImageAssetRequest(title="t", brand_id="b", headline="Hello", author="porn star"),
        lambda: AddSlideRequest(headline="Buy porn now"),
    ):
        with pytest.raises(ContentRejected):
            build()
