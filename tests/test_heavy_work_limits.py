"""The memory limits on heavy work: the total-pixel cap on exports, the audio length budget, and the one-at-a-time gate."""
import asyncio
import io

import numpy as np
import pytest
import soundfile as sf
from PIL import Image

from app.pipelines.media import audio_budget, export_options
from app.shared import heavy_work


def _png(size):
    out = io.BytesIO()
    Image.new("RGB", size, (9, 9, 9)).save(out, format="PNG")
    return out.getvalue()


def _wav(seconds: float, sr: int = 8000, channels: int = 2) -> bytes:
    out = io.BytesIO()
    sf.write(out, np.zeros((int(seconds * sr), channels), dtype="float32"), sr, format="WAV", subtype="PCM_16")
    return out.getvalue()


def test_a_big_cover_at_4x_is_limited_by_total_pixels_not_just_the_longest_side():
    # 3000 x 3000 at 4x would be 144 megapixels; the cap keeps it at or under 24.
    img = export_options._enlarged_image(_png((300, 300)), 4)  # same ratio check at a small size
    assert img.size == (1200, 1200)
    big = Image.new("RGB", (3000, 3000))
    out = io.BytesIO()
    big.save(out, format="PNG", compress_level=0)
    result = export_options._enlarged_image(out.getvalue(), 4)
    assert result.size[0] * result.size[1] <= export_options.MAX_PIXELS
    assert result.size[0] > 3000  # still enlarged, just not by the full factor


def test_render_export_does_not_enlarge_beyond_the_cap_in_any_format():
    for fmt in ("png", "jpg", "webp"):
        data = export_options.render_export(_png((2000, 2000)), export_format=fmt, upscale=4)
        with Image.open(io.BytesIO(data)) as made:
            assert made.size[0] * made.size[1] <= export_options.MAX_PIXELS


def test_a_short_recording_fits_the_audio_budget():
    assert audio_budget.check_fits(_wav(2.0)) is None


def test_a_long_recording_is_refused_with_a_plain_message_before_decoding():
    long_audio = _wav(60.0, sr=8000, channels=2)
    message = audio_budget.check_fits(long_audio, budget_bytes=2 * 1024 * 1024)
    assert message and "minute" in message and "playing" not in message
    assert "play, transcribe and publish" in message


def test_the_allowed_length_shrinks_with_more_channels_and_a_higher_rate():
    mono = audio_budget.seconds_allowed(44100, 1)
    stereo = audio_budget.seconds_allowed(44100, 2)
    assert mono == pytest.approx(stereo * 2)
    assert audio_budget.seconds_allowed(22050, 2) == pytest.approx(stereo * 2)


def test_cleanup_and_mixing_refuse_a_recording_over_the_budget(monkeypatch):
    from app.pipelines.media import audio_assemble, audio_cleanup

    monkeypatch.setattr(audio_budget, "WORK_BUDGET_BYTES", 1024)
    data = _wav(5.0)
    with pytest.raises(audio_cleanup.CleanupError):
        audio_cleanup._decode(data)
    with pytest.raises(audio_assemble.AssembleError):
        audio_assemble._decode(data, "voice")


async def test_heavy_jobs_run_one_at_a_time_in_a_thread():
    import threading

    state = {"now": 0, "peak": 0, "threads": set()}

    def work():
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        state["threads"].add(threading.current_thread())
        import time

        time.sleep(0.05)
        state["now"] -= 1
        return 1

    heavy_work._gate = None  # a fresh gate for this test's loop
    results = await asyncio.gather(*[heavy_work.run_heavy(work) for _ in range(4)])
    assert results == [1, 1, 1, 1]
    assert state["peak"] == heavy_work.HEAVY_CONCURRENCY
    assert threading.main_thread() not in state["threads"]
