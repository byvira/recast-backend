"""Which address emails are sent from: the configured one when it is real, Resend's test sender otherwise."""
from app.core.notifications import TEST_SENDER, sender_address, sender_for


def test_a_real_address_is_used_plain_or_with_a_name():
    assert sender_address("hello@mail.byvirastudio.com") == "hello@mail.byvirastudio.com"
    assert sender_address("  Recast <hello@mail.byvirastudio.com>  ") == "hello@mail.byvirastudio.com"


def test_nothing_set_or_the_example_placeholder_uses_the_test_sender():
    assert sender_address("") == TEST_SENDER
    assert sender_address(None) == TEST_SENDER
    assert sender_address("noreply@yourdomain.com") == TEST_SENDER
    assert sender_address("Recast <noreply@YourDomain.com>") == TEST_SENDER
    assert sender_address("not an address") == TEST_SENDER


def test_the_sender_carries_its_display_name():
    assert sender_for("Recast", "hello@mail.byvirastudio.com") == "Recast <hello@mail.byvirastudio.com>"
    assert sender_for("Recast Ops", "hello@mail.byvirastudio.com") == "Recast Ops <hello@mail.byvirastudio.com>"
    assert sender_for("Recast", "") == f"Recast <{TEST_SENDER}>"


async def test_a_refused_sender_falls_back_to_the_test_sender_once(monkeypatch):
    import sys
    import types

    from app.core import notifications

    sent = []

    class _Emails:
        @staticmethod
        def send(payload):
            sent.append(payload["from"])
            if payload["from"].endswith("<hello@mail.example.com>"):
                raise RuntimeError("The domain is not verified")

    monkeypatch.setitem(sys.modules, "resend", types.SimpleNamespace(Emails=_Emails, api_key=""))
    monkeypatch.setattr(notifications.settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(notifications, "_DEFAULT_FROM", "Recast <hello@mail.example.com>")

    assert await notifications.send_templated_email("otp-verification", "member@example.com", {}) is True
    assert sent == ["Recast <hello@mail.example.com>", f"Recast <{notifications.TEST_SENDER}>"]


async def test_when_even_the_test_sender_fails_it_reports_failure_without_looping(monkeypatch):
    import sys
    import types

    from app.core import notifications

    sent = []

    class _Emails:
        @staticmethod
        def send(payload):
            sent.append(payload["from"])
            raise RuntimeError("down")

    monkeypatch.setitem(sys.modules, "resend", types.SimpleNamespace(Emails=_Emails, api_key=""))
    monkeypatch.setattr(notifications.settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(notifications, "_DEFAULT_FROM", f"Recast <{notifications.TEST_SENDER}>")
    assert await notifications.send_templated_email("otp-verification", "member@example.com", {}) is False
    assert len(sent) == 1
