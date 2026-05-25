"""Authentication routes — OTP request/verify, signup, login, refresh, logout."""

import random
import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from jose import JWTError, jwt
from pymongo.errors import DuplicateKeyError

from app.core.auth import (
    blacklist_token,
    clear_auth_cookies,
    create_access_token,
    create_refresh_token,
    generate_username,
    get_current_user,
    get_token_from_request,
    is_username_taken,
    rotate_refresh_token,
    set_auth_cookies,
)
from app.core.config import settings
from app.core.middleware import limiter
from app.core.notifications import send_otp
from app.core.otp import (
    check_rate_limit,
    clear_otp_state,
    generate_otp,
    is_locked,
    normalize_identifier,
    verify_otp,
)
from app.db.mongo import users
from app.db.redis import get_redis
from app.models.user import (
    AuthResponse,
    LoginBody,
    OTPChannel,
    OTPRequestBody,
    OTPSentResponse,
    OTPVerifyBody,
    SignupCompleteBody,
    UserPlan,
    UserProfileResponse,
    VerifyOTPResponse,
)

router = APIRouter()

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^\+[1-9]\d{7,14}$")


def _validate_identifier_format(identifier: str, channel: OTPChannel) -> None:
    """Validate that the identifier format matches the declared delivery channel.

    Ensures emails match a basic RFC-style pattern and phone numbers conform
    to E.164 international format (e.g. +14155552671).

    Args:
        identifier: Normalised email address or E.164 phone number string.
        channel: OTPChannel.EMAIL or OTPChannel.SMS declared by the caller.

    Raises:
        HTTPException 400: If the identifier format does not match the channel.
    """
    if channel == OTPChannel.EMAIL:
        if not _EMAIL_RE.match(identifier):
            raise HTTPException(status_code=400, detail="Invalid email address.")
    else:
        if not _PHONE_RE.match(identifier):
            raise HTTPException(
                status_code=400,
                detail="Phone number must be in E.164 format (e.g. +14155552671).",
            )


def _build_profile_response(user: dict) -> UserProfileResponse:
    """Convert a raw MongoDB user document into a typed UserProfileResponse.

    Strips internal MongoDB fields (e.g. _id) and applies safe defaults for
    optional fields that may be absent in older documents.

    Args:
        user: Raw dict returned directly from a MongoDB find operation.

    Returns:
        A fully populated UserProfileResponse Pydantic model instance.
    """
    return UserProfileResponse(
        id=user["id"],
        name=user["name"],
        username=user["username"],
        avatar_url=user.get("avatar_url", ""),
        bio=user.get("bio", ""),
        website=user.get("website", ""),
        timezone=user.get("timezone", "UTC"),
        language=user.get("language", "en"),
        preferred_platforms=user.get("preferred_platforms", []),
        plan=user.get("plan", UserPlan.FREE),
        credits_used=user.get("credits_used", 0),
        credits_limit=user.get("credits_limit", 100),
        onboarding_done=user.get("onboarding_done", False),
        brand_profiles=user.get("brand_profiles", []),
        created_at=user["created_at"],
    )


@router.post("/request-otp", response_model=OTPSentResponse)
@limiter.limit("5/minute")
async def request_otp(
    request: Request,
    body: OTPRequestBody,
) -> OTPSentResponse:
    """Send a one-time password to the given email or phone identifier.

    Validates the identifier format, checks for account lockout, enforces
    per-identifier rate limits, generates a 6-digit OTP, stores it in Redis
    with a 10-minute TTL, and dispatches it via the requested channel.

    Rate limited to 5 requests per minute per IP address.

    Args:
        request: FastAPI Request instance (required by slowapi rate limiter).
        body: OTPRequestBody containing identifier string and delivery channel.

    Returns:
        OTPSentResponse confirming dispatch and the 60-second cooldown period.

    Raises:
        HTTPException 400: Identifier format does not match the declared channel.
        HTTPException 423: Identifier is locked after too many failed attempts.
        HTTPException 429: IP-level rate limit exceeded.
    """
    identifier = normalize_identifier(body.identifier)
    _validate_identifier_format(identifier, body.channel)

    if await is_locked(identifier):
        raise HTTPException(
            status_code=423,
            detail="Account locked due to too many failed attempts. Try again in 15 minutes.",
        )

    await check_rate_limit(identifier)
    otp = await generate_otp(identifier)
    await send_otp(identifier, otp, body.channel.value)

    return OTPSentResponse(
        message=f"OTP sent via {body.channel.value}.",
        cooldown_seconds=60,
    )


