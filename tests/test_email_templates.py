"""The ops and status emails go out as templates, and fall back to the plain version when a template cannot be sent. No email is sent."""
import resend

from app.core import notifications
from app.health import errors, status
from app.shared.llm_health import alerts


def _alert(**over):
    base = dict(key="k", kind="new_issue", severity="high", subject="New error #18 (Server): Boom",
                body="First seen POST /request-otp.\nRelease local.\nhttps://x.test/ops/errors?issue=18")
    return alerts.Alert(**{**base, **over})


def test_an_alert_becomes_the_fields_of_its_template():
    v = alerts.alert_variables(_alert())
    assert v["HEADLINE"] == "New error #18 (Server): Boom" and v["KIND"] == "New problem" and v["SEVERITY"] == "high"
    assert v["DETAILS"] == "First seen POST /request-otp. Release local."
    assert v["LINK_URL"] == "https://x.test/ops/errors?issue=18" and v["LINK_LABEL"]


def test_an_alert_with_no_link_has_no_link_fields():
    v = alerts.alert_variables(_alert(kind="provider_down", body="Most recent calls failed."))
    assert v["LINK_URL"] == "" and v["LINK_LABEL"] == "" and v["DETAILS"] == "Most recent calls failed."


def test_the_morning_note_fields_are_always_filled_in():
    quiet = alerts.digest_variables([], {}, 0, "")
    assert all(quiet[k] for k in ("OPEN_SUMMARY", "OPEN_LIST", "DOWN_PROVIDERS", "FAILED_CALLS", "OPEN_COUNT"))
    busy = alerts.digest_variables([{"number": 3, "priority": "high", "title": "x"}], {"groq": "down"}, 4, "https://x.test")
    assert busy["OPEN_COUNT"] == "1" and busy["OPEN_SUMMARY"] == "1 high" and busy["DOWN_PROVIDERS"] == "Groq"
    assert busy["FAILED_CALLS"] == "4" and busy["LINK_URL"] == "https://x.test/ops/llm"


async def test_an_ops_email_uses_the_template_and_falls_back_to_plain_for_an_address_it_could_not_reach(monkeypatch):
    sent_templates: list[tuple] = []
    plain: list[dict] = []

    async def fake_templated(template, to, variables, from_override=None, **kw):
        sent_templates.append((template, to, kw.get("subject")))
        return to != "bad@x.test"

    monkeypatch.setattr(notifications, "send_templated_email", fake_templated)
    monkeypatch.setattr(resend.Emails, "send", staticmethod(lambda payload: plain.append(payload)))
    monkeypatch.setattr("app.core.config.settings.RESEND_API_KEY", "key")

    ok = await alerts._send_email(["good@x.test", "bad@x.test"], "Subject", "Body", template="ops-alert", variables={"A": "b"})
    assert ok is True
    assert [t[:2] for t in sent_templates] == [("ops-alert", "good@x.test"), ("ops-alert", "bad@x.test")]
    assert sent_templates[0][2] == "[Ekavo] Subject"
    assert len(plain) == 1 and plain[0]["to"] == ["bad@x.test"]                  # only the address the template missed


async def test_an_ops_email_with_no_template_is_still_plain(monkeypatch):
    plain: list[dict] = []
    monkeypatch.setattr(resend.Emails, "send", staticmethod(lambda payload: plain.append(payload)))
    monkeypatch.setattr("app.core.config.settings.RESEND_API_KEY", "key")
    assert await alerts._send_email(["a@x.test"], "S", "B") is True
    assert plain and plain[0]["subject"] == "[Ekavo] S"


async def test_the_person_an_error_was_given_to_gets_the_assigned_template(monkeypatch):
    got: dict = {}

    async def fake_send(to, subject, body, **kw):
        got.update(to=to, kw=kw)
        return True

    monkeypatch.setattr(alerts, "_send_email", fake_send)
    await errors.notify_assignee({"number": 18, "title": "Boom"}, "ana@x.test", "Vinod")
    assert got["kw"]["template"] == "ops-assigned"
    assert got["kw"]["variables"]["ERROR_NUMBER"] == "18" and got["kw"]["variables"]["BY"] == "Vinod"


async def test_a_status_email_tries_its_template_first_and_falls_back_to_plain(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.ENVIRONMENT", "production")
    monkeypatch.setattr("app.core.config.settings.RESEND_API_KEY", "key")
    calls: list[str] = []
    plain: list[dict] = []
    works = {"template": True}

    async def fake_templated(template, to, variables, from_override=None, **kw):
        calls.append(template)
        return works["template"]

    monkeypatch.setattr(notifications, "send_templated_email", fake_templated)
    monkeypatch.setattr(resend.Emails, "send", staticmethod(lambda payload: plain.append(payload)))

    assert await status._send("a@x.test", "S", "T", template=status.CONFIRM_TEMPLATE, variables={"CONFIRM_URL": "u"})
    assert calls == ["status-confirm"] and plain == []
    works["template"] = False
    assert await status._send("a@x.test", "S", "T", template=status.UPDATE_TEMPLATE, variables={})
    assert calls[-1] == "status-update" and len(plain) == 1
