"""Where a connected account's posts can go, per platform, and whether Ops has opened each choice.

Posts go to the connected account unless the member chooses otherwise. A platform only appears here when it really has a choice
(LinkedIn: the profile or a Company Page; Facebook: a Page). Everything else posts to the one connected account and offers no choice.
Each choice has a default (on or off) and Ops can switch it per platform; a choice that needs the platform's own approval, such as
LinkedIn Company Pages, starts off until that approval has come."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.core.config import settings


@dataclass(frozen=True)
class DestinationKind:
    kind: str
    label: str
    #: Whether Ops can switch it. The connected account itself (the default) can never be switched off.
    switchable: bool
    #: Whether the platform has to approve the app for it first.
    needs_approval: bool
    note: str


#: platform key -> the choices beyond "the connected account itself"
SUPPORT: dict[str, list[DestinationKind]] = {
    "linkedin": [
        DestinationKind(
            "organization", "Company Pages", True, True,
            "Needs LinkedIn to approve the app for the w_organization_social and r_organization_admin permissions first.",
        ),
    ],
    "facebook": [
        DestinationKind("page", "Facebook Pages", False, False, "Facebook only posts to Pages, so this is always on."),
    ],
}


def kinds_for(platform: str) -> list[DestinationKind]:
    return SUPPORT.get(platform, [])


def default_enabled(platform: str, kind: str) -> bool:
    """What a choice is when Ops has not set it."""
    if platform == "linkedin" and kind == "organization":
        return bool(settings.LINKEDIN_ORG_POSTING_ENABLED)
    return any(k.kind == kind and not k.needs_approval for k in kinds_for(platform))


def effective(ops: Optional[dict], platform: str, kind: str) -> bool:
    """Whether a choice is open: Ops's setting when there is one, else its default. A choice the platform does not have is never open."""
    support = next((k for k in kinds_for(platform) if k.kind == kind), None)
    if support is None:
        return False
    if not support.switchable:
        return True
    saved = ((ops or {}).get("destinations") or {}).get(kind)
    return bool(saved) if saved is not None else default_enabled(platform, kind)


async def destination_enabled(platform: str, kind: str) -> bool:
    from app.pipelines.platform_ops.store import get_ops
    from app.platforms.base import get_platform, import_all

    import_all()
    definition = get_platform(platform)
    if definition is None:
        return False
    return effective(await get_ops(definition), platform, kind)
