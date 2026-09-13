"""Notification delivery — Resend (email, via published templates) and Twilio (SMS).

In ENVIRONMENT=development all delivery is short-circuited and logged instead
so engineers can test without real credentials.

In ENVIRONMENT=production the actual APIs are called.  Any exception is caught
and logged; the function returns False so the caller can surface a friendly
error (or simply carry on) rather than crashing.
"""

import logging

from app.core.config import settings

logger = logging.getLogger(__name__)

_DEFAULT_FROM = "Recast <onboarding@resend.dev>"
OPS_FROM = "Recast Ops <onboarding@resend.dev>"


async def send_templated_email(
    template_id: str, to: str, variables: dict, from_override: str | None = None
) -> bool:
    """Send a published Resend template to *to*, filling in *variables*.

    This is the one place application code touches ``resend.Emails.send`` —
    every notification in this codebase should go through here rather than
    building HTML/subject strings by hand.

    Skips silently if *to* is falsy (e.g. the recipient has no email on
    file). In development the send is logged instead, matching the OTP
    flow's dev short-circuit.

    Args:
        template_id: Alias of the published Resend template to use.
        to: Recipient email address.
        variables: Template variables, keyed exactly as defined on Resend.
        from_override: Overrides the default "Recast <...>" sender — used
            by the ops-facing templates ("Recast Ops <...>").

    Returns:
        True on success (or logged in dev), False if skipped or delivery failed.
    """
    if not to:
        logger.debug("No recipient — skipping templated email '%s'", template_id)
        return False

    if settings.ENVIRONMENT != "production":
        logger.info("[DEV MODE] Templated email '%s' to %s: %s", template_id, to, variables)
        return True

    try:
        import resend  # type: ignore[import-untyped]

        resend.api_key = settings.RESEND_API_KEY
        resend.Emails.send(
            {
                "from": from_override or _DEFAULT_FROM,
                "to": to,
                "template": {"id": template_id, "variables": variables},
            }
        )
        return True
    except Exception as exc:
        logger.error("Failed to send templated email '%s' to %s: %s", template_id, to, exc)
        return False


async def send_otp_email(email: str, otp: str) -> bool:
    """Send an OTP to *email* via the "otp-verification" Resend template.

    Args:
        email: Recipient email address.
        otp: 6-digit OTP string.

    Returns:
        True on success, False on any delivery failure.
    """
    return await send_templated_email(
        "otp-verification",
        email,
        {"OTP_CODE": otp, "EXPIRY_MINUTES": settings.OTP_EXPIRE_MINUTES},
    )


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
