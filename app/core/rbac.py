"""Permission-based RBAC — roles map to permission sets, not hardcoded checks."""

from fastapi import HTTPException
from app.db.mongo import workspace_members

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "owner": {
        "invite_members",
        "remove_members",
        "manage_roles",
        "edit_brand_voice",
        "manage_billing",
        "publish_content",
        "manage_workspace_settings",
    },
    "admin": {
        "invite_members",
        "remove_members",
        "edit_brand_voice",
        "publish_content",
    },
    "editor": {
        "publish_content",
    },
    "viewer": set(),
}


async def get_member(workspace_id: str, user_id: str) -> dict:
    """Fetch the caller's membership record for a workspace.

    Raises:
        HTTPException 403: If the user is not a member of the workspace.
    """
    member = await workspace_members.find_one(
        {"workspace_id": workspace_id, "user_id": user_id}
    )
    if not member:
        raise HTTPException(status_code=403, detail="Not a member of this workspace.")
    return member


async def require_permission(workspace_id: str, user_id: str, permission: str) -> dict:
    """Verify the caller's role grants the given permission.

    Args:
        workspace_id: Target workspace ID.
        user_id: Caller's user ID.
        permission: Permission string, e.g. "invite_members".

    Returns:
        The caller's WorkspaceMember dict, if permitted.

    Raises:
        HTTPException 403: Not a member, or role lacks the permission.
    """
    member = await get_member(workspace_id, user_id)
    allowed = ROLE_PERMISSIONS.get(member["role"], set())
    if permission not in allowed:
        raise HTTPException(
            status_code=403,
            detail=f"Role '{member['role']}' does not have permission: {permission}.",
        )
    return member