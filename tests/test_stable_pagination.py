"""Every list that pages with skip sorts on a unique tiebreaker as well, so rows that share a timestamp are never repeated or skipped between pages."""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
#: Sorting on this alone is already unique (it picks old saved versions of one project to delete; it is not a page of a list).
UNIQUE_ALONE = {"version"}


def _chain(call: ast.Call) -> list[ast.Call]:
    calls, node = [], call
    while isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        calls.append(node)
        node = node.func.value
        if isinstance(node, ast.Await):
            node = node.value
    return calls


def _problem(call: ast.Call) -> str | None:
    sorts = [c for c in _chain(call) if c.func.attr == "sort"]
    if not sorts:
        return "pages with skip and no sort"
    first = sorts[0].args[0] if sorts[0].args else None
    if isinstance(first, (ast.List, ast.Tuple)) and len(first.elts) >= 2:
        return None
    if isinstance(first, ast.Constant) and first.value in UNIQUE_ALONE:
        return None
    return "pages with skip on a sort that is not unique"


def test_every_paged_list_has_a_unique_tiebreaker():
    found = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "skip":
                problem = _problem(node)
                if problem:
                    found.append(f"{path.relative_to(APP)}:{node.lineno} {problem}")
    assert not found, "\n".join(found)
