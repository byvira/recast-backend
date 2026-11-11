"""Every route of the app is a tool, deliberately left out with a reason, or on the checked-in list of routes still to decide.

The list (app/tools/undecided_routes.txt) may only shrink: covering or exempting a route removes it, and a new route that nobody has
decided about fails here. When the list is empty, every route has been decided.
"""

import collections

from app.tools import coverage
from app.tools.contract import Risk
from app.tools.exemptions import RULES, exempt_reason
from app.tools.registry import get_registry


def test_every_route_is_a_tool_or_exempt_or_on_the_shrinking_list():
    left = coverage.undecided()
    listed = coverage.read_allowlist()
    new = sorted(set(left) - set(listed))
    done = sorted(set(listed) - set(left))
    assert not new, (
        f"{len(new)} route(s) are not a tool, not exempt and not on the list. Cover them with a tool, add an exemption with a reason, "
        f"or (only for a deliberate wait) add them to the list: {new[:15]}"
    )
    assert not done, (
        f"{len(done)} route(s) on the list are now covered or exempt. Remove them (python -m app.tools.coverage --write): {done[:15]}"
    )


def test_every_route_a_tool_claims_exists_and_is_claimed_once():
    served = coverage.app_routes()
    claims = collections.defaultdict(list)
    for tool in get_registry().all():
        for route in tool.routes:
            claims[route].append(tool.name)
    missing = sorted(r for r in claims if r not in served)
    coarse = {tool.name for tool in get_registry().all() if tool.coarse}
    # a route several coarse tools share (one save route behind many named outcomes) is allowed; any other double claim is not
    twice = {r: names for r, names in claims.items() if len(names) > 1 and not set(names) <= coarse}
    assert not missing, f"tools claim routes the app does not serve: {missing[:10]}"
    assert not twice, f"routes claimed by more than one tool: {dict(list(twice.items())[:10])}"


def test_a_tool_never_claims_a_route_that_is_kept_out_on_purpose():
    for tool in get_registry().all():
        for route in tool.routes:
            assert exempt_reason(route) is None, f"{tool.name} claims {route}, which is exempt: {exempt_reason(route)}"


def test_a_read_tool_claims_only_reads():
    for tool in get_registry().all():
        if tool.risk is Risk.READ:
            assert all(route.startswith("GET ") for route in tool.routes), tool.name


def test_every_exemption_rule_still_matches_a_route():
    served = coverage.app_routes()
    def matches(route: str, method: str, prefix: str) -> bool:
        verb, path = route.split(" ", 1)
        if method not in ("*", verb):
            return False
        return path == prefix[:-1] if prefix.endswith("$") else path.startswith(prefix)

    for method, prefix, reason in RULES:
        assert any(matches(r, method, prefix) for r in served), f"the exemption for {prefix!r} ({reason}) matches no route; remove it"
