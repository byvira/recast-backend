"""OTP generation, verification, and rate-limit enforcement using Redis."""

import hmac
import random

import phonenumbers
from fastapi import HTTPException

from app.core.config import settings
from app.db.redis import get_redis


def _mask_email(email: str) -> str:
    """Mask an email's local part for display, e.g. "john@x.com" -> "j***@x.com"."""
    local, _, domain = email.partition("@")
    if not domain:
        return email
    return f"{local[:1]}***@{domain}"


def normalize_identifier(identifier: str) -> str:
    """Normalize an identifier before use as a Redis key or DB query.

    Email addresses are lower-cased and stripped.  Phone numbers are parsed
    and returned in E.164 format using the ``phonenumbers`` library.

    Args:
        identifier: Raw email or phone string from the client.

    Returns:
        Normalized lowercase email or E.164 phone number.
    """
    identifier = identifier.lower().strip()
    try:
        parsed = phonenumbers.parse(identifier, None)
        if phonenumbers.is_valid_number(parsed):
            return phonenumbers.format_number(
                parsed, phonenumbers.PhoneNumberFormat.E164
            )
    except Exception:
        pass
    return identifier


async def generate_otp(identifier: str) -> str:
    """Generate a cryptographically random 6-digit OTP and store it in Redis.

    The OTP is stored at ``otp:{identifier}:code`` with a TTL of
    ``OTP_EXPIRE_MINUTES``.  Any previous OTP for this identifier is
    overwritten atomically.

    Args:
        identifier: Normalized email or phone (call normalize_identifier first).

    Returns:
        The generated 6-digit OTP string (for delivery via email/SMS).
    """
    identifier = normalize_identifier(identifier)
    otp = str(random.SystemRandom().randint(100000, 999999))
    redis = await get_redis()
    await redis.set(
        f"otp:{identifier}:code",
        otp,
        ex=settings.OTP_EXPIRE_MINUTES * 60,
    )
    # A fresh code starts with a fresh set of guesses.
    await redis.delete(f"otp:{identifier}:attempts")
    return otp


async def verify_otp(identifier: str, otp: str) -> bool:
    """Verify an OTP code, deleting it and setting the verified flag atomically.

    Flow:
    1. Fetch stored OTP → 401 if expired/missing.
    2. Compare codes → count a failed attempt on a mismatch.
    3. On max attempts → cancel that code (a new one must be requested).
    4. On success → pipeline: delete code + attempts, set verified flag.

    Args:
        identifier: Raw identifier (normalized internally).
        otp: 6-digit code supplied by the user.

    Returns:
        True on successful verification.

    Raises:
        HTTPException 401: OTP expired/missing or code mismatch.
    """
    identifier = normalize_identifier(identifier)
    redis = await get_redis()

    stored = await redis.get(f"otp:{identifier}:code")
    if not stored:
        raise HTTPException(
            status_code=401,
            detail="OTP expired or not found. Please request a new one.",
        )

    # Every try is counted before it is compared, in one atomic step, so a burst of guesses sent at the same moment still uses up the
    # allowance together. Counting after the comparison let each of them read the code before any of them cancelled it.
    attempts = int(await redis.incr(f"otp:{identifier}:attempts"))
    await redis.expire(
        f"otp:{identifier}:attempts",
        settings.OTP_LOCK_MINUTES * 60,
    )
    if attempts > settings.OTP_MAX_ATTEMPTS:
        await redis.delete(f"otp:{identifier}:code", f"otp:{identifier}:attempts")
        raise HTTPException(
            status_code=401,
            detail="Too many wrong codes. Please request a new one.",
        )

    if not hmac.compare_digest(str(stored), str(otp)):
        remaining = settings.OTP_MAX_ATTEMPTS - attempts

        if attempts >= settings.OTP_MAX_ATTEMPTS:
            # Only this code is cancelled. The address itself is never locked, so a stranger typing wrong codes for someone else's email
            # cannot stop that person signing in; they just ask for a new code.
            await redis.delete(f"otp:{identifier}:code", f"otp:{identifier}:attempts")
            raise HTTPException(
                status_code=401,
                detail="Too many wrong codes. Please request a new one.",
            )

        raise HTTPException(
            status_code=401,
            detail=f"Invalid OTP. {remaining} attempt(s) remaining.",
        )

    # SUCCESS — atomically delete code + attempts, set verified flag
    pipe = redis.pipeline()
    pipe.delete(f"otp:{identifier}:code")
    pipe.delete(f"otp:{identifier}:attempts")
    pipe.set(
        f"otp:{identifier}:verified",
        "1",
        ex=settings.OTP_EXPIRE_MINUTES * 60,
    )
    await pipe.execute()
    return True


