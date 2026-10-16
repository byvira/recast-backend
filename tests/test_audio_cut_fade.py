"""Cut sections and fades in the cleanup engine, measured on real synthetic audio."""
import io

import numpy as np
import pytest
import soundfile as sf
from pydantic import ValidationError

from app.pipelines.media.audio_cleanup import CleanupError, CleanupSettings, apply_cleanup

SR = 16000


def _wav(signal: np.ndarray) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, signal.astype("float32"), SR, format="WAV")
    return buf.getvalue()


def _read(data: bytes) -> np.ndarray:
    out, sr = sf.read(io.BytesIO(data), dtype="float32")
    assert sr == SR
    return out


def _tone(seconds: float, amp: float = 0.3) -> np.ndarray:
    t = np.arange(int(SR * seconds)) / SR
    return amp * np.sin(2 * np.pi * 220 * t)


def test_a_chosen_section_is_removed_and_the_length_drops_by_that_much():
    out, _, applied = apply_cleanup(_wav(_tone(10)), CleanupSettings(cut_ranges=[(2.0, 5.0)]), [])
    assert abs(len(_read(out)) / SR - 7.0) < 0.1
    assert applied["sections_cut"] == 1 and applied["seconds_removed"] == 3.0


def test_words_after_a_cut_move_earlier_and_words_inside_it_are_dropped():
    words = [
        {"text": "keep", "start_s": 0.5, "end_s": 1.0},
        {"text": "gone", "start_s": 3.0, "end_s": 3.5},
        {"text": "later", "start_s": 6.0, "end_s": 6.5},
    ]
    _, after, _ = apply_cleanup(_wav(_tone(10)), CleanupSettings(cut_ranges=[(2.0, 5.0)]), words)
    assert [w["text"] for w in after] == ["keep", "later"]
    assert abs(after[1]["start_s"] - 3.0) < 0.05


def test_cuts_past_the_end_are_refused_and_so_is_removing_everything():
    with pytest.raises(CleanupError):
        apply_cleanup(_wav(_tone(5)), CleanupSettings(cut_ranges=[(20.0, 30.0)]), [])
    with pytest.raises(CleanupError):
        apply_cleanup(_wav(_tone(5)), CleanupSettings(cut_ranges=[(0.0, 5.0)]), [])


def test_a_backwards_or_negative_range_is_not_accepted():
    for bad in ([(5.0, 2.0)], [(-1.0, 2.0)], [(3.0, 3.0)]):
        with pytest.raises(ValidationError):
            CleanupSettings(cut_ranges=bad)


def test_fades_bring_the_ends_down_to_silence_and_leave_the_middle_alone():
    out, _, applied = apply_cleanup(_wav(_tone(6)), CleanupSettings(fade_in_s=1.0, fade_out_s=1.0), [])
    audio = _read(out)
    assert abs(audio[:50]).max() < 0.01 and abs(audio[-50:]).max() < 0.01
    middle = audio[2 * SR:4 * SR]
    assert abs(middle).max() > 0.25
    assert applied["fade_in_s"] == 1.0 and applied["fade_out_s"] == 1.0


def test_a_fade_after_a_cut_lands_on_the_new_end():
    out, _, _ = apply_cleanup(_wav(_tone(10)), CleanupSettings(cut_ranges=[(6.0, 9.0)], fade_out_s=0.5), [])
    audio = _read(out)
    assert abs(audio[-40:]).max() < 0.01
