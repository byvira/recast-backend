"""OTP generation, verification, and rate-limit enforcement using Redis."""

import random

import phonenumbers
from fastapi import HTTPException

from app.core.config import settings
from app.db.redis import get_redis


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
    return otp


async def verify_otp(identifier: str, otp: str) -> bool:
    """Verify an OTP code, deleting it and setting the verified flag atomically.

    Flow:
    1. Check locked key → 423 if present.
    2. Fetch stored OTP → 401 if expired/missing.
    3. Compare codes → increment attempts counter on failure.
    4. On max attempts → set lock key → 423.
    5. On success → pipeline: delete code + attempts, set verified flag.

    Args:
        identifier: Raw identifier (normalized internally).
        otp: 6-digit code supplied by the user.

    Returns:
        True on successful verification.

    Raises:
        HTTPException 423: Account locked due to too many failures.
        HTTPException 401: OTP expired/missing or code mismatch.
    """
    identifier = normalize_identifier(identifier)
    redis = await get_redis()

    if await redis.exists(f"otp:{identifier}:locked"):
        raise HTTPException(
            status_code=423,
            detail="Account locked due to too many failed attempts. Try again in 15 minutes.",
        )

    stored = await redis.get(f"otp:{identifier}:code")
    if not stored:
        raise HTTPException(
            status_code=401,
            detail="OTP expired or not found. Please request a new one.",
        )

    if stored != otp:
        attempts = int(await redis.incr(f"otp:{identifier}:attempts"))
        await redis.expire(
            f"otp:{identifier}:attempts",
            settings.OTP_LOCK_MINUTES * 60,
        )
        remaining = settings.OTP_MAX_ATTEMPTS - attempts

        if attempts >= settings.OTP_MAX_ATTEMPTS:
            await redis.set(
                f"otp:{identifier}:locked",
                "1",
                ex=settings.OTP_LOCK_MINUTES * 60,
            )
            raise HTTPException(
                status_code=423,
                detail="Too many failed attempts. Account locked for 15 minutes.",
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

    # Per-request cooldown
    if await redis.exists(f"otp:{identifier}:cooldown"):
        ttl = await redis.ttl(f"otp:{identifier}:cooldown")
        raise HTTPException(
            status_code=429,
            detail=f"Please wait {ttl} seconds before requesting another OTP.",
        )

    # Hourly limit
    hour_count = await redis.get(f"otp:{identifier}:hour_count")
    if hour_count and int(hour_count) >= settings.OTP_MAX_SENDS_PER_HOUR:
        raise HTTPException(
            status_code=429,
            detail="Hourly OTP limit reached. Try again later.",
        )

    # Daily limit
    day_count = await redis.get(f"otp:{identifier}:day_count")
    if day_count and int(day_count) >= settings.OTP_MAX_SENDS_PER_DAY:
        raise HTTPException(
            status_code=429,
            detail="Daily OTP limit reached. Try again tomorrow.",
        )

    # All checks passed — set cooldown and increment counters atomically
    pipe = redis.pipeline()
    pipe.set(f"otp:{identifier}:cooldown", "1", ex=settings.OTP_COOLDOWN_SECONDS)
    pipe.incr(f"otp:{identifier}:hour_count")
    pipe.expire(f"otp:{identifier}:hour_count", 3600)
    pipe.incr(f"otp:{identifier}:day_count")
    pipe.expire(f"otp:{identifier}:day_count", 86400)
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
