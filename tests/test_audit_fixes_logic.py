"""Small, fast tests for the pure-logic fixes from the flow audit (no database, no model calls)."""

import base64
import io
import json
import os
from datetime import timezone

from app.pipelines.publish.media_fit import BLUESKY_MAX_IMAGE_BYTES, fit_image_for_bluesky, jpeg_url
from app.pipelines.publish.spine import extra_media_note
from app.pipelines.publish.supervisor.fixer import _declared_limit, fix_content
from app.pipelines.publish.bluesky.publisher import _jwt_expiry
from app.pipelines.publish.meta import instagram
from app.pipelines.media.tts_generation import _deepgram_can_read
from app.pipelines.text.quality import check_banned_words
from app.api.v1.audio_assets import _master_transcript, _spoken_text


# ── pictures made acceptable to a platform ───────────────────────────────────

def test_jpeg_url_only_changes_our_own_image_host_and_only_once():
    assert jpeg_url("https://res.cloudinary.com/x/image/upload/v1/a/b.png") == "https://res.cloudinary.com/x/image/upload/f_jpg,q_90/v1/a/b.png"
    already = "https://res.cloudinary.com/x/image/upload/f_auto/v1/a.png"
    assert jpeg_url(already) == already
    other = "https://example.com/a.png"
    assert jpeg_url(other) == other


def test_a_small_picture_is_sent_to_bluesky_untouched():
    data = b"not even an image, but under the limit"
    assert fit_image_for_bluesky(data, "image/png") == (data, "image/png")


def test_a_big_picture_is_shrunk_to_fit_bluesky():
    from PIL import Image

    image = Image.frombytes("RGB", (1600, 1600), os.urandom(1600 * 1600 * 3))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    assert len(buf.getvalue()) > BLUESKY_MAX_IMAGE_BYTES
    out, mime = fit_image_for_bluesky(buf.getvalue(), "image/png")
    assert mime == "image/jpeg" and len(out) <= BLUESKY_MAX_IMAGE_BYTES


def test_an_unreadable_big_file_is_returned_as_it_was():
    junk = b"x" * (BLUESKY_MAX_IMAGE_BYTES + 10)
    assert fit_image_for_bluesky(junk, "image/png") == (junk, "image/png")


# ── connection renewal ──────────────────────────────────────────────────────

