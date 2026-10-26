"""Prompts are given a language as words, never as a bare code such as "ta+en"."""

from app.shared.localized_strings import language_label


def test_plain_code_becomes_the_language_name():
    assert language_label("ta") == "Tamil"


def test_mixed_register_reads_as_words_in_either_order():
    assert language_label("ta+en") == "Tamil written in English letters, blended with English"
    assert language_label("en+hi") == "Hindi written in English letters, blended with English"


def test_no_code_is_left_bare():
    assert "+" not in language_label("hi+en")
