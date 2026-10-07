"""JWT utilities, token blacklisting, refresh token rotation, and auth dependency."""

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import structlog
from fastapi import Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
# QA-001: was python-jose, unmaintained with known CVEs (algorithm-confusion,
# a JWE decompression-bomb DoS) — neither directly exploitable here (a single
# fixed algorithm, no JWE ever used) but it's the entire session-auth layer
# and gets no further security fixes. PyJWT is actively maintained and a
# drop-in replacement for the encode/decode calls below (same parameter
# names); only the exception type changes (JWTError → PyJWTError).
import jwt
from jwt import PyJWTError

from app.core.config import settings
from app.db.mongo import users
from app.db.redis import get_redis

security = HTTPBearer(auto_error=False)

# ── Cookie configuration ──────────────────────────────────────────────────────

# The access cookie lives as long as the access token does (JWT_EXPIRE_HOURS); the browser then refreshes it with the refresh cookie.
REFRESH_TOKEN_MAX_AGE = 60 * 60 * 24 * 30   # 30 days in seconds


def _cookie_attributes() -> dict:
    """Where the sign-in cookies live and how they travel. Outside production: plain HTTP and Lax, so local work needs no certificate.
    In production: Secure, and SameSite from COOKIE_SAMESITE ("none" unless set to "lax" or "strict"), on COOKIE_DOMAIN when one is set.
    Setting and clearing must use the same attributes or the browser ignores the deletion."""
    is_prod = settings.ENVIRONMENT == "production"
    configured = (settings.COOKIE_SAMESITE or "none").strip().lower()
    samesite = (configured if configured in ("none", "lax", "strict") else "none") if is_prod else "lax"
    attributes: dict = {"secure": is_prod, "samesite": samesite}
    if is_prod and settings.COOKIE_DOMAIN.strip():
        attributes["domain"] = settings.COOKIE_DOMAIN.strip()
    return attributes


def set_auth_cookies(response: Response, access_token: str, refresh_token: str) -> None:
    """Write access and refresh tokens as HttpOnly Secure cookies on the response.

    In development (non-production) the ``secure`` flag is disabled so cookies
    work over plain HTTP on localhost.

    SameSite=None in production: the deployed frontend and backend sit on
    unrelated origins (e.g. a vercel.app frontend calling an onrender.com
    backend) — that's cross-*site*, not just cross-origin, and
    SameSite=Lax cookies are never attached to cross-site fetch/XHR calls.
    With Lax, login itself looks like it works (the Set-Cookie header is
    still present on that response) but every subsequent request — even the
    refresh call — silently goes out with no cookie, 401s, and the client
    bounces back to the login page in a loop. SameSite=None requires Secure,
    which is fine since production is already HTTPS-only; local dev (same
    site, just a different port) keeps Lax so it still works over plain
    HTTP.

    Args:
        response: FastAPI Response instance to attach cookies to.
        access_token: Signed JWT access token string.
        refresh_token: Signed JWT refresh token string.
    """
    attributes = _cookie_attributes()
    response.set_cookie(key="access_token", value=access_token, max_age=settings.JWT_EXPIRE_HOURS * 3600, httponly=True, **attributes)
    response.set_cookie(key="refresh_token", value=refresh_token, max_age=REFRESH_TOKEN_MAX_AGE, httponly=True, **attributes)


def clear_auth_cookies(response: Response) -> None:
    """Delete access and refresh token cookies from the browser on logout.

    Deleting a cookie sets its Max-Age to 0, causing the browser to
    discard it immediately on the next response receipt. The browser only
    honors a deletion if secure/samesite match how the cookie was
    originally set, so these must track set_auth_cookies exactly.

    Args:
        response: FastAPI Response instance to remove cookies from.
    """
    attributes = _cookie_attributes()
    response.delete_cookie("access_token", **attributes)
    response.delete_cookie("refresh_token", **attributes)


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
        "jti": uuid4().hex,
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
        "jti": uuid4().hex,
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
    except PyJWTError as exc:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token.",
        ) from exc


# ── Blacklist ─────────────────────────────────────────────────────────────────

def _blacklist_key(token: str) -> str:
    return f"blacklist:{hashlib.sha256(token.encode()).hexdigest()}"


# ── Sessions ──────────────────────────────────────────────────────────────────
# Every sign-in starts a session (`sid`). Its tokens all carry it, so the whole session can be ended at once: when a refresh token that
# was already used is shown again (someone holds a copy), and when a person logs out.

REUSE_GRACE_SECONDS = 10


def issue_tokens(user_id: str, sid: str | None = None) -> tuple[str, str]:
    """A new access and refresh token for a person, in a new session or in the session given."""
    claims = {"sub": user_id, "sid": sid or uuid4().hex}
    return create_access_token(claims), create_refresh_token(claims)


async def revoke_session(sid: str | None) -> None:
    """Ends a session: none of its tokens work again, wherever copies of them are."""
    if not sid:
        return
    redis = await get_redis()
    await redis.set(f"revoked_session:{sid}", "1", ex=settings.JWT_REFRESH_EXPIRE_DAYS * 86400)


async def is_session_revoked(sid: str | None) -> bool:
    if not sid:
        return False
    redis = await get_redis()
    return bool(await redis.exists(f"revoked_session:{sid}"))


