"""Reads the code itself. A name used in a module but defined nowhere in it (a variable renamed in one place and left in another)
compiles and imports fine, and only fails when that line runs, which may be a path no other test reaches. This finds every one."""
import ast
import builtins
import pathlib
import warnings

APP = pathlib.Path(__file__).resolve().parent.parent / "app"
_KNOWN = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__spec__", "__path__", "__package__"}


def _undefined(tree: ast.AST) -> set[str]:
    defined: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(n.name)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            defined.add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            defined.update((a.asname or a.name).split(".")[0] for a in n.names)
        elif isinstance(n, ast.arg):
            defined.add(n.arg)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            defined.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            defined.update(n.names)
        elif isinstance(n, ast.MatchAs) and n.name:
            defined.add(n.name)
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return used - defined - _KNOWN


def test_no_module_uses_a_name_it_never_defines():
    found = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        for path in sorted(APP.rglob("*.py")):
            missing = _undefined(ast.parse(path.read_text(encoding="utf-8")))
            if missing:
                found.append(f"{path.relative_to(APP.parent).as_posix()}: {sorted(missing)}")
    assert found == []
