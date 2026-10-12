"""Sound checks measure a real recording with ffmpeg, so these run the real measurement on synthetic audio."""
import io

import numpy as np
import soundfile as sf

from app.pipelines.audio.sound_checks import run_sound_checks

SR = 16000


def _wav(samples: np.ndarray) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, samples.astype("float32"), SR, format="WAV")
    return buf.getvalue()


def _by_id(checks: list[dict]) -> dict:
    return {c["id"]: c for c in checks}


async def test_sound_checks_measure_a_real_recording():
    t = np.arange(SR * 10) / SR
    tone = np.sin(2 * np.pi * 200 * t) * 0.1
    tone[SR * 4: SR * 8] = 0  # a four second gap in the middle
    checks = _by_id(await run_sound_checks(_wav(tone)))

    assert set(checks) == {"loudness", "peaks", "silences"}
    assert checks["loudness"]["status"] == "attention"  # about -24 LUFS: quiet
    assert checks["peaks"]["status"] == "good"
    assert checks["silences"]["status"] == "attention"
    assert "4" in checks["silences"]["message"]


async def test_sound_checks_say_not_checked_for_a_file_that_cannot_be_read():
    checks = _by_id(await run_sound_checks(b"this is not audio"))
    assert {c["status"] for c in checks.values()} == {"unknown"}


def test_loudness_is_judged_against_the_standard_the_member_chose():
    from app.pipelines.audio.sound_checks import judge

    metrics = {"loudness_lufs": -23.4, "peak_db": -3.0, "silences": [], "measured": True}
    assert _by_id(judge(metrics))["loudness"]["status"] == "attention"          # quiet for a podcast
    on_target = _by_id(judge(metrics, target_lufs=-23.0))["loudness"]
    assert on_target["status"] == "good" and "-23" in on_target["message"]       # right for broadcast
    off = _by_id(judge({**metrics, "loudness_lufs": -17.0}, target_lufs=-23.0))["loudness"]
    assert off["status"] == "attention" and "leveled it to -23" in off["message"]
