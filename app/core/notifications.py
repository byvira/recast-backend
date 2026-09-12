"""OTP notification delivery — Resend (email) and Twilio (SMS).

In ENVIRONMENT=development all delivery is short-circuited and the OTP is
printed to the console so engineers can test without real credentials.

In ENVIRONMENT=production the actual APIs are called.  Any exception is caught
and logged; the function returns False so the auth flow can surface a friendly
error rather than crashing.
"""

import logging

from app.core.config import settings

logger = logging.getLogger(__name__)

_EMAIL_SUBJECT = "Your verification code"
_EMAIL_HTML_TEMPLATE = """
<div style="font-family:sans-serif;max-width:480px;margin:auto">
  <h2>Your verification code</h2>
  <p style="font-size:32px;letter-spacing:8px;font-weight:bold">{otp}</p>
  <p>This code expires in {minutes} minutes. Do not share it with anyone.</p>
</div>
"""


async def send_otp_email(email: str, otp: str) -> bool:
    """Send an OTP to *email* via Resend.

    In development mode the OTP is printed to the console instead.

    Args:
        email: Recipient email address.
        otp: 6-digit OTP string.

    Returns:
        True on success, False on any delivery failure.
    """
    if settings.ENVIRONMENT != "production":
        print(f"\n[DEV MODE] OTP for {email}: {otp}\n", flush=True)
        logger.info("DEV MODE — OTP for %s: %s", email, otp)
        return True

    try:
        import resend  # type: ignore[import-untyped]

        resend.api_key = settings.RESEND_API_KEY
        resend.Emails.send(
            {
                "from": settings.EMAIL_FROM,
                "to": email,
                "subject": _EMAIL_SUBJECT,
                "html": _EMAIL_HTML_TEMPLATE.format(
                    otp=otp, minutes=settings.OTP_EXPIRE_MINUTES
                ),
            }
        )
        return True
    except Exception as exc:
        logger.error("Failed to send OTP email to %s: %s", email, exc)
        return False


async def send_otp_sms(phone: str, otp: str) -> bool:
    """Send an OTP to *phone* via Twilio SMS.

    In development mode the OTP is printed to the console instead.

    Args:
        phone: E.164 formatted phone number.
        otp: 6-digit OTP string.

    Returns:
        True on success, False on any delivery failure.
    """
    if settings.ENVIRONMENT != "production":
        print(f"\n[DEV MODE] OTP for {phone}: {otp}\n", flush=True)
        logger.info("DEV MODE — OTP for %s: %s", phone, otp)
        return True

    try:
        from twilio.rest import Client  # type: ignore[import-untyped]

        client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
        client.messages.create(
            body=f"Your verification code is {otp}. Valid for {settings.OTP_EXPIRE_MINUTES} minutes.",
            from_=settings.TWILIO_PHONE_NUMBER,
            to=phone,
        )
        return True
    except Exception as exc:
        logger.error("Failed to send OTP SMS to %s: %s", phone, exc)
        return False


_ALERT_EMAIL_HTML_TEMPLATE = """
<div style="font-family:sans-serif;max-width:560px;margin:auto">
  <h2>{subject}</h2>
  <pre style="white-space:pre-wrap;font-family:inherit;font-size:14px">{body}</pre>
</div>
"""


async def send_alert_email(subject: str, body: str) -> bool:
    """Send an ops alert to ALERT_EMAIL via Resend — same integration as OTP email.

    Skips silently if ALERT_EMAIL isn't configured (matches the Slack alert's
    no-op-until-configured contract). In development the alert is logged
    instead of sent, matching send_otp_email's dev short-circuit.

    Args:
        subject: Short alert subject line.
        body: Plain-text alert body (rendered inside a <pre> block).

    Returns:
        True if sent (or logged in dev), False if skipped or delivery failed.
    """
    if not settings.ALERT_EMAIL:
        logger.debug("ALERT_EMAIL not configured — skipping alert email: %s", subject)
        return False

    if settings.ENVIRONMENT != "production":
        logger.info("[DEV MODE] Alert email to %s — %s\n%s", settings.ALERT_EMAIL, subject, body)
        return True

    try:
        import resend  # type: ignore[import-untyped]

        resend.api_key = settings.RESEND_API_KEY
        resend.Emails.send(
            {
                "from": settings.EMAIL_FROM,
                "to": settings.ALERT_EMAIL,
                "subject": f"[Recast Alert] {subject}",
                "html": _ALERT_EMAIL_HTML_TEMPLATE.format(subject=subject, body=body),
            }
        )
        return True
    except Exception as exc:
        logger.error("Failed to send alert email (%s): %s", subject, exc)
        return False


async def send_otp(identifier: str, otp: str, channel: str) -> bool:
    """Route OTP delivery to the correct channel.

    Args:
        identifier: Normalized email or E.164 phone number.
        otp: 6-digit OTP string.
        channel: ``"email"`` or ``"sms"``.

    Returns:
        True on successful dispatch, False on failure.
    """
    if channel == "email":
        return await send_otp_email(identifier, otp)
    return await send_otp_sms(identifier, otp)
