"""Spoken length helpers (app.pipelines.media.duration). Pure, no network or database."""

import pytest

from app.pipelines.media import duration as d


def test_the_sample_script_that_caused_the_18_second_bug_really_is_about_that_long():
    sample = (
        "In this episode, we unpack why a good system beats a linear to-do list every single time. "
        "When you build in buffer for the unexpected, your output stays consistent and dependable even under pressure. "
        "Today, we walk through the complete process from a written script to a polished, ready-to-publish episode."
    )
    assert d.count_words(sample) == 50
    assert 17 <= d.estimate_seconds(sample, 150) <= 21


def test_estimate_follows_pace_and_clamps_it():
    text = "word " * 150
    assert d.estimate_seconds(text, 150) == 60.0
    assert d.estimate_seconds(text, 300) == d.estimate_seconds(text, 200)  # capped at the maximum pace
    assert d.estimate_seconds(text, 10) == d.estimate_seconds(text, 100)  # raised to the minimum pace
    assert d.estimate_seconds("", 150) == 0.0
    assert d.clamp_words_per_minute(None) == d.DEFAULT_WORDS_PER_MINUTE
    assert d.clamp_words_per_minute(float("nan")) == d.DEFAULT_WORDS_PER_MINUTE


@pytest.mark.parametrize("text,expected", [
    ("Write a 2 minute intro for my podcast", 120),
    ("a 2-minute intro", 120),
    ("make it about 90 seconds", 90),
    ("30 second ad", 30),
    ("1.5 minutes long", 90),
    ("keep it to 45 secs", 45),
    ("Script for a 10 min episode", 600),
])
def test_a_length_written_in_the_text_is_picked_up(text, expected):
    assert d.detect_requested_seconds(text) == expected


@pytest.mark.parametrize("text", [
    "", "no length here", "Chapter 5 s", "room 2 m wide", "We grew 3x in 2024", "call me at 5", "model 30 s",
])
def test_ordinary_text_is_not_mistaken_for_a_length(text):
    assert d.detect_requested_seconds(text) is None


def test_one_letter_units_need_a_cue_word_nearby():
    assert d.detect_requested_seconds("an intro of 2 m") == 120
    assert d.detect_requested_seconds("the ad is 30 s long") == 30


def test_a_requested_length_is_kept_inside_the_limits():
    assert d.detect_requested_seconds("a 5 second intro") == d.MIN_TARGET_SECONDS
    assert d.detect_requested_seconds("a 2 hour episode") == d.MAX_TARGET_SECONDS


def test_the_first_length_wins():
    assert d.detect_requested_seconds("a 1 minute intro, then 3 minutes of talk") == 60


def test_fit_assessment_says_short_long_or_ok():
    text = "word " * 75  # 30 seconds at 150 wpm
    assert d.fit_assessment(text, 60)["status"] == "short"
    assert d.fit_assessment(text, 30)["status"] == "ok"
    assert d.fit_assessment(text, 15)["status"] == "long"
    info = d.fit_assessment(text, 60)
    assert info["words"] == 75 and info["words_needed"] == 150 and info["estimated_seconds"] == 30.0


def test_words_for_seconds_and_limits():
    assert d.words_for_seconds(60, 150) == 150
    assert d.words_for_seconds(0.1, 150) == 1
    assert d.clamp_target_seconds(1) == d.MIN_TARGET_SECONDS
    assert d.clamp_target_seconds(99999) == d.MAX_TARGET_SECONDS
    assert all(d.MIN_TARGET_SECONDS <= p <= d.MAX_TARGET_SECONDS for p in d.PRESETS_SECONDS)


def test_real_duration_comes_from_the_last_timed_word():
    words = [{"word": "a", "end_s": 0.4}, {"word": "b", "end_s": 18.25}, {"word": "c", "end_s": 9.0}]
    assert d.duration_from_words(words) == 18.25


def test_duration_from_objects_and_bad_entries():
    class W:
        def __init__(self, end_s):
            self.end_s = end_s

    assert d.duration_from_words([W(2.0), W(5.5)]) == 5.5
    assert d.duration_from_words([{"end_s": None}, {"end_s": float("nan")}, {"end_s": -1}]) is None
    assert d.duration_from_words([]) is None
    assert d.duration_from_words(None) is None


def test_unreadable_audio_has_no_length_rather_than_a_guess():
    assert d.audio_duration_seconds(b"not audio") is None
    assert d.audio_duration_seconds(b"") is None


def test_the_length_of_real_audio_is_read_from_the_file():
    import io

    import numpy as np
    import soundfile as sf

    for fmt in ("WAV", "MP3"):
        buf = io.BytesIO()
        sf.write(buf, np.zeros(22050 * 3), 22050, format=fmt)
        assert d.audio_duration_seconds(buf.getvalue()) == pytest.approx(3.0, abs=0.1)


def test_trim_keeps_whole_sentences_and_fits_the_limit():
    from app.pipelines.media.duration import estimate_seconds, trim_to_seconds

    text = " ".join(["This is a sentence with seven words."] * 40)
    out = trim_to_seconds(text, 30, 150)
    assert estimate_seconds(out, 150) <= 30
    assert out.endswith(".") and out.count(".") >= 1
    assert trim_to_seconds("Short one.", 30, 150) == "Short one."
