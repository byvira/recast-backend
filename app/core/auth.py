"""JWT utilities, token blacklisting, refresh token rotation, and auth dependency."""

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from app.core.config import settings
from app.db.mongo import users
from app.db.redis import get_redis

security = HTTPBearer(auto_error=False)

# ── Cookie configuration ──────────────────────────────────────────────────────

ACCESS_TOKEN_MAX_AGE  = 60 * 60 * 24        # 24 hours in seconds
REFRESH_TOKEN_MAX_AGE = 60 * 60 * 24 * 30   # 30 days in seconds


def set_auth_cookies(response: Response, access_token: str, refresh_token: str) -> None:
    """Write access and refresh tokens as HttpOnly Secure cookies on the response.

    In development (non-production) the ``secure`` flag is disabled so cookies
    work over plain HTTP on localhost. In production both cookies require HTTPS.

    SameSite=lax prevents CSRF while still allowing top-level navigations
    (e.g. OAuth redirects) to carry the cookie.

    Args:
        response: FastAPI Response instance to attach cookies to.
        access_token: Signed JWT access token string.
        refresh_token: Signed JWT refresh token string.
    """
    is_prod = settings.ENVIRONMENT == "production"

    response.set_cookie(
        key="access_token",
        value=access_token,
        max_age=ACCESS_TOKEN_MAX_AGE,
        httponly=True,
        secure=is_prod,
        samesite="lax",
    )
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        max_age=REFRESH_TOKEN_MAX_AGE,
        httponly=True,
        secure=is_prod,
        samesite="lax",
    )


def clear_auth_cookies(response: Response) -> None:
    """Delete access and refresh token cookies from the browser on logout.

    Deleting a cookie sets its Max-Age to 0, causing the browser to
    discard it immediately on the next response receipt.

    Args:
        response: FastAPI Response instance to remove cookies from.
    """
    response.delete_cookie("access_token")
    response.delete_cookie("refresh_token")


def get_token_from_request(request: Request) -> str | None:
    """Extract a bearer token from the request, preferring cookies over headers.

    Checks the ``access_token`` HttpOnly cookie first. Falls back to the
    ``Authorization: Bearer <token>`` header for non-browser API clients
    such as Postman or mobile apps that cannot send cookies.

    Args:
        request: Incoming FastAPI Request instance.

    Returns:
        Raw JWT string if found, or None if no token is present.
    """
    # Cookie takes priority — safer than headers for browser clients
    token = request.cookies.get("access_token")
    if token:
        return token

    # Fallback for Postman, CLI tools, and non-browser API clients
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]

    return None


# ── Token creation ────────────────────────────────────────────────────────────

def create_access_token(data: dict[str, Any]) -> str:
    """Create a signed JWT access token from the given payload data.

    Merges caller-supplied claims with standard claims (iss, aud, type,
    iat, exp) and signs with the application secret key.

    The ``sub`` claim should be set by the caller:
        ``create_access_token({"sub": user_id})``

    Args:
        data: Dict of additional claims to include in the payload.
              Must contain at least ``{"sub": user_id}``.

    Returns:
        Compact serialised JWT string.
    """
    now = datetime.now(timezone.utc)
    payload = {
        **data,
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
        "type": "access",
        "iat": now,
        "exp": now + timedelta(hours=settings.JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def create_refresh_token(data: dict[str, Any]) -> str:
    """Create a signed JWT refresh token from the given payload data.

    Identical to create_access_token but uses a longer expiry and sets
    ``type=refresh`` to prevent this token from being accepted as an
    access token by the auth dependency.

    Args:
        data: Dict of additional claims to include in the payload.
              Must contain at least ``{"sub": user_id}``.

    Returns:
        Compact serialised JWT string.
    """
    now = datetime.now(timezone.utc)
    payload = {
        **data,
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
        "type": "refresh",
        "iat": now,
        "exp": now + timedelta(days=settings.JWT_REFRESH_EXPIRE_DAYS),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


# ── Token verification ────────────────────────────────────────────────────────

def verify_token(token: str, expected_type: str = "access") -> dict[str, Any]:
    """Decode and validate a JWT, enforcing iss, aud, exp, and type claims.

    Uses the application SECRET_KEY and ALGORITHM from settings. Validates
    the ``type`` claim to prevent refresh tokens being used as access tokens
    and vice versa.

    Args:
        token: Raw JWT string without the ``Bearer`` prefix.
        expected_type: ``"access"`` or ``"refresh"``. Defaults to ``"access"``.

    Returns:
        Decoded payload dictionary containing all JWT claims.

    Raises:
        HTTPException 401: On signature failure, expiry, or type mismatch.
    """
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
            audience=settings.JWT_AUDIENCE,
            issuer=settings.JWT_ISSUER,
        )
        if payload.get("type") != expected_type:
            raise HTTPException(
                status_code=401,
                detail=f"Invalid token type. Expected '{expected_type}'.",
            )
        return payload
    except JWTError as exc:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token.",
        ) from exc


# ── Blacklist ─────────────────────────────────────────────────────────────────

async def blacklist_token(token: str) -> None:
    """Add a token to the Redis blacklist using only its remaining TTL.

    The Redis key is set to expire at the same instant the JWT itself would
    expire, so blacklisted tokens are automatically evicted from Redis without
    any manual cleanup. Tokens that are already expired are silently ignored.

    Args:
        token: Raw JWT string to invalidate immediately.
    """
    try:
        # Decode without expiry verification to extract the exp claim
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
            options={"verify_exp": False},
        )
        exp = payload.get("exp", 0)
        remaining = int(exp - datetime.now(timezone.utc).timestamp())
        if remaining > 0:
            redis = await get_redis()
            await redis.set(f"blacklist:{token}", "1", ex=remaining)
    except Exception:
        pass  # Token is already invalid — no blacklist entry needed


