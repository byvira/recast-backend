"""The member's data copy never carries a secret, and the route is only for the signed-in member."""
from datetime import datetime, timezone

from app.shared.account_export import _clean


def test_secrets_and_storage_ids_are_dropped_at_every_level():
    row = {
        "_id": "x", "id": "u1", "name": "Asha", "totp_secret": "s", "recovery_codes": ["a"], "password_hash": "h",
        "settings": {"theme": "dark", "api_key": "k", "nested": [{"endpoint": "https://push", "label": "ok"}]},
    }
    out = _clean(row)
    assert out == {"id": "u1", "name": "Asha", "settings": {"theme": "dark", "nested": [{"label": "ok"}]}}


def test_dates_become_text():
    assert _clean({"at": datetime(2026, 1, 2, tzinfo=timezone.utc)}) == {"at": "2026-01-02T00:00:00+00:00"}


def test_route_exists_once_and_needs_sign_in():
    from app.api.v1.users import router

    hits = [r for r in router.routes if getattr(r, "path", "") == "/me/export"]
    assert len(hits) == 1 and "GET" in hits[0].methods
    assert any(d.call.__name__ == "get_current_user" for d in hits[0].dependant.dependencies)
