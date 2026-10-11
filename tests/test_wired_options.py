"""The choices a member can make for pictures, narrations and campaign posts, and that each one really changes the result."""
import asyncio
import io

import numpy as np
import pytest
import soundfile as sf
from PIL import Image, ImageDraw
from pydantic import ValidationError

from app.models.campaign import CampaignMediaPlan, CampaignTextOptions, CreateCampaignRequest
from app.models.text import ExtrasConfig, Platform
from app.pipelines.media import narration_delivery
from app.pipelines.media.export_options import enlarge, is_default_export, render_export
from app.pipelines.media.visual_profiles import VISUAL_PROFILES, style_line
from app.pipelines.text.quality import run_quality_gate
from app.prompts.registry import load_prompt


def _png(width: int = 100, height: int = 125) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (200, 60, 40)).save(buf, "PNG")
    return buf.getvalue()


# ── picture export ──────────────────────────────────────────────────────────

def test_default_export_options_keep_the_standard_path():
    assert is_default_export(1, 300, True)
    assert not is_default_export(2, 300, True)
    assert not is_default_export(1, 150, True)
    assert not is_default_export(1, 300, False)


@pytest.mark.parametrize("factor", [2, 4])
def test_enlarge_makes_the_picture_that_much_bigger(factor):
    out = Image.open(io.BytesIO(enlarge(_png(), factor)))
    assert out.size == (100 * factor, 125 * factor)


def test_enlarge_one_x_is_untouched():
    data = _png()
    assert enlarge(data, 1) == data


def test_a_very_large_picture_is_not_enlarged_past_the_cap():
    out = Image.open(io.BytesIO(enlarge(_png(5000, 5000), 4)))
    assert max(out.size) <= 8000


def test_export_writes_the_chosen_resolution_into_the_file():
    out = Image.open(io.BytesIO(render_export(_png(), export_format="png", dpi=150)))
    assert round(out.info["dpi"][0]) == 150
    jpg = Image.open(io.BytesIO(render_export(_png(), export_format="jpg", dpi=72)))
    assert round(jpg.info["dpi"][0]) == 72


def test_export_formats_and_size_settings():
    # a picture with real detail, so squeezing the file makes a difference
    noisy = io.BytesIO()
    rng = np.random.default_rng(1)
    Image.fromarray(rng.integers(0, 255, (400, 320, 3), dtype="uint8")).save(noisy, "PNG")
    plain = render_export(noisy.getvalue(), export_format="webp", optimize=False)
    small = render_export(noisy.getvalue(), export_format="webp", optimize=True)
    assert Image.open(io.BytesIO(small)).format == "WEBP"
    assert len(small) <= len(plain)
    with pytest.raises(ValueError):
        render_export(_png(), export_format="gif")


# ── looks ────────────────────────────────────────────────────────────────────

def test_every_look_has_words_and_unknown_ones_have_none():
    for name in VISUAL_PROFILES:
        assert style_line(name).startswith("Style:")
    assert style_line("nope") == ""
    assert style_line(None) == ""


# ── campaign options ─────────────────────────────────────────────────────────

def test_campaign_text_options_default_to_what_a_campaign_did_before():
    opts = CampaignTextOptions()
    assert (opts.tone, opts.hook_count, opts.hashtags, opts.carousel_format, opts.enforce_char_limits) == ("brand", 3, True, False, True)
    assert CreateCampaignRequest(name="n", brand_id="b", topic_cluster="t", platforms=["LinkedIn"]).text_options == opts


def test_campaign_text_options_reject_bad_values():
    with pytest.raises(ValidationError):
        CampaignTextOptions(tone="loud")
    with pytest.raises(ValidationError):
        CampaignTextOptions(hook_count=9)


def test_campaign_media_options_are_kept_and_checked():
    plan = CampaignMediaPlan(
        enabled=True, kinds=["audio", "image"],
        audio={"delivery_style": "keynote", "mastering": "studio"}, image={"visual_profile": "mono_technical"},
        video={"caption_style": "karaoke_glow"},
    )
    assert (plan.audio.delivery_style, plan.audio.mastering, plan.image.visual_profile, plan.video.caption_style) == (
        "keynote", "studio", "mono_technical", "karaoke_glow",
    )
    with pytest.raises(ValidationError):
        CampaignMediaPlan(enabled=True, kinds=["image"], image={"visual_profile": "nope"})


# ── text options ─────────────────────────────────────────────────────────────

def _hooks(count: int) -> str:
    return load_prompt(
        "text/hooks/generate", brand_context="b", platform_label="LinkedIn", hook_count=count,
        banned_openings=[], banned_words=[], content="x", language_line="",
    )


