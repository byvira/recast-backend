"""A web address saved without https:// still builds working links (an approval link on the wrong host came from exactly this)."""
from app.core.config import Settings


def _settings(**kw):
    # the two values every settings object must have are given here, so no real .env is read
    return Settings(_env_file=None, SECRET_KEY="test-secret-key-for-settings-only", REDIS_URL="redis://localhost:6379/0", **kw)


def test_an_address_with_no_scheme_gets_https_and_loses_a_trailing_slash():
    s = _settings(FRONTEND_URL="ekavo.byvirastudio.com/", API_PUBLIC_URL="recast-api.byvirastudio.com")
    assert s.FRONTEND_URL == "https://ekavo.byvirastudio.com"
    assert s.API_PUBLIC_URL == "https://recast-api.byvirastudio.com"


def test_a_full_address_is_kept_and_http_for_local_is_not_changed():
    s = _settings(FRONTEND_URL="http://localhost:3000", API_PUBLIC_URL="https://api.example.com/")
    assert s.FRONTEND_URL == "http://localhost:3000"
    assert s.API_PUBLIC_URL == "https://api.example.com"


def test_the_approval_link_is_built_on_the_frontend_host(monkeypatch):
    from app.tools import oauth
    s = _settings(FRONTEND_URL="ekavo.byvirastudio.com")
    link = f"{s.FRONTEND_URL.rstrip('/')}/oauth/authorize?request=abc"
    assert link == "https://ekavo.byvirastudio.com/oauth/authorize?request=abc"
    assert oauth is not None