def session_id_of(token: str | None) -> str | None:
    """The session a token belongs to, read without insisting it has not expired (a logout still has to end an old session)."""
    if not token:
        return None
    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM], audience=settings.JWT_AUDIENCE, issuer=settings.JWT_ISSUER,
            options={"verify_exp": False},
        )
        return payload.get("sid")
    except PyJWTError:
        return None


def _issued_before_cutoff(payload: dict[str, Any], user: dict | None) -> bool:
    """True when the person asked to sign out everywhere after this token was issued."""
    cutoff = (user or {}).get("tokens_valid_after")
    if not cutoff:
        return False
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    return float(payload.get("iat", 0)) < cutoff.timestamp()


async def blacklist_token(token: str) -> None:
    """Add a token to the Redis blacklist using only its remaining TTL.

    The Redis key is set to expire at the same instant the JWT itself would
    expire, so blacklisted tokens are automatically evicted from Redis without
    any manual cleanup. Tokens that are already expired are silently ignored.

    Args:
        token: Raw JWT string to invalidate immediately.
    """
    try:
        # Decode without expiry verification to extract the exp claim.
        # audience/issuer are validated the same way create_access_token /
        # create_refresh_token signed them, so this only rejects a genuinely
        # forged token, not every real one — see api/v1/auth.py's logout
        # decode for the historical version of this footgun (fixed there and
        # never present in PyJWT to begin with; kept explicit here anyway).
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
            audience=settings.JWT_AUDIENCE,
            issuer=settings.JWT_ISSUER,
            options={"verify_exp": False},
        )
        exp = payload.get("exp", 0)
        remaining = int(exp - datetime.now(timezone.utc).timestamp())
        if remaining > 0:
            redis = await get_redis()
            # The key is a hash of the token, so a copy of the Redis data is not a copy of live tokens. The value is when it was revoked.
            await redis.set(_blacklist_key(token), str(datetime.now(timezone.utc).timestamp()), ex=remaining)
    except Exception:
        pass  # Token is already invalid — no blacklist entry needed


async def is_token_blacklisted(token: str) -> bool:
    """Check whether a token exists in the Redis blacklist.

    Args:
        token: Raw JWT string to check.

    Returns:
        True if the token has been blacklisted, False if it is still valid.
    """
    return await _revoked_at(token) is not None


async def _revoked_at(token: str) -> float | None:
    """When a token was revoked (0.0 when that is not known), or None if it has not been. Entries made before keys were hashed are honoured
    until they expire."""
    redis = await get_redis()
    value = await redis.get(_blacklist_key(token))
    if value is not None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
    return 0.0 if await redis.exists(f"blacklist:{token}") else None


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

    sid = payload.get("sid")
    revoked = HTTPException(status_code=401, detail="Refresh token has been revoked. Please log in again.")

    if await is_session_revoked(sid):
        raise revoked

    # A token already used (or revoked at logout) is refused. When it is shown again well after it was used, someone holds a copy: the whole
    # session ends, so neither the copy nor the tokens made from it keep working. Within a few seconds it is two requests that raced
    # (two tabs), which is refused without ending anything.
    used_at = await _revoked_at(refresh_token)
    if used_at is not None:
        if sid and used_at and (datetime.now(timezone.utc).timestamp() - used_at) > REUSE_GRACE_SECONDS:
            await revoke_session(sid)
        raise revoked

    user = await users.find_one({"id": user_id}, {"tokens_valid_after": 1})
    if not user or _issued_before_cutoff(payload, user):
        raise revoked

    # Blacklist immediately before issuing replacement — prevents replay
    await blacklist_token(refresh_token)
    return issue_tokens(user_id, sid)


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
    except PyJWTError:
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

    if await is_session_revoked(payload.get("sid")):
        raise HTTPException(status_code=401, detail="Session has ended. Please log in again.")

    user = await users.find_one({"id": user_id})
    if not user:
        raise HTTPException(
            status_code=404,
            detail="User not found.",
        )
    if _issued_before_cutoff(payload, user):
        raise HTTPException(status_code=401, detail="You signed out everywhere. Please log in again.")

    structlog.contextvars.bind_contextvars(user_id=user_id)
    return user


async def require_platform_staff(user: dict = Depends(get_current_user)) -> dict:
    """FastAPI dependency: the authenticated user must be Recast staff.

    Distinct from app.core.workspace.require(permission), which gates on a
    *workspace* role (owner/admin of the one workspace in the request
    context). This gates on the *platform* — for routes that return
    cross-tenant data (e.g. the Ops LLM Health page's aggregate Groq/Gemini
    usage, latency and error messages spanning every workspace on this
    server, not just the caller's own). A paying customer who owns a
    workspace is not Recast staff, and must never reach those routes just
    by being a workspace owner.

    is_master_admin also passes (a strictly broader grant — full Ops
    Dashboard access on any workspace, not just read access to this
    cross-tenant data; see app.core.workspace.require_ops_admin).

    No bootstrap UI exists yet — flip the flag directly in Mongo for the
    first account:
        db.users.update_one({"email": "you@example.com"},
                             {"$set": {"is_platform_staff": True}})

    Args:
        user: Authenticated user document, from get_current_user.

    Returns:
        The same user document, once confirmed to be platform staff.

    Raises:
        HTTPException 403: neither is_platform_staff nor is_master_admin is set.
    """
    if not user.get("is_platform_staff") and not user.get("is_master_admin"):
        raise HTTPException(status_code=403, detail="Platform staff access required.")
    return user