def test_hook_prompt_asks_for_exactly_the_chosen_number():
    for count in (1, 3, 5):
        text = _hooks(count)
        assert f"exactly {count} alternative opening hook" in text
        assert f"Hook {count} " in text
        assert f"Hook {count + 1} " not in text


def test_extras_carry_the_new_text_options():
    extras = ExtrasConfig(hook_count=5, carousel_format=True, enforce_char_limits=False).model_dump()
    assert (extras["hook_count"], extras["carousel_format"], extras["enforce_char_limits"]) == (5, True, False)
    with pytest.raises(ValidationError):
        ExtrasConfig(hook_count=0)


def test_a_post_over_the_tweet_limit_fails_only_when_limits_are_enforced():
    long_tweet = "word " * 80
    kwargs = dict(content=long_tweet, platform=Platform.TWITTER, brand_context="", banned_words=[])
    hard = asyncio.run(run_quality_gate(**kwargs, enforce_limits=True))
    soft = asyncio.run(run_quality_gate(**kwargs, enforce_limits=False))
    assert any("280" in i and not i.startswith("Advisory") for i in hard.issues)
    assert any("280" in i and i.startswith("Advisory") for i in soft.issues)
    assert not any("280" in i and not i.startswith("Advisory") for i in soft.issues)


def test_carousel_instruction_names_slides():
    assert "Slide 1:" in load_prompt("text/generate/carousel_format")


# ── narration ────────────────────────────────────────────────────────────────

def test_each_delivery_style_changes_the_voice_settings():
    assert narration_delivery.settings_for("keynote")["pause_cadence"] == "tight"
    assert narration_delivery.settings_for("founder")["emotional_tone"] == "authoritative"
    assert narration_delivery.settings_for("unknown") == {}


def test_mastering_gives_back_a_real_file_and_a_bad_file_gives_none():
    sr = 22050
    t = np.linspace(0, 2, sr * 2)
    buf = io.BytesIO()
    sf.write(buf, (0.3 * np.sin(2 * np.pi * 220 * t)).astype("float32"), sr, format="WAV")
    for preset in ("studio", "broadcast"):
        out = asyncio.run(narration_delivery.master(buf.getvalue(), preset))
        assert out and out[:4] == b"RIFF"
    assert asyncio.run(narration_delivery.master(b"not audio", "studio")) is None
    assert asyncio.run(narration_delivery.master(buf.getvalue(), "nope")) is None


# ── transcript punctuation ───────────────────────────────────────────────────

def _words(text: str):
    from app.models.audio_asset import TranscriptWord

    return [TranscriptWord(word=w, start_s=i * 0.4, end_s=i * 0.4 + 0.3) for i, w in enumerate(text.split())]


def test_punctuation_is_restored_without_moving_any_timing():
    from app.pipelines.audio.transcriber import restore_punctuation

    bare = _words("so what do you think")
    out = restore_punctuation(bare, "So, what do you think?")
    assert [w.word for w in out] == ["So,", "what", "do", "you", "think?"]
    assert [(w.start_s, w.end_s) for w in out] == [(w.start_s, w.end_s) for w in bare]


def test_a_word_that_cannot_be_matched_is_left_alone():
    from app.pipelines.audio.transcriber import restore_punctuation

    out = restore_punctuation(_words("hello there friend"), "Completely different words here.")
    assert [w.word for w in out] == ["hello", "there", "friend"]


def test_punctuation_can_be_taken_off():
    from app.pipelines.audio.transcriber import strip_punctuation

    assert [w.word for w in strip_punctuation(_words("So, what do you think?"))] == ["So", "what", "do", "you", "think"]


# ── background removal and looks ─────────────────────────────────────────────

def test_a_cutout_is_laid_on_the_brand_colour():
    from app.pipelines.media.background_removal import on_colour

    cut = io.BytesIO()
    Image.new("RGBA", (40, 40), (0, 0, 0, 0)).save(cut, "PNG")  # nothing in it: all background
    out = Image.open(io.BytesIO(on_colour(cut.getvalue(), "#e04a1f"))).convert("RGB")
    assert out.getpixel((10, 10)) == (224, 74, 31)


def test_background_removal_makes_a_real_cutout():
    from app.pipelines.media import background_removal

    if not background_removal.available():
        pytest.skip("the background removal library is not installed here")
    src = Image.new("RGB", (320, 320), (240, 240, 240))
    ImageDraw.Draw(src).ellipse((90, 90, 230, 230), fill=(200, 30, 30))
    buf = io.BytesIO()
    src.save(buf, "PNG")
    out = background_removal.cut_out(buf.getvalue())
    if out is None:
        pytest.skip("the small model could not be loaded here (it is fetched on first use)")
    assert Image.open(io.BytesIO(out)).mode == "RGBA"


