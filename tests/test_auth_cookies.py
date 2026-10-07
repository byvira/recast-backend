"""How the sign-in cookies are set and cleared in each environment."""
from fastapi import Response

from app.core import auth
from app.core.config import settings


def _cookies(response: Response) -> list[str]:
    return [value.decode() for key, value in response.raw_headers if key == b"set-cookie"]


def _set(monkeypatch, environment: str, samesite: str = "none", domain: str = "") -> list[str]:
    monkeypatch.setattr(settings, "ENVIRONMENT", environment)
    monkeypatch.setattr(settings, "COOKIE_SAMESITE", samesite)
    monkeypatch.setattr(settings, "COOKIE_DOMAIN", domain)
    response = Response()
    auth.set_auth_cookies(response, "a", "r")
    return _cookies(response)


def test_production_cookies_are_secure_and_httponly_and_default_to_none(monkeypatch):
    cookies = _set(monkeypatch, "production")
    assert len(cookies) == 2
    for cookie in cookies:
        text = cookie.lower()
        assert "secure" in text and "httponly" in text and "samesite=none" in text and "domain=" not in text


def test_production_can_use_lax_once_the_site_and_api_share_a_domain(monkeypatch):
    for cookie in _set(monkeypatch, "production", samesite="Lax"):
        assert "samesite=lax" in cookie.lower() and "secure" in cookie.lower()


def test_an_unknown_value_falls_back_to_none_and_the_domain_is_used_only_when_set(monkeypatch):
    assert all("samesite=none" in c.lower() for c in _set(monkeypatch, "production", samesite="sideways"))
    assert all("domain=.example.com" in c.lower() for c in _set(monkeypatch, "production", domain=".example.com"))


def test_outside_production_cookies_work_over_plain_http(monkeypatch):
    for cookie in _set(monkeypatch, "development", samesite="none", domain=".example.com"):
        text = cookie.lower()
        assert "samesite=lax" in text and "secure" not in text and "domain=" not in text


def test_clearing_uses_the_same_attributes_as_setting(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "COOKIE_SAMESITE", "lax")
    monkeypatch.setattr(settings, "COOKIE_DOMAIN", "")
    response = Response()
    auth.clear_auth_cookies(response)
    for cookie in _cookies(response):
        text = cookie.lower()
        assert "max-age=0" in text and "samesite=lax" in text and "secure" in text
