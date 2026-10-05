"""Non-Latin scripts on pictures use their own fonts, and every picture prompt lives in a prompt file."""
import os
from pathlib import Path

import pytest

from app.pipelines.media import image_render
from app.prompts.registry import load_prompt

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "app" / "prompts" / "media"


def _variables_of(name: str) -> dict:
    """Every variable a prompt file uses, each given a value, so the file can be filled in."""
    from jinja2 import meta

    from app.prompts.registry import _env

    source = (_PROMPT_DIR / f"{name}.jinja").read_text(encoding="utf-8")
    return {var: "X" for var in meta.find_undeclared_variables(_env.parse(source))}



def test_tamil_and_hindi_text_pick_their_own_fonts_and_latin_keeps_the_brand_font():
    tamil = image_render.script_font_entry("வணக்கம் world")
    hindi = image_render.script_font_entry("नमस्ते")
    assert tamil and "Tamil" in tamil["regular"] and "Tamil" in tamil["bold"]
    assert hindi and "Devanagari" in hindi["regular"] and "Devanagari" in hindi["bold"]
    assert image_render.script_font_entry("Clarity beats scale") is None
    assert image_render.script_font_entry("") is None
    assert image_render.script_font_entry(None) is None


def test_the_script_fonts_exist_on_disk_and_load():
    for text in ("வணக்கம்", "नमस्ते"):
        entry = image_render.script_font_entry(text)
        for weight in ("regular", "bold"):
            assert os.path.exists(os.path.join(image_render.fonts_dir(), entry[weight])), entry[weight]
        font = image_render._load_font("inter", 40, bold=True, text=text)
        assert font.getlength(text) > 0


def test_every_picture_prompt_is_a_non_empty_prompt_file():
    names = sorted(p.stem for p in _PROMPT_DIR.glob("image_*.jinja"))
    assert len(names) >= 18, names
    for name in names:
        text = load_prompt(f"media/{name}", **_variables_of(name))
        assert isinstance(text, str) and text.strip(), name
        assert "{{" not in text and "{%" not in text, f"{name} left a placeholder unfilled"


@pytest.mark.parametrize("name", ["image_no_text", "image_screen_suffix"])
def test_the_no_writing_prompt_pieces_carry_the_rule(name):
    text = load_prompt(f"media/{name}", **_variables_of(name)).lower()
    assert any(word in text for word in ("text", "letters", "words", "writing")), name
