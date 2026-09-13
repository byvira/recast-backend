"""Tests for the shared Resend template-email helper.

Covers the production-only guard in send_templated_email — every notification
in the app goes through this one function, so a bug here would silently
affect every email the product sends.
"""

import sys
import types

import pytest

from app.core.config import settings
from app.core.notifications import OPS_FROM, send_templated_email


@pytest.fixture(autouse=True)
def _restore_environment():
    original = settings.ENVIRONMENT
    yield
    settings.ENVIRONMENT = original


def _install_fake_resend(monkeypatch, calls):
    fake_resend = types.SimpleNamespace(
        api_key=None,
        Emails=types.SimpleNamespace(send=lambda payload: calls.append(payload)),
    )
    monkeypatch.setitem(sys.modules, "resend", fake_resend)


async def test_dev_mode_never_calls_resend(monkeypatch):
    """In non-production, resend must not be touched at all."""
    settings.ENVIRONMENT = "development"
    calls = []
    _install_fake_resend(monkeypatch, calls)

    result = await send_templated_email(
        "otp-verification", "user@example.com", {"OTP_CODE": "123456"}
    )

    assert result is True
    assert calls == []


async def test_no_recipient_is_skipped_even_in_production(monkeypatch):
    """A falsy `to` short-circuits before the production check even matters."""
    settings.ENVIRONMENT = "production"
    calls = []
    _install_fake_resend(monkeypatch, calls)

    result = await send_templated_email("otp-verification", "", {"OTP_CODE": "123456"})

    assert result is False
    assert calls == []


async def test_production_mode_sends_template_payload(monkeypatch):
    settings.ENVIRONMENT = "production"
    calls = []
    _install_fake_resend(monkeypatch, calls)

    variables = {
        "WORKSPACE_NAME": "Acme",
        "OLD_ROLE": "editor",
        "NEW_ROLE": "admin",
        "CHANGED_BY_NAME": "Ada",
    }
    result = await send_templated_email("role-changed", "member@example.com", variables)

    assert result is True
    assert len(calls) == 1
    assert calls[0]["to"] == "member@example.com"
    assert calls[0]["template"] == {"id": "role-changed", "variables": variables}
    assert calls[0]["from"] == "Recast <onboarding@resend.dev>"


async def test_from_override_is_used_for_ops_templates(monkeypatch):
    settings.ENVIRONMENT = "production"
    calls = []
    _install_fake_resend(monkeypatch, calls)

    await send_templated_email(
        "publish-fatal-alert", "ops@example.com", {"PIECE_ID": "p1"}, from_override=OPS_FROM
    )

    assert calls[0]["from"] == OPS_FROM


async def test_delivery_failure_is_swallowed(monkeypatch):
    """A raising Resend call must return False, never propagate."""
    settings.ENVIRONMENT = "production"

    def _boom(payload):
        raise RuntimeError("resend is down")

    fake_resend = types.SimpleNamespace(api_key=None, Emails=types.SimpleNamespace(send=_boom))
    monkeypatch.setitem(sys.modules, "resend", fake_resend)

    result = await send_templated_email("otp-verification", "user@example.com", {"OTP_CODE": "1"})

    assert result is False
