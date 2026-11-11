"""Which routes of the app are offered as tools, which are deliberately not, and which nobody has decided about yet.

Every route under /api/v1 must be one of three things: claimed by a tool (its `routes`), listed in `exemptions` with a reason (staff,
public pages, account and money screens, and so on), or in the checked-in list of routes still to decide. The test compares them, so
a new route cannot be added to the app without deciding how it is offered, and the list of undecided routes can only shrink.

Run `python -m app.tools.coverage` to see what is still undecided, and add `--write` to refresh the checked-in list after covering some.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Iterable

from app.tools.exemptions import exempt_reason

_IGNORED_METHODS = {"HEAD", "OPTIONS"}
ALLOWLIST_PATH = os.path.join(os.path.dirname(__file__), "undecided_routes.txt")


def _walk(routes, prefix: str, found: set[str]) -> None:
    """Collect "METHOD /path" for every API route, following included routers (which hold their own prefix and routes)."""
    from fastapi.routing import APIRoute

    for route in routes:
        if isinstance(route, APIRoute):
            for method in route.methods - _IGNORED_METHODS:
                found.add(f"{method} {prefix}{route.path}")
        elif hasattr(route, "original_router") and hasattr(route, "include_context"):
            _walk(route.original_router.routes, prefix + (route.include_context.prefix or ""), found)


def app_routes() -> set[str]:
    """Every "METHOD /path" the app serves under /api/v1, exactly as the app declares it."""
    from app.main import app

    found: set[str] = set()
    _walk(app.routes, "", found)
    return {r for r in found if r.split(" ", 1)[1].startswith("/api/v1/")}


def claimed_routes() -> dict[str, str]:
    """Route -> the tool that claims it. A route claimed twice is an error the test reports."""
    from app.tools.registry import get_registry

    claimed: dict[str, str] = {}
    for tool in get_registry().all():
        for route in tool.routes:
            claimed.setdefault(route, tool.name)
    return claimed


def undecided(routes: Iterable[str] | None = None) -> list[str]:
    """Routes that are neither claimed by a tool nor exempt, sorted."""
    served = set(routes) if routes is not None else app_routes()
    claimed = claimed_routes()
    return sorted(r for r in served if r not in claimed and exempt_reason(r) is None)


def read_allowlist() -> list[str]:
    if not os.path.exists(ALLOWLIST_PATH):
        return []
    with open(ALLOWLIST_PATH, encoding="utf8") as handle:
        return [line.strip() for line in handle if line.strip() and not line.startswith("#")]


def write_allowlist(items: list[str]) -> None:
    with open(ALLOWLIST_PATH, "w", encoding="utf8", newline="\n") as handle:
        handle.write("# Routes not yet claimed by a tool or exempted. This list may only shrink (see app/tools/coverage.py).\n")
        handle.write("\n".join(items) + ("\n" if items else ""))


def _group(route: str) -> str:
    match = re.match(r"^[A-Z]+ /api/v1/([^/]+)", route)
    return match.group(1) if match else "other"


if __name__ == "__main__":
    left = undecided()
    if "--write" in sys.argv:
        write_allowlist(left)
        print(f"wrote {len(left)} undecided routes")
    else:
        by_group: dict[str, int] = {}
        for route in left:
            by_group[_group(route)] = by_group.get(_group(route), 0) + 1
        print(f"{len(left)} undecided routes")
        for group, count in sorted(by_group.items(), key=lambda kv: -kv[1]):
            print(f"  {count:4}  {group}")
