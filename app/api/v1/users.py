"""User profile routes — read and update the authenticated user's profile."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pymongo.errors import DuplicateKeyError

from app.core.auth import get_current_user, is_username_taken
from app.core.middleware import limiter
from app.db.mongo import users
from app.models.user import PublicProfileResponse, UserProfileResponse, UserUpdateBody

router = APIRouter()


def _build_profile_response(user: dict) -> UserProfileResponse:
    """Convert a raw MongoDB user document to UserProfileResponse.

    Args:
        user: Raw dict from MongoDB.

    Returns:
        UserProfileResponse instance.
    """
    from app.models.user import UserPlan

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


@router.get("/me", response_model=UserProfileResponse)
@limiter.limit("100/minute")
async def get_my_profile(
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> UserProfileResponse:
    """Return the authenticated user's profile.

    Args:
        request: FastAPI Request (required by slowapi).
        current_user: Full user document injected by get_current_user.

    Returns:
        UserProfileResponse with all non-sensitive fields.
    """
    return _build_profile_response(current_user)


@router.put("/me", response_model=UserProfileResponse)
@limiter.limit("20/minute")
async def update_my_profile(
    request: Request,
    body: UserUpdateBody,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> UserProfileResponse:
    """Update allowed fields on the authenticated user's profile.

    Email, phone, plan, and credits fields cannot be updated here.
    Username uniqueness is enforced; DuplicateKeyError from MongoDB is caught.

    Args:
        request: FastAPI Request (required by slowapi).
        body: Partial update payload.
        current_user: Full user document injected by get_current_user.

    Returns:
        Updated UserProfileResponse.

    Raises:
        HTTPException 409: If the new username is already taken.
    """
    update_fields: dict[str, Any] = {}

    for field in ("name", "bio", "website", "avatar_url", "timezone", "language"):
        value = getattr(body, field, None)
        if value is not None:
            update_fields[field] = value

    if body.preferred_platforms is not None:
        update_fields["preferred_platforms"] = body.preferred_platforms

    if body.username is not None:
        new_username = body.username.lower()
        if new_username != current_user.get("username"):
            if await is_username_taken(new_username):
                raise HTTPException(status_code=409, detail="Username is already taken.")
            update_fields["username"] = new_username

    if not update_fields:
        return _build_profile_response(current_user)

    try:
        await users.update_one(
            {"id": current_user["id"]},
            {"$set": update_fields},
        )
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Username is already taken.",
        )

    updated = await users.find_one({"id": current_user["id"]})
    if not updated:
        raise HTTPException(status_code=404, detail="User not found.")

    return _build_profile_response(updated)


@router.get("/{username}", response_model=PublicProfileResponse)
@limiter.limit("100/minute")
async def get_public_profile(
    request: Request,
    username: str,
) -> PublicProfileResponse:
    """Return the public profile for any user by username.

    No authentication required.  All private fields are stripped.

    Args:
        request: FastAPI Request (required by slowapi).
        username: Target user's username (case-insensitive).

    Returns:
        PublicProfileResponse with only public fields.

    Raises:
        HTTPException 404: If no user exists with the given username.
    """
    user = await users.find_one({"username": username.lower()})
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    return PublicProfileResponse(
        username=user["username"],
        name=user["name"],
        avatar_url=user.get("avatar_url", ""),
        bio=user.get("bio", ""),
        website=user.get("website", ""),
    )
