"""Boxes (picture-in-picture, split screen), rounded corners and transitions on video clips, in the edit list and in what the worker is sent."""
import pytest

from app.video import edl


def _edit(**clip_b):
    e = edl.blank("landscape")
    e["video"] = [{"id": "a", "media_id": "m1", "at_f": 0, "dur_f": 100}, {"id": "b", "media_id": "m2", "at_f": 82, "dur_f": 60, **clip_b}]
    e["duration_f"] = 142
    return e


def test_a_clip_is_full_frame_with_no_transition_unless_it_says_otherwise():
    clip = edl.validate(_edit())["video"][1]
    assert clip["box"] == {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0} and clip["radius"] == 0.0 and clip["transition_in"] == {"kind": "none", "dur_f": 0}


def test_a_box_a_radius_and_a_transition_are_kept():
    clip = edl.validate(_edit(box={"x": 0.5, "y": 0.0, "w": 0.5, "h": 0.5}, radius=0.2, transition_in={"kind": "slide_left", "dur_f": 18}))["video"][1]
    assert clip["box"] == {"x": 0.5, "y": 0.0, "w": 0.5, "h": 0.5} and clip["radius"] == 0.2 and clip["transition_in"] == {"kind": "slide_left", "dur_f": 18}


@pytest.mark.parametrize("bad,fragment", [
    ({"box": {"x": 0.8, "w": 0.5}}, "outside the picture"),
    ({"box": {"w": 0.01}}, "box"),
    ({"transition_in": {"kind": "spin", "dur_f": 5}}, "must be one of"),
    ({"transition_in": {"kind": "dissolve", "dur_f": 0}}, "transition"),
    ({"transition_in": {"kind": "dissolve", "dur_f": 61}}, "longer than the clip"),
    ({"radius": 0.9}, "radius"),
])
def test_a_bad_box_radius_or_transition_is_refused_in_plain_words(bad, fragment):
    with pytest.raises(edl.EditError) as err:
        edl.validate(_edit(**bad))
    assert fragment in str(err.value)


def test_the_worker_is_sent_the_box_the_radius_and_the_transition_with_the_clip_named_by_its_input():
    edit = edl.validate(_edit(box={"x": 0.5, "y": 0.0, "w": 0.5, "h": 0.5}, transition_in={"kind": "wipe_right", "dur_f": 12}))
    media = {"m1": {"url": "https://x/1.mp4", "kind": "video"}, "m2": {"url": "https://x/2.mp4", "kind": "video"}}

    job = edl.to_job(edit, media, {"crf": 21})

    sent = job["edit"]["video"][1]
    assert sent["src"] != "m2" and sent["box"]["x"] == 0.5 and sent["transition_in"] == {"kind": "wipe_right", "dur_f": 12}