@router.post("/verify-otp", response_model=VerifyOTPResponse)
@limiter.limit("20/minute")
async def verify_otp_route(
    request: Request,
    body: OTPVerifyBody,
) -> VerifyOTPResponse:
    """Verify a submitted OTP code and mark the identifier as verified in Redis.

    On success the OTP entry is deleted from Redis and a short-lived
    ``otp:{identifier}:verified`` key is written with a 10-minute TTL.
    This verified flag is required by the subsequent /signup and /login
    endpoints to prevent token issuance without OTP completion.

    Also checks whether an account already exists for the identifier so the
    frontend can route the user to signup vs. login without a separate call.

    Rate limited to 20 requests per minute per IP address.

    Args:
        request: FastAPI Request instance (required by slowapi rate limiter).
        body: OTPVerifyBody containing identifier, OTP code, and channel.

    Returns:
        VerifyOTPResponse with ``valid=True`` and an ``is_new_user`` boolean
        indicating whether an account already exists for this identifier.

    Raises:
        HTTPException 401: OTP is invalid, expired, or has been consumed.
        HTTPException 423: Identifier is locked after too many failed attempts.
    """
    identifier = normalize_identifier(body.identifier)
    await verify_otp(identifier, body.otp)

    existing = await users.find_one(
        {
            "$or": [
                {"email": identifier},
                {"phone": identifier},
                {"auth_identifiers": identifier},
            ]
        }
    )

    return VerifyOTPResponse(valid=True, is_new_user=existing is None)


@router.post("/signup", response_model=AuthResponse)
@limiter.limit("10/minute")
async def signup(
    request: Request,
    response: Response,
    body: SignupCompleteBody,
) -> AuthResponse:
    """Complete new user registration after successful OTP verification.

    Requires the ``otp:{identifier}:verified`` Redis key written by
    /verify-otp. Validates that neither the identifier nor the desired
    username is already registered, creates the user document in MongoDB,
    clears all OTP state for the identifier, issues a JWT access + refresh
    token pair, and sets both as HttpOnly Secure cookies on the response.

    The access token is also returned in the response body to support
    non-browser API clients that cannot read cookies.

    Rate limited to 10 requests per minute per IP address.

    Args:
        request: FastAPI Request instance (required by slowapi rate limiter).
        response: FastAPI Response instance used to attach auth cookies.
        body: SignupCompleteBody with identifier, channel, name, and username.

    Returns:
        AuthResponse containing access_token, refresh_token, and user profile.
        Auth cookies (access_token, refresh_token) are also set on the response.

    Raises:
        HTTPException 401: Verified flag missing or expired — OTP flow required.
        HTTPException 409: Email/phone or username already registered.
    """
    identifier = normalize_identifier(body.identifier)
    redis = await get_redis()

    if not await redis.exists(f"otp:{identifier}:verified"):
        raise HTTPException(
            status_code=401,
            detail="OTP not verified or session expired. Please restart the verification flow.",
        )

    existing = await users.find_one(
        {
            "$or": [
                {"email": identifier},
                {"phone": identifier},
                {"auth_identifiers": identifier},
            ]
        }
    )
    if existing:
        raise HTTPException(status_code=409, detail="User already exists. Please log in.")

    username = body.username.lower()
    if await is_username_taken(username):
        raise HTTPException(status_code=409, detail="Username is already taken.")

    now = datetime.now(timezone.utc)
    user_id = str(uuid4())

    user_doc: dict[str, Any] = {
        "id": user_id,
        "name": body.name,
        "username": username,
        "auth_identifiers": [identifier],
        "avatar_url": "",
        "bio": "",
        "website": "",
        "timezone": "UTC",
        "language": "en",
        "preferred_platforms": [],
        "plan": UserPlan.FREE.value,
        "credits_used": 0,
        "credits_limit": 100,
        "onboarding_done": False,
        "brand_profiles": [],
        "created_at": now,
        "last_active": now,
    }

    if body.channel == OTPChannel.EMAIL:
        user_doc["email"] = identifier
    else:
        user_doc["phone"] = identifier

    try:
        await users.insert_one(user_doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Username or identifier already taken. Please choose another.",
        )

    await clear_otp_state(identifier)

    access_token  = create_access_token({"sub": user_id})
    refresh_token = create_refresh_token({"sub": user_id})

    # Write HttpOnly Secure cookies — browser clients never touch tokens directly
    set_auth_cookies(response, access_token, refresh_token)

    return AuthResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        user=_build_profile_response(user_doc),
    )


