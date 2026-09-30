"""Export file names and layout rules (app.pipelines.export.naming). Pure."""

import pytest

from app.pipelines.export import naming as n


def test_the_required_pattern():
    assert n.export_filename("Why systems beat willpower", "LinkedIn", "text", "post", "txt") == \
        "why-systems-beat-willpower_linkedin_text_post.txt"


def test_media_keeps_its_native_format():
    assert n.export_filename("Launch card", "Instagram", "image", "card", "png").endswith("_image_card.png")
    assert n.export_filename("Episode", None, "audio", "narration", "mp3").endswith("_library_audio_narration.mp3")


@pytest.mark.parametrize("mime,ext", [
    ("image/png", "png"), ("image/jpeg", "jpg"), ("audio/mpeg", "mp3"), ("audio/wav", "wav"),
    ("audio/x-wav", "wav"), ("video/mp4", "mp4"), ("video/quicktime", "mov"), ("audio/mpeg; charset=x", "mp3"),
])
def test_extensions_follow_the_real_type(mime, ext):
    assert n.extension_for(mime) == ext


def test_unknown_types_do_not_crash():
    assert n.extension_for("application/x-strange") == "bin"  # subtype too long to trust
    assert n.extension_for(None) == "bin"
    assert n.extension_for("nonsense") == "bin"
    assert n.extension_for("text/plain") == "plain"


def test_slugs_are_safe_for_any_file_system():
    assert n.slugify('  A: "weird" / title? <ok> | *  ') == "a-weird-title-ok"
    assert n.slugify("") == "untitled"
    assert n.slugify(None) == "untitled"
    assert n.slugify("CON") == "untitled"
    assert n.slugify("../../etc/passwd") == "etc-passwd"  # no path characters survive
    assert "/" not in n.slugify("a/b\\c") and "." not in n.slugify("a.b")
    assert len(n.slugify("x" * 500)) <= n.MAX_TITLE_CHARS


def test_non_latin_titles_are_kept_not_emptied():
    assert n.slugify("எப்படி இருக்கீங்க") != "untitled"
    assert n.slugify("नमस्ते दुनिया") != "untitled"


def test_repeated_names_get_a_number():
    assert n.unique_names(["a.txt", "b.txt", "a.txt", "A.txt"]) == ["a.txt", "b.txt", "a-2.txt", "A-3.txt"]
    assert n.unique_names([]) == []


def test_title_comes_from_the_first_non_empty_line():
    assert n.first_line_title("\n\n  Hello world \nsecond") == "Hello world"
    assert n.first_line_title("") == "untitled"
    assert n.first_line_title(None, "x") == "x"
