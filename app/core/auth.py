"""JWT utilities and FastAPI bearer-token dependency."""

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from app.core.config import settings

_bearer = HTTPBearer()


def create_access_token(data: dict) -> str:
    """Encode a signed JWT containing *data* with a default expiry claim.

    Args:
        data: Arbitrary payload dict to embed in the token.

    Returns:
        Compact serialised JWT string.
    """
    payload = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )
    payload["exp"] = expire
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def verify_token(token: str) -> dict[str, Any]:
    """Decode and validate *token*, raising 401 on any failure.

    Args:
        token: Raw JWT string (no ``Bearer`` prefix).

    Returns:
        Decoded payload dictionary.

    Raises:
        HTTPException: 401 if the token is invalid or expired.
    """
    exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload: dict[str, Any] = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
        return payload
    except JWTError as jwt_err:
        raise exc from jwt_err


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> dict[str, Any]:
    """FastAPI dependency — extract and verify the incoming bearer token.

    Args:
        credentials: Injected by FastAPI from the ``Authorization`` header.

    Returns:
        Decoded JWT payload for the authenticated caller.
    """
    return verify_token(credentials.credentials)