@router.post("/login", response_model=AuthResponse)
@limiter.limit("20/minute")
async def login(
    request: Request,
    response: Response,
    body: LoginBody,
) -> AuthResponse:
    """Issue a new token pair for an existing user after OTP verification.

    Requires the ``otp:{identifier}:verified`` Redis key written by
    /verify-otp. Looks the user up across email, phone, and auth_identifiers
    fields to support accounts created via either channel. Updates
    ``last_active`` timestamp, clears OTP state, issues tokens, and sets
    HttpOnly Secure cookies on the response.

    Rate limited to 20 requests per minute per IP address.

    Args:
        request: FastAPI Request instance (required by slowapi rate limiter).
        response: FastAPI Response instance used to attach auth cookies.
        body: LoginBody containing identifier and channel.

    Returns:
        AuthResponse containing access_token, refresh_token, and user profile.
        Auth cookies (access_token, refresh_token) are also set on the response.

    Raises:
        HTTPException 401: Verified flag missing or expired — OTP flow required.
        HTTPException 404: No account found — frontend should redirect to signup.
    """
    identifier = normalize_identifier(body.identifier)
    redis = await get_redis()

    if not await redis.exists(f"otp:{identifier}:verified"):
        raise HTTPException(
            status_code=401,
            detail="OTP not verified or session expired. Please restart the verification flow.",
        )

    user = await users.find_one(
        {
            "$or": [
                {"email": identifier},
                {"phone": identifier},
                {"auth_identifiers": identifier},
            ]
        }
    )

    if not user:
        raise HTTPException(
            status_code=404,
            detail="No account found for this identifier. Please sign up.",
        )

    await users.update_one(
        {"id": user["id"]},
        {"$set": {"last_active": datetime.now(timezone.utc)}},
    )

    await clear_otp_state(identifier)

    access_token  = create_access_token({"sub": user["id"]})
    refresh_token = create_refresh_token({"sub": user["id"]})

    # Write HttpOnly Secure cookies — browser clients never touch tokens directly
    set_auth_cookies(response, access_token, refresh_token)

    return AuthResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        user=_build_profile_response(user),
    )


@router.post("/refresh")
@limiter.limit("20/minute")
async def refresh_tokens(
    request: Request,
    response: Response,
) -> dict[str, str]:
    """Rotate the refresh token and issue a new access + refresh token pair.

    Reads the refresh token from the ``refresh_token`` HttpOnly cookie first.
    Falls back to a JSON request body ``refresh_token`` field to support
    Postman and non-browser API clients that cannot send cookies.

    The old refresh token is immediately blacklisted by rotate_refresh_token
    to prevent replay attacks. New tokens are written back as HttpOnly cookies
    and also returned in the response body for non-browser clients.

    Rate limited to 20 requests per minute per IP address.

    Args:
        request: FastAPI Request instance (required by slowapi rate limiter).
        response: FastAPI Response instance used to update auth cookies.

    Returns:
        Dict containing ``access_token`` and ``refresh_token`` strings.
        Updated auth cookies are also set on the response.

    Raises:
        HTTPException 400: No refresh token found in cookie or request body.
        HTTPException 401: Refresh token is invalid, expired, or blacklisted.
    """
    # Prefer cookie — more secure than body for browser clients
    refresh_token = request.cookies.get("refresh_token")

    # Fallback to request body for Postman and non-browser API clients
    if not refresh_token:
        try:
            body = await request.json()
            refresh_token = body.get("refresh_token", "")
        except Exception:
            pass

    if not refresh_token:
        raise HTTPException(
            status_code=400,
            detail="No refresh token found in cookie or request body.",
        )

    new_access, new_refresh = await rotate_refresh_token(refresh_token)

    # Rotate cookies so the browser always holds the latest token pair
    set_auth_cookies(response, new_access, new_refresh)

    return {"access_token": new_access, "refresh_token": new_refresh}


