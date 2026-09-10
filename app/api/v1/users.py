"""User profile routes — read and update the authenticated user's profile."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pymongo.errors import DuplicateKeyError

from app.core.auth import get_current_user, is_username_taken
from app.core.middleware import limiter
from app.db.mongo import users, workspace_members, workspaces
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
        email=user.get("email", ""),                        
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
        social_accounts=user.get("social_accounts", []),
        auth_identifiers=user.get("auth_identifiers", []),
        default_workspace_id=user.get("default_workspace_id"),
        last_active=user.get("last_active"),
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

    if body.default_workspace_id is not None:
        member = await workspace_members.find_one(
            {"workspace_id": body.default_workspace_id, "user_id": current_user["id"]}
        )
        if not member or member.get("status") != "active":
            raise HTTPException(
                status_code=403, detail="Not a member of that workspace."
            )
        update_fields["default_workspace_id"] = body.default_workspace_id

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


@router.get("/me/workspaces")
@limiter.limit("100/minute")
async def list_my_workspaces(
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """List every workspace the authenticated user is an active member of.

    Each entry carries the caller's role and whether it is their current default.
    """
    memberships = await workspace_members.find(
        {"user_id": current_user["id"], "status": "active"}
    ).to_list(length=200)

    ws_ids = [m["workspace_id"] for m in memberships]
    ws_docs = {
        w["id"]: w
        for w in await workspaces.find({"id": {"$in": ws_ids}}).to_list(length=200)
    }
    default_id = current_user.get("default_workspace_id")

    items = []
    for m in memberships:
        w = ws_docs.get(m["workspace_id"])
        if not w:
            continue
        items.append(
            {
                "workspace_id": w["id"],
                "name": w["name"],
                "tier": w.get("tier"),
                "is_personal": w.get("is_personal", False),
                "role": m["role"],
                "is_default": w["id"] == default_id,
            }
        )
    return {"items": items}


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
        email=user['email'],
        avatar_url=user.get("avatar_url", ""),
        bio=user.get("bio", ""),
        website=user.get("website", ""),
    )