async def is_token_blacklisted(token: str) -> bool:
    """Check whether a token exists in the Redis blacklist.

    Args:
        token: Raw JWT string to check.

    Returns:
        True if the token has been blacklisted, False if it is still valid.
    """
    redis = await get_redis()
    return bool(await redis.exists(f"blacklist:{token}"))


# ── Refresh token rotation ────────────────────────────────────────────────────

async def rotate_refresh_token(refresh_token: str) -> tuple[str, str]:
    """Validate a refresh token, immediately blacklist it, and issue a new pair.

    Implements refresh token rotation — each refresh token can only be used
    once. The old token is blacklisted before new tokens are issued, so a
    stolen refresh token cannot be replayed after legitimate rotation.

    Args:
        refresh_token: A valid ``type=refresh`` JWT string.

    Returns:
        Tuple of (new_access_token, new_refresh_token) as compact JWT strings.

    Raises:
        HTTPException 401: If the token is invalid, expired, or has no sub claim.
    """
    payload = verify_token(refresh_token, expected_type="refresh")
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid refresh token payload.")

    # Blacklist immediately before issuing replacement — prevents replay
    await blacklist_token(refresh_token)

    new_access  = create_access_token({"sub": user_id})
    new_refresh = create_refresh_token({"sub": user_id})
    return new_access, new_refresh


# ── Username helpers ──────────────────────────────────────────────────────────

def generate_username(name: str) -> str:
    """Auto-generate a URL-safe base username slug from a full name.

    Strips non-alphanumeric characters, lowercases, and truncates to 26
    characters to leave room for a 4-digit numeric suffix when checking
    uniqueness. Example: ``"John Doe"`` → ``"johndoe"``.

    Args:
        name: User's full display name (may contain spaces and unicode).

    Returns:
        Slugified base username string, not yet guaranteed to be unique.
    """
    from slugify import slugify

    return slugify(name, separator="", lowercase=True)[:26]


async def is_username_taken(username: str) -> bool:
    """Check whether a username is already registered in the users collection.

    Case-insensitive: both ``JohnDoe`` and ``johndoe`` are treated as taken
    if either variant exists in the database.

    Args:
        username: Username string to check (will be lowercased before query).

    Returns:
        True if the username is already registered, False if available.
    """
    existing = await users.find_one({"username": username.lower()})
    return existing is not None


# ── Auth dependency ───────────────────────────────────────────────────────────

async def get_current_user(request: Request) -> dict:
    """FastAPI dependency that extracts and validates the current authenticated user.

    Reads the bearer token from the ``access_token`` HttpOnly cookie first,
    then falls back to the ``Authorization: Bearer`` header for non-browser
    clients. Checks the Redis blacklist before decoding to catch logged-out
    tokens. Uses settings.SECRET_KEY and settings.ALGORITHM for decoding,
    and validates iss, aud, and type=access claims.

    Args:
        request: Incoming FastAPI Request instance.

    Returns:
        Full user document dict from MongoDB for the authenticated user.

    Raises:
        HTTPException 401: No token found, token blacklisted, or token invalid.
        HTTPException 404: Token is valid but user no longer exists in DB.
    """
    token = get_token_from_request(request)

    if not token:
        raise HTTPException(
            status_code=401,
            detail="Authentication required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Reject blacklisted tokens before attempting to decode
    if await is_token_blacklisted(token):
        raise HTTPException(
            status_code=401,
            detail="Token has been revoked. Please log in again.",
        )

    # Decode and validate all claims — raises 401 on any failure
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,               # ← fixed: was settings.JWT_SECRET_KEY
            algorithms=[settings.ALGORITHM],   # ← fixed: was settings.JWT_ALGORITHM
            audience=settings.JWT_AUDIENCE,
            issuer=settings.JWT_ISSUER,
        )
    except JWTError:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token.",
        )

    # Enforce access token type — reject refresh tokens used as access tokens
    if payload.get("type") != "access":
        raise HTTPException(
            status_code=401,
            detail="Invalid token type. Access token required.",
        )

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=401,
            detail="Invalid token payload — missing subject claim.",
        )

    user = await users.find_one({"id": user_id})
    if not user:
        raise HTTPException(
            status_code=404,
            detail="User not found.",
        )

    return user