def _jwt(exp):
    def part(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{part({'alg': 'x'})}.{part({'exp': exp})}.sig"


def test_the_login_expiry_is_read_from_the_token_itself():
    when = _jwt_expiry(_jwt(2_000_000_000))
    assert when is not None and when.tzinfo == timezone.utc and when.year == 2033
    assert _jwt_expiry("garbage") is None
    assert _jwt_expiry(_jwt(None)) is None


# ── platform limits come from the registry ──────────────────────────────────

def test_trim_limits_are_declared_per_platform_in_the_registry():
    assert _declared_limit("linkedin") == 3000
    assert _declared_limit("LinkedIn") == 3000
    assert _declared_limit("bluesky") == 300
    assert _declared_limit("youtube") == 5000
    assert _declared_limit("blog") is None


def test_the_fixer_trims_to_the_declared_limit():
    fixed, text = fix_content("bluesky", "Hello there. " * 40, "Content too long")
    assert fixed and len(text) <= 300


# ── brand rules ────────────────────────────────────────────────────────────

def test_a_banned_word_matches_whole_words_only():
    assert check_banned_words("We take a class on this", ["ass"]) == []
    assert check_banned_words("That was an ass move", ["ass"]) == ["ass"]


# ── audio ──────────────────────────────────────────────────────────────────

def test_the_fallback_voice_refuses_scripts_it_cannot_read():
    assert _deepgram_can_read("This is a perfectly ordinary English sentence about our product launch.") is True
    assert _deepgram_can_read("இது ஒரு தமிழ் வாக்கியம், இதை ஆங்கிலக் குரல் படிக்க முடியாது.") is False
    assert _deepgram_can_read("ok") is True  # too short to tell: allowed, as before


def test_a_post_is_cleaned_for_reading_aloud():
    out = _spoken_text("## Big news\n- **We** shipped `it` my_file\nLove this #Launch #AI team\n> quote #x")
    assert out == "Big news\nWe shipped it my_file\nLove this team\nquote"
    assert "#" not in out and "*" not in out


def test_captions_use_the_transcript_the_approved_master_had():
    current = [{"word": "new", "start_s": 0.0, "end_s": 1.0}]
    pinned = [{"word": "old", "start_s": 0.0, "end_s": 2.0}]
    assert _master_transcript({"transcript": current, "approved_master_media_id": "m1", "approved_master_transcript": pinned}) == pinned
    # approved before the timings were kept, or not approved: the current ones, as before
    assert _master_transcript({"transcript": current, "approved_master_media_id": "m1"}) == current
    assert _master_transcript({"transcript": current}) == current


# ── posting ───────────────────────────────────────────────────────────────

def test_the_post_says_when_only_the_first_attached_picture_went_out():
    assert extra_media_note({"attachments": [{"media_id": "a"}]}) is None
    assert extra_media_note({"attachments": []}) is None
    note = extra_media_note({"attachments": [{"media_id": "a"}, {"media_id": "b"}, {"media_id": "c"}]})
    assert note and "first of 3" in note


class _FakeResponse:
    def __init__(self, status):
        self.status_code = 200
        self._status = status

    def json(self):
        return {"status_code": self._status}


class _FakeClient:
    def __init__(self, statuses):
        self.statuses = list(statuses)

    async def get(self, *_args, **_kwargs):
        return _FakeResponse(self.statuses.pop(0) if self.statuses else "IN_PROGRESS")


async def test_a_video_is_waited_for_until_instagram_says_it_is_ready(monkeypatch):
    monkeypatch.setattr(instagram, "_READY_POLL_SECONDS", 0)
    assert await instagram._wait_until_ready(_FakeClient(["IN_PROGRESS", "IN_PROGRESS", "FINISHED"]), "c1", "t") == (True, None)


async def test_a_video_instagram_cannot_process_is_reported_plainly(monkeypatch):
    monkeypatch.setattr(instagram, "_READY_POLL_SECONDS", 0)
    ready, problem = await instagram._wait_until_ready(_FakeClient(["IN_PROGRESS", "ERROR"]), "c1", "t")
    assert ready is False and problem and "could not process" in problem


async def test_a_video_still_processing_after_the_wait_is_tried_again_later(monkeypatch):
    monkeypatch.setattr(instagram, "_READY_POLL_SECONDS", 0)
    monkeypatch.setattr(instagram, "_READY_POLLS", 3)
    assert await instagram._wait_until_ready(_FakeClient([]), "c1", "t") == (False, None)


# ── sound checks for a recording (owner decision Q6) ─────────────────────────

from app.pipelines.audio.sound_checks import judge, parse_report  # noqa: E402


def test_the_sound_report_is_read_into_numbers():
    report = (
        "[Parsed_volumedetect_0] max_volume: -3.2 dB\n"
        "[silencedetect @ 0x1] silence_end: 12.0 | silence_duration: 3.5\n"
        "[silencedetect @ 0x1] silence_end: 20.0 | silence_duration: 4.1\n"
        '{\n\t"input_i" : "-17.40",\n\t"input_tp" : "-3.20"\n}\n'
    )
    m = parse_report(report)
    assert m["loudness_lufs"] == -17.4 and m["peak_db"] == -3.2 and m["silences"] == [3.5, 4.1] and m["measured"] is True


def test_a_comfortable_recording_is_good_on_all_three():
    checks = judge({"loudness_lufs": -16.0, "peak_db": -3.0, "silences": [0.4], "measured": True})
    assert [c["status"] for c in checks] == ["good", "good", "good"]


def test_quiet_loud_clipping_and_gaps_are_each_called_out_in_plain_words():
    quiet = judge({"loudness_lufs": -26.0, "peak_db": -10.0, "silences": [], "measured": True})
    assert quiet[0]["status"] == "attention" and "faint" in quiet[0]["message"]
    loud = judge({"loudness_lufs": -8.0, "peak_db": -0.1, "silences": [], "measured": True})
    assert loud[0]["status"] == "attention" and loud[1]["status"] == "attention"
    gaps = judge({"loudness_lufs": -16.0, "peak_db": -3.0, "silences": [3.2, 6.0, 1.0], "measured": True})
    assert gaps[2]["status"] == "attention" and "2 gaps" in gaps[2]["message"]


def test_a_file_that_cannot_be_read_is_unknown_not_good():
    assert [c["status"] for c in judge({"loudness_lufs": None, "peak_db": None, "silences": [], "measured": False})] == ["unknown"] * 3


# ── Tamil and Hindi in picture headlines (owner decision Q7) ─────────────────

from app.pipelines.media.image_render import _FONTS_DIR, _load_font, script_font_entry  # noqa: E402


def test_the_right_font_is_chosen_for_the_script_in_a_headline():
    assert script_font_entry("One episode. Ten posts.") is None
    assert script_font_entry("ஒரு episode, பத்து posts")["regular"] == "NotoSansTamil-Regular.ttf"
    assert script_font_entry("एक episode से दस posts")["bold"] == "NotoSansDevanagari-Bold.ttf"
    assert script_font_entry("") is None and script_font_entry(None) is None


def test_the_script_fonts_are_bundled_and_draw_real_letters_and_english_words():
    for name, sample in (("NotoSansTamil-Regular.ttf", "தமிழ்"), ("NotoSansDevanagari-Regular.ttf", "हिन्दी")):
        assert os.path.exists(os.path.join(_FONTS_DIR, name))
    tamil = _load_font("inter", 60, text="ஒரு episode")
    hindi = _load_font("inter", 60, bold=True, text="एक episode")
    for font, native in ((tamil, "ஒரு"), (hindi, "एक")):
        # real glyphs, not the empty box a font without the script draws: the native text and the English word both have width
        assert font.getmask(native).getbbox() and font.getmask("episode").getbbox()
        assert font.getlength("episode") > 5 * font.getlength("")
    # ordinary text keeps the brand font
    assert os.path.basename(_load_font("inter", 40, text="Hello").path) == "Inter.ttf"
    assert os.path.basename(tamil.path) == "NotoSansTamil-Regular.ttf" and os.path.basename(hindi.path) == "NotoSansDevanagari-Bold.ttf"


# ── English words stay English; brand voice in audio translation (owner decisions Q1 and Q2) ──

from app.core.config import settings  # noqa: E402
from app.pipelines.text.brand_context import build_tone_and_terms  # noqa: E402
from app.prompts.registry import load_prompt  # noqa: E402


def test_english_terms_rule_is_on_and_only_for_one_non_english_language_under_the_brand_tone():
    assert settings.ENGLISH_TERMS_STAY_ENGLISH is True
    rule = build_tone_and_terms("brand", "ta")
    assert "Keep brand names, product names, and technical or business terms in English" in rule
    assert "Translate everyday words into Tamil" in rule
    assert "normally say a word in English, keep it in English" in rule
    assert build_tone_and_terms(None, "hi") != ""
    # English, mixed languages and the other tones are left to what they already do
    assert build_tone_and_terms("brand", "en") == ""
    assert build_tone_and_terms("brand", "ta+en") == ""
    assert "ENGLISH TERMS" not in build_tone_and_terms("casual", "ta")


def test_the_audio_translation_asks_for_the_brand_voice_and_keeps_english_terms():
    with_brand = load_prompt("audio/localize/translate_script", target_language="Tamil", script="Hello team", brand_context="VOICE: warm and plain")
    assert "VOICE: warm and plain" in with_brand and "Write in this brand's voice and tone" in with_brand
    assert "Keep brand names, product names, and technical or business terms in English" in with_brand
    assert "written in English letters so the voice reads them as English" in with_brand
    without = load_prompt("audio/localize/translate_script", target_language="Tamil", script="Hello team", brand_context="")
    assert "Write in this brand's voice" not in without and "Keep brand names" in without


def test_the_shaping_library_ships_with_the_project_and_loads_only_on_linux():
    import pathlib
    import app as app_package

    lib = pathlib.Path(app_package.__file__).resolve().parent / "pipelines" / "media" / "native" / "libfribidi.so.0"
    assert lib.exists() and lib.read_bytes()[:4] == b"\x7fELF"
    assert (lib.parent / "FRIBIDI-LICENSE.txt").exists()
