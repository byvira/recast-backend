"""What a member is told about a finished render: the measured numbers, and plain notes. The worker's staff-facing warnings are not
passed on as they are."""
from app.video import jobs


def _report(**over):
    base = {"passed": True, "duration_s": 61.2, "width": 1080, "height": 1920, "fps": 30.0, "loudness_lufs": -14.2, "true_peak_dbfs": -1.9,
            "black_seconds": 0.0, "frozen_seconds": 0.0, "warnings": [], "gate_version": 3}
    base.update(over)
    return base


def test_a_clean_report_has_the_numbers_and_no_notes():
    q = jobs.quality_summary(_report(), -14.0)
    assert q["passed"] is True and q["width"] == 1080 and q["loudness_lufs"] == -14.2 and q["notes"] == []
    assert "gate_version" not in q and "warnings" not in q


def test_what_is_worth_a_look_is_said_in_plain_words():
    q = jobs.quality_summary(_report(black_seconds=2.5, frozen_seconds=4.0, loudness_lufs=-9.0, warnings=["the sound track is silent"]), -14.0)
    text = " ".join(q["notes"])
    assert "2.5 seconds of the picture are black" in text and "4.0 seconds of the picture do not move" in text
    assert "sound track is silent" in text and "-9.0, a little away from the -14 you chose" in text


def test_a_report_that_is_missing_or_odd_gives_nothing_rather_than_a_guess():
    assert jobs.quality_summary(None) is None and jobs.quality_summary("fine") is None
    q = jobs.quality_summary({"passed": "yes", "loudness_lufs": "loud"})
    assert q["passed"] is False and q["loudness_lufs"] is None


def test_a_finished_job_carries_its_quality_and_target_and_a_running_one_does_not():
    done = {"id": "j", "status": "done", "report": _report(), "target_lufs": -16.0, "output": {"url": "https://x/v.mp4", "bytes": 1}}
    out = jobs.public(done)
    assert out["quality"]["passed"] is True and out["target_lufs"] == -16.0
    running = {"id": "j", "status": "claimed", "report": _report(), "target_lufs": -16.0}
    assert "quality" not in jobs.public(running)
