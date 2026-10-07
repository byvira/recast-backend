from app.pipelines.text.generator import as_text


def test_plain_text_is_unchanged():
    assert as_text("Hello world") == "Hello world"


def test_missing_value_is_empty():
    assert as_text(None) == ""


def test_object_with_text_key_gives_the_text():
    assert as_text({"text": "The post", "extra": 1}) == "The post"


def test_object_without_known_key_joins_its_text_parts():
    assert as_text({"hook": "Line one", "body": "Line two"}) == "Line two"
    assert as_text({"hook": "Line one", "cta": "Line two"}) == "Line one\n\nLine two"


def test_list_of_parts_is_joined():
    assert as_text(["One", {"text": "Two"}]) == "One\n\nTwo"
