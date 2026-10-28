"""Every prompt file is cut into locked and editable sections by marker lines, and the markers change nothing.

The markers are template comments. Jinja drops them, so a marked file must compile to the same template as the file with
its marker lines removed. The prompt list (app/prompts/manifest.json) must match the files and the code exactly.
"""

import json
import re
import sys
from pathlib import Path

import pytest

from app.prompt_library.regions import (
    compose,
    find_slots,
    has_markers,
    parse_regions,
    strip_markers,
    token_stream,
)
from app.prompts.registry import _env

ROOT = Path(__file__).resolve().parent.parent
PROMPTS = ROOT / "app" / "prompts"
PATHS = sorted(p.relative_to(PROMPTS).as_posix()[:-6] for p in PROMPTS.rglob("*.jinja"))
MANIFEST = json.loads((PROMPTS / "manifest.json").read_text(encoding="utf-8"))["prompts"]
ID = re.compile(r"^[a-z0-9_]+$")


def _text(path: str) -> str:
    return (PROMPTS / f"{path}.jinja").read_text(encoding="utf-8").replace("\r\n", "\n")


def test_every_prompt_file_is_in_the_prompt_list_and_nothing_else_is():
    assert sorted(MANIFEST) == PATHS


@pytest.mark.parametrize("path", PATHS)
def test_markers_change_nothing_the_template_engine_reads(path):
    text = _text(path)
    assert has_markers(text), f"{path} has no section markers"
    plain = strip_markers(text)
    assert token_stream(_env, text) == token_stream(_env, plain)
    assert compose(parse_regions(text)) == plain


@pytest.mark.parametrize("path", PATHS)
def test_sections_match_the_class(path):
    regions = parse_regions(_text(path))
    ids = [r.id for r in regions]
    assert len(ids) == len(set(ids)), f"{path} repeats a section id"
    assert all(ID.match(i) for i in ids), ids
    assert not any(r.id.startswith("outside_") for r in regions), f"{path} has text outside the markers"
    klass = MANIFEST[path]["klass"]
    editable = [r for r in regions if r.kind == "editable"]
    if klass == "dev":
        assert not editable, f"{path} is Dev and must have no editable section"
    else:
        assert editable, f"{path} is {klass} and needs at least one editable section"
    for r in editable:
        assert r.label.strip(), f"{path}: editable section {r.id} needs a label"


@pytest.mark.parametrize("path", PATHS)
def test_protected_text_is_really_in_the_file(path):
    plain = strip_markers(_text(path))
    for literal in MANIFEST[path]["protectedLiterals"]:
        assert literal in plain, (path, literal)


def test_every_untrusted_fence_is_protected():
    for path in PATHS:
        plain = strip_markers(_text(path))
        for m in re.finditer(r"\{\{-?[^{}]*\|\s*untrusted\([^{}]*\)[^{}]*-?\}\}", plain):
            assert m.group(0) in MANIFEST[path]["protectedLiterals"], (path, m.group(0))


def test_the_prompt_list_is_up_to_date_with_the_files_and_the_code():
    """Fails when a prompt file, its class or a call that renders it changed without re-running the export script:
    .venv/Scripts/python.exe scripts/export_prompt_library_mock.py manifest"""
    sys.path.insert(0, str(ROOT / "scripts"))
    import export_prompt_library_mock as export

    fresh = json.loads(export.manifest_text(export.build_items()))["prompts"]
    stale = sorted(p for p in set(fresh) | set(MANIFEST) if fresh.get(p) != MANIFEST.get(p))
    assert not stale, f"app/prompts/manifest.json is out of date for: {stale[:10]}"


def test_variables_the_code_passes_are_listed_for_fragments_too():
    """Prompts read through render_fragment are listed with what the code passes them, like load_prompt ones."""
    receives = {r["name"] for r in MANIFEST["content_guard/screen"]["receives"]}
    assert receives, "content_guard/screen is rendered with variables but none are listed"
    assert set(find_slots(strip_markers(_text("content_guard/screen")))) <= receives | {"text"}