async def check_rate_limit(identifier: str) -> None:
    """Enforce layered OTP send rate limits: cooldown → hourly → daily.

    Checks are applied in order.  If all pass, the cooldown key is set and
    hourly/daily counters are incremented atomically via pipeline.

    Args:
        identifier: Raw identifier (normalized internally).

    Raises:
        HTTPException 429: If any rate limit is exceeded.
    """
    identifier = normalize_identifier(identifier)
    redis = await get_redis()

    cooldown, hours, days = f"otp:{identifier}:cooldown", f"otp:{identifier}:hour_count", f"otp:{identifier}:day_count"

    # Per-request cooldown. Taken with one atomic "set if not set", so of several requests arriving together exactly one gets it and
    # the rest are refused; checking first and setting afterwards let all of them through and sent a code for each.
    if not await redis.set(cooldown, "1", nx=True, ex=settings.OTP_COOLDOWN_SECONDS):
        ttl = await redis.ttl(cooldown)
        raise HTTPException(
            status_code=429,
            detail=f"Please wait {ttl} seconds before requesting another OTP.",
        )

    # Hourly and daily limits. A request over a limit gives back what it took (the count and the cooldown), so refused requests are
    # never counted and cannot keep the limit running for longer.
    hour_count = int(await redis.incr(hours))
    if hour_count > settings.OTP_MAX_SENDS_PER_HOUR:
        await redis.decr(hours)
        await redis.delete(cooldown)
        raise HTTPException(
            status_code=429,
            detail="Hourly OTP limit reached. Try again later.",
        )

    day_count = int(await redis.incr(days))
    if day_count > settings.OTP_MAX_SENDS_PER_DAY:
        await redis.decr(days)
        await redis.decr(hours)
        await redis.delete(cooldown)
        raise HTTPException(
            status_code=429,
            detail="Daily OTP limit reached. Try again tomorrow.",
        )

    pipe = redis.pipeline()
    pipe.expire(hours, 3600)
    pipe.expire(days, 86400)
    await pipe.execute()


async def is_locked(identifier: str) -> bool:
    """Check whether the identifier is locked due to excessive failed OTP attempts.

    Args:
        identifier: Raw identifier (normalized internally).

    Returns:
        True if the lock key exists in Redis.
    """
    identifier = normalize_identifier(identifier)
    redis = await get_redis()
    return bool(await redis.exists(f"otp:{identifier}:locked"))


async def clear_otp_state(identifier: str) -> None:
    """Delete all sensitive OTP Redis keys after a successful authentication.

    Clears: code, attempts, locked, verified, cooldown.
    Preserves: hour_count, day_count (rate-limit memory is intentionally kept).

    Args:
        identifier: Raw identifier (normalized internally).
    """
    identifier = normalize_identifier(identifier)
    redis = await get_redis()
    await redis.delete(
        f"otp:{identifier}:code",
        f"otp:{identifier}:attempts",
        f"otp:{identifier}:locked",
        f"otp:{identifier}:verified",
        f"otp:{identifier}:cooldown",
    )
