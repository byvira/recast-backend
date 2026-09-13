"""Tests for app.prompts.registry.load_prompt — the sole entry point every
migrated prompt template is rendered through.
"""

import pytest
from jinja2.exceptions import UndefinedError

from app.prompts.registry import load_prompt


def test_load_prompt_renders_known_variables_exactly():
    result = load_prompt("fixtures/_test_fixture", name="Ada", score=42)
    assert result == "Hello Ada, your score is 42.\n"


def test_load_prompt_raises_on_missing_variable():
    with pytest.raises(UndefinedError):
        load_prompt("fixtures/_test_fixture", name="Ada")
