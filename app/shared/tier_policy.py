"""How a workspace's plan shapes its flow. One person working alone has nobody to sign off for and nobody to police, so a
single workspace skips the review step and the team governance. A duo keeps light team rules. A large workspace keeps the full
flow. A workspace can still switch the review step on or off itself (`require_review`), whatever its plan."""
import re
from typing import Any, Optional

from app.db.mongo import workspace_members, workspaces

# review: whether posts wait for approval by default. team_rules: whether Odette's team checks (seats, permissions, members
# joining and leaving) and its team screens apply. cross_creator: whether comparing creators makes sense. cohorts: whether
# grouping creators into cohorts is worth offering. no_self_approval: with a review step, whether the author may not approve
# their own post when someone else is able to. Left off for large so existing team workspaces behave exactly as before.
TIER_POLICY: dict[str, dict[str, bool]] = {
    "single": {"review": False, "team_rules": False, "cross_creator": False, "cohorts": False, "no_self_approval": False},
    "duo": {"review": False, "team_rules": True, "cross_creator": True, "cohorts": False, "no_self_approval": True},
    "large": {"review": True, "team_rules": True, "cross_creator": True, "cohorts": True, "no_self_approval": False},
}
_UNKNOWN = TIER_POLICY["large"]  # an unknown plan keeps everything on

# Odette rules left out of a plan, because they could not mean anything there. One person has no seats to exceed, no one to
# police and no one joining or leaving; the shared-signal check needs three or more people to be more than noise.
_TEAM_RULES = frozenset({"tier_seat_exceeded", "rbac_violation", "member_churn", "assistant_signal_storm"})
HIDDEN_RULES: dict[str, frozenset[str]] = {
    "single": _TEAM_RULES,
    "duo": frozenset({"assistant_signal_storm"}),
    "large": frozenset(),
}


def policy_for(workspace: Optional[dict]) -> dict[str, Any]:
    """The flow settings for a workspace document: its plan's defaults, with the member's own review choice on top."""
    ws = workspace or {}
    tier = str(ws.get("tier") or "")
    base = dict(TIER_POLICY.get(tier, _UNKNOWN))
    override = ws.get("require_review")
    review = base["review"] if override is None else bool(override)
    return {
        "tier": tier,
        "review_required": review,
        "review_is_default": override is None,
        "team_rules": base["team_rules"],
        "cross_creator": base["cross_creator"],
        "cohorts": base["cohorts"],
        "no_self_approval": review and base["no_self_approval"],
        "hidden_rules": sorted(HIDDEN_RULES.get(tier, frozenset())),
    }


async def workspace_policy(workspace_id: str) -> dict[str, Any]:
    return policy_for(await workspaces.find_one({"id": workspace_id}, {"tier": 1, "require_review": 1}))


async def review_required(workspace_id: str) -> bool:
    return (await workspace_policy(workspace_id))["review_required"]


async def default_approval(workspace_id: str) -> str:
    """The approval a new post starts with: waiting for review, or approved at once when the workspace has no review step."""
    return "pending" if await review_required(workspace_id) else "approved"


async def blocks_self_approval(workspace_id: str, approver_id: str, author_id: Optional[str]) -> bool:
    """True when this approver may not approve this post: the workspace has a review step with the no-self-approval rule, they
    wrote the post, and another owner or admin is there to do it instead. When they are the only one who can approve, they
    can, so nobody is ever left stuck."""
    policy = await workspace_policy(workspace_id)
    if not policy["no_self_approval"] or not author_id or author_id != approver_id:
        return False
    other = await workspace_members.find_one(
        {"workspace_id": workspace_id, "status": "active", "role": {"$in": ["owner", "admin"]}, "user_id": {"$ne": approver_id}},
        {"user_id": 1},
    )
    return other is not None


def plan_note(policy: dict[str, Any]) -> str:
    """A line for Odette's prompt saying who works in this workspace, so a briefing never recommends something that cannot apply."""
    if policy.get("team_rules") is False:
        return (
            "PLAN: this workspace has exactly one person, who is the owner and does all the work. Never recommend anything about "
            "team members, seats, roles, permissions, invitations, approvals or reviewing other people's work. Recommend only what "
            "one person can do: publishing rhythm, brand voice, platforms and results."
        )
    if policy.get("cohorts") is False:
        return (
            "PLAN: this workspace has two people. Keep recommendations simple and between the two of them. Do not suggest "
            "cohorts, groups of creators or formal review chains."
        )
    return ""


_TEAM_WORDS = re.compile(
    r"\b(seats?|roles?|permissions?|members?|teammates?|invit\w*|approvers?|approvals?|cohorts?|admins?|rbac|colleagues?)\b",
    re.IGNORECASE,
)


def mentions_team_topics(*texts: Optional[str]) -> bool:
    """True when any text talks about team matters (seats, roles, members, approvals...)."""
    return any(bool(t) and _TEAM_WORDS.search(t) for t in texts)