@router.post("/logout")
@limiter.limit("20/minute")
async def logout(
    request: Request,
    response: Response,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, str]:
    """Invalidate the current session by blacklisting tokens and clearing cookies.

    Extracts the access token from the ``access_token`` cookie first, then
    falls back to the Authorization header for non-browser clients. Blacklists
    the access token in Redis with a TTL matching its remaining validity.

    Also blacklists the refresh token from the ``refresh_token`` cookie if
    present, computing its remaining TTL from the JWT expiry claim to avoid
    storing already-expired tokens unnecessarily.

    Clears both HttpOnly cookies from the browser response regardless of
    whether a valid token was found, ensuring a clean client-side state.

    Rate limited to 20 requests per minute per IP address.

    Args:
        request: FastAPI Request instance (required by slowapi rate limiter).
        response: FastAPI Response instance used to delete auth cookies.
        current_user: Full user document injected by get_current_user.
            Ensures only authenticated users can hit this endpoint.

    Returns:
        Dict with a ``message`` key confirming successful logout.
    """
    # Extract access token — cookie takes priority over Authorization header
    token = request.cookies.get("access_token")
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]

    if token:
        await blacklist_token(token)

    # Also invalidate the refresh token to prevent token rotation post-logout
    refresh_token = request.cookies.get("refresh_token")
    if refresh_token:
        try:
            payload = jwt.decode(
                refresh_token,
                settings.JWT_SECRET_KEY,
                algorithms=[settings.JWT_ALGORITHM],
            )
            # Only blacklist if token has remaining validity
            ttl = payload.get("exp", 0) - int(datetime.now(timezone.utc).timestamp())
            if ttl > 0:
                redis = await get_redis()
                await redis.setex(f"blacklist:{refresh_token}", ttl, "1")
        except JWTError:
            pass  # Already expired — no blacklist entry needed

    # Clear cookies from browser regardless of token validity
    clear_auth_cookies(response)

    return {"message": "Logged out successfully."}


@router.get("/check-username/{username}")
@limiter.limit("30/minute")
async def check_username(
    request: Request,
    username: str,
) -> dict[str, Any]:
    """Check whether a username is available and suggest an alternative if taken.

    Validates the username against a strict alphanumeric + underscore pattern
    (3–30 characters). If unavailable, generates a base slug from the input
    and appends random 4-digit suffixes until a free candidate is found.
    Falls back to a UUID hex suffix if no candidate is found within 20 tries.

    Rate limited to 30 requests per minute per IP address.

    Args:
        request: FastAPI Request instance (required by slowapi rate limiter).
        username: Desired username string from the URL path parameter.

    Returns:
        Dict with ``available`` (bool) and ``suggestion`` (str).
        suggestion is empty string when the username is available.

    Raises:
        HTTPException 400: Username contains invalid characters or wrong length.
    """
    if not re.match(r"^[a-zA-Z0-9_]{3,30}$", username):
        raise HTTPException(
            status_code=400,
            detail="Username must be 3–30 characters and contain only letters, numbers, or underscores.",
        )

    available = not await is_username_taken(username.lower())

    suggestion = ""
    if not available:
        base = generate_username(username)
        # Try up to 20 random suffixes before falling back to UUID
        for _ in range(20):
            candidate = f"{base}_{random.randint(1000, 9999)}"
            if not await is_username_taken(candidate):
                suggestion = candidate
                break
        if not suggestion:
            suggestion = f"{base}_{uuid4().hex[:6]}"

    return {"available": available, "suggestion": suggestion}