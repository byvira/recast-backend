"""The request log never writes sensitive query values: only keys known to be harmless keep their value."""

from starlette.datastructures import QueryParams

from app.core.middleware import safe_query


def test_oauth_callback_code_and_state_are_redacted():
    out = safe_query(QueryParams("code=abc123secret&state=xyz789"))
    assert "abc123secret" not in out and "xyz789" not in out
    assert out == "code=[redacted]&state=[redacted]"


def test_stream_source_text_is_redacted():
    out = safe_query(QueryParams("source_text=My%20private%20draft&platform=linkedin"))
    assert "private" not in out
    assert "platform=linkedin" in out


def test_harmless_paging_and_filters_keep_their_values():
    assert safe_query(QueryParams("page=2&limit=20&sort=newest")) == "page=2&limit=20&sort=newest"


def test_a_long_value_on_a_safe_key_is_redacted():
    assert safe_query(QueryParams("cursor=" + "x" * 200)) == "cursor=[redacted]"


def test_repeated_keys_are_each_handled():
    assert safe_query(QueryParams("platforms=x&platforms=linkedin&email=a@b.co")) == "platforms=x&platforms=linkedin&email=[redacted]"
