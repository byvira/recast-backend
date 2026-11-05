"""The links the support assistant offers under a reply: worked out by rules from the member's own workspace, never by the model, and
only to pages the member can open themselves."""
from __future__ import annotations

from app.shared.support_actions import CONNECTIONS, MAX_ACTIONS, suggest_actions


def test_an_expired_connection_gets_a_reconnect_link():
    out = suggest_actions({"platforms": [{"platform": "instagram", "health": "expired"}, {"platform": "x", "health": "healthy"}]})
    assert out == [{"label": "Reconnect Instagram", "href": CONNECTIONS}]


def test_each_problem_points_at_the_screen_where_it_is_fixed():
    snapshot = {
        "platforms": [{"platform": "linkedin", "health": "disconnected"}],
        "workspace": {"generation_halted": True},
        "schedule": {"overdue": 2},
        "campaigns": [{"status": "paused"}],
        "errors": [{}, {}, {}],
    }
    out = suggest_actions(snapshot, out_of_credits=True)
    assert len(out) == MAX_ACTIONS, "never more than three links under one reply"
    assert out[0]["label"] == "Reconnect Linkedin" and out[1] == {"label": "See your credits and plan", "href": "/dashboard/settings?tab=billing"}
    assert all(a["href"].startswith("/dashboard/") for a in out), "only the member's own pages"


def test_a_healthy_workspace_gets_no_links_and_missing_data_never_breaks_it():
    assert suggest_actions({"platforms": [{"platform": "x", "health": "healthy"}], "schedule": {"overdue": 0}}) == []
    assert suggest_actions({}) == []
    assert suggest_actions({"platforms": None, "campaigns": None, "errors": None, "schedule": None, "workspace": None}) == []


def test_the_same_problem_on_two_accounts_is_two_links_but_never_the_same_one_twice():
    out = suggest_actions({"platforms": [{"platform": "x", "health": "expired"}, {"platform": "x", "health": "expired"}]})
    assert out == [{"label": "Reconnect X", "href": CONNECTIONS}]