def test_the_looks_change_a_picture_and_none_leaves_it_alone():
    from app.pipelines.media.image_effects import apply_effects

    base = Image.new("RGBA", (300, 300), (30, 30, 30, 255))
    assert apply_effects(base, primary_hex="#e04a1f", accent_hex="#38bdf8", effects={}) is base
    for look in ("glow", "dual_tone", "grid"):
        made = apply_effects(base, primary_hex="#e04a1f", accent_hex="#38bdf8", effects={look: True})
        assert made.tobytes() != base.tobytes(), look


# ── type scale, corner radius, dropped documents ─────────────────────────────

def _headline_size(ratio):
    from app.pipelines.media.image_layers import default_layers
    from app.pipelines.media.image_render import BrandTokens

    brand = BrandTokens(primary_hex="#1c1814", secondary_hex="#0f172a", accent_hex="#38bdf8", heading_font=None, body_font=None)
    layers = default_layers(size=(1080, 1350), brand=brand, headline="Systems beat to-do lists", type_ratio=ratio)
    return next(l for l in layers if l.name == "Headline").size


def test_the_standard_type_ratio_keeps_the_headline_size_and_others_change_it():
    standard = _headline_size(None)
    assert _headline_size(1.333) == standard
    assert _headline_size(1.125) < standard < _headline_size(1.5)


def test_corner_radius_rounds_the_picture_and_zero_keeps_it_square():
    from app.pipelines.media.image_layers import render_layers
    from app.pipelines.media.image_render import BrandTokens

    brand = BrandTokens(primary_hex="#1c1814", secondary_hex="#0f172a", accent_hex="#38bdf8", heading_font=None, body_font=None)
    rounded = Image.open(io.BytesIO(render_layers(size=(400, 500), background_bytes=None, layers=[], brand=brand, effects={"corner_radius": 0.08})))
    assert rounded.mode == "RGBA" and rounded.getpixel((0, 0))[3] == 0 and rounded.getpixel((200, 250))[3] == 255
    square = Image.open(io.BytesIO(render_layers(size=(400, 500), background_bytes=None, layers=[], brand=brand, effects={})))
    assert square.mode == "RGB"


def test_a_logo_tile_gets_round_corners_from_its_inner_radius():
    from app.pipelines.media.image_layers import _round_corners

    tile = Image.new("RGBA", (100, 100), (255, 0, 0, 255))
    out = _round_corners(tile, 0.3)
    assert out.getpixel((0, 0))[3] == 0 and out.getpixel((50, 50))[3] == 255
    assert _round_corners(tile, 0) is tile


def _docx_bytes(text: str) -> bytes:
    from docx import Document

    document = Document()
    document.add_paragraph(text)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def test_documents_give_their_text_and_a_title():
    from app.pipelines.campaigns.source_files import read_source_file

    out = read_source_file("Plan notes.docx", _docx_bytes("Customers want one clear answer. " * 4))
    assert out["title"] == "Plan notes" and "clear answer" in out["text"] and out["condensed"] is False
    assert "Hello world" in read_source_file("a.md", ("Hello world " * 5).encode())["text"]


def test_a_long_document_is_condensed_from_across_all_of_it():
    from app.pipelines.campaigns.source_files import read_source_file
    from app.pipelines.campaigns.sources import MAX_SOURCE_CHARS

    long_text = " ".join(f"Section {i} covers topic {i}." for i in range(3000))
    out = read_source_file("big.txt", long_text.encode())
    assert out["condensed"] is True and len(out["text"]) <= MAX_SOURCE_CHARS + 100
    assert "Section 2900" in out["text"] or "Section 28" in out["text"]


def test_a_pdf_is_read():
    from fpdf import FPDF
    from app.pipelines.campaigns.source_files import read_source_file

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.multi_cell(0, 8, "Systems beat to-do lists because they repeat. " * 6)
    assert "Systems beat" in read_source_file("x.pdf", bytes(pdf.output()))["text"]


@pytest.mark.parametrize("name,data,words", [
    ("clip.mp4", b"x", "video"), ("tool.exe", b"x", "Use a text"), ("empty.txt", b"", "empty"), ("tiny.txt", b"hi", "hardly any text"),
])
def test_files_that_cannot_be_used_say_why(name, data, words):
    from app.pipelines.campaigns.source_files import read_source_file
    from app.pipelines.campaigns.sources import SourceError

    with pytest.raises(SourceError) as err:
        read_source_file(name, data)
    assert words in str(err.value)
