from app.pipelines.text.angle_sources import text_for_angles


def test_deck_slide_text_in_any_stored_shape_is_read():
    deck = {
        "title": "Why We Feel Behind",
        "slides": [
            {"text_content": {"headline": "Slide one", "body": ["first point", "second point"]}},
            {"text_content": ["a line", {"text": "nested"}]},
            {"text_content": "plain words"},
            {"text_content": None},
            None,
            "not a slide",
        ],
        "og_description": None,
        "alt_text": 5,
    }
    out = text_for_angles("image", deck)
    assert out.startswith("Why We Feel Behind")
    for expected in ("Slide one", "first point", "second point", "a line", "nested", "plain words"):
        assert expected in out


def test_deck_without_words_gives_nothing():
    assert text_for_angles("image", {"title": "x", "slides": [{"text_content": {}}]}) is None
    assert text_for_angles("image", {"slides": "broken"}) is None


def test_recording_uses_script_then_transcript():
    assert "hello" in text_for_angles("audio", {"title": "T", "script": "hello there"})
    spoken = text_for_angles("audio", {"transcript": [{"word": "one"}, {"word": "two"}, "junk"]})
    assert spoken == "one two"
