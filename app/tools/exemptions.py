"""Routes that are deliberately not tools, each with the reason.

A rule is (method or "*", a path prefix, the reason). The reason names why the route stays out: it is for staff, a public page, a
device or render machine, a sign-in or account screen, money, people and access, or a destructive action that stays in the app. The
coverage test checks that every rule still matches a route, so a rule cannot outlive the route it covered.
"""

from __future__ import annotations

from typing import Optional

STAFF = "staff only (Ops)"
PUBLIC = "public page or form, not a member's action"
DEVICE = "a device, render machine or internal screen route"
ACCOUNT = "sign-in, account or support screen"
MONEY = "money stays in the app (plans, payments, refunds)"
PEOPLE = "people, roles and workspace settings stay in the app"
DANGER = "destructive or legal, stays in the app"
STUB = "a legacy stub that answers which route to use instead"
CONNECT = "connecting an account or saving a secret needs the person's own sign-in, so it stays in the app"
JOBS = "the generic action door; every action it can start is its own tool"
STREAM = "a live stream or session a screen listens to; the same work is a tool that runs in the background (see runs)"
OWNER = "an owner-only control over the whole workspace or its keys, so it stays in the app and a key cannot reach it"
APPROVAL = "a person's own yes for a call made from outside; it is how a key gets one, so a key can never reach it"
HISTORY = "the person's own view of what connected apps changed, and undo from the app; the same records and undo as changes.list and changes.undo"
ASK = "Ask Ekavo is itself a way in to the tools (it plans and runs them for the person), so it is not a tool for another way in to call"
CHANNELS = "linking a chat app to yourself is done by the person in the app; the chat then acts as them through the same tools, so it is not a tool itself"
SESSION = "the person signing a command line or assistant in, and seeing and revoking those sign-ins, is done by the person in the app and never by a key"
FILE = "needs the file itself (an upload or a download), so it stays in the app; the link to the file is in the tool that shows the item"

# (method, path prefix, reason). "*" matches every method. Longest matching prefix wins, so a narrow rule can override a wide one.
# A prefix ending in "$" matches that exact path only (so "/api/v1/audio$" does not also match "/api/v1/audio-assets").
RULES: list[tuple[str, str, str]] = [
    ("*", "/api/v1/ops/", STAFF),
    ("*", "/api/v1/public/", PUBLIC),
    ("*", "/api/v1/waitlist", PUBLIC),
    ("*", "/api/v1/contact", PUBLIC),
    ("*", "/api/v1/share/", PUBLIC),
    ("*", "/api/v1/worker/", DEVICE),
    ("*", "/api/v1/push", DEVICE),
    ("*", "/api/v1/thumbnails", DEVICE),
    ("*", "/api/v1/suggest", DEVICE),
    ("*", "/api/v1/post-targets", DEVICE),
    ("*", "/api/v1/audio$", STUB),
    ("*", "/api/v1/jobs", JOBS),
    ("POST", "/api/v1/audio-assets/upload", FILE),
    ("POST", "/api/v1/image-assets/upload", FILE),
    ("POST", "/api/v1/image-assets/{image_asset_id}/slides/{slide_number}/looks/cutout", FILE),
    ("GET", "/api/v1/image-assets/{image_asset_id}/slides/{slide_number}/original", FILE),
    ("POST", "/api/v1/media$", FILE),
    ("DELETE", "/api/v1/brand/{brand_id}$", DANGER),
    ("POST", "/api/v1/campaigns/source-file", FILE),
    ("POST", "/api/v1/campaigns/{campaign_id}/thumbnail", FILE),
    ("GET", "/api/v1/campaigns/{campaign_id}/export", FILE),
    ("POST", "/api/v1/video-media/uploads", FILE),
    ("GET", "/api/v1/video-media/uploads/", FILE),
    ("POST", "/api/v1/video-media/uploads/", FILE),
    ("POST", "/api/v1/video-fonts", FILE),
    ("POST", "/api/v1/video-projects/{project_id}/import-timeline", FILE),
    ("GET", "/api/v1/video-projects/{project_id}/handoff/", FILE),
    ("GET", "/api/v1/video-narrate/captions/{media_id}/", FILE),
    ("GET", "/api/v1/video-sfx/files/", PUBLIC),
    ("GET", "/api/v1/audio-assets/feed/{token}.xml", PUBLIC),
    ("*", "/api/v1/video-review-public/", PUBLIC),
    ("*", "/api/v1/pipeline/", STREAM),
    ("*", "/api/v1/developer/", OWNER),
    ("*", "/api/v1/tool-approvals", APPROVAL),
    ("*", "/api/v1/tool-changes", HISTORY),
    ("*", "/api/v1/tool-reach", HISTORY),
    ("*", "/api/v1/ask", ASK),
    ("*", "/api/v1/channels", CHANNELS),
    ("*", "/api/v1/tool-sessions", SESSION),
    ("POST", "/api/v1/supervisor/run", OWNER),
    ("POST", "/api/v1/supervisor/kill-switch", OWNER),
    ("GET", "/api/v1/activity/stream", STREAM),
    ("POST", "/api/v1/client-errors", DEVICE),
    ("POST", "/api/v1/destinations/wordpress/connect", CONNECT),
    ("POST", "/api/v1/destinations/ghost/connect", CONNECT),
    ("POST", "/api/v1/destinations/mailchimp/connect", CONNECT),
    ("*", "/api/v1/platforms/{key}/connection", CONNECT),
    ("*", "/api/v1/support/inbound-email", DEVICE),
    ("*", "/api/v1/auth/", ACCOUNT),
    ("*", "/api/v1/oauth/", ACCOUNT),
    ("*", "/api/v1/onboarding/", ACCOUNT),
    ("*", "/api/v1/support/", ACCOUNT),
    ("*", "/api/v1/invites/", PEOPLE),
]
# reads of the account, the workspace and billing can be tools (credits, plan, people's names are not shown); their writes stay in the app
for _method in ("POST", "PUT", "PATCH", "DELETE"):
    RULES.append((_method, "/api/v1/workspaces", PEOPLE))
RULES.append(("POST", "/api/v1/billing/", MONEY))
RULES.append(("PUT", "/api/v1/users/me", ACCOUNT))
RULES.append(("DELETE", "/api/v1/users/me", ACCOUNT))
RULES.append(("GET", "/api/v1/billing/approval/", MONEY))


def exempt_reason(route: str) -> Optional[str]:
    """The reason a route ("METHOD /path") stays out of the tool list, or None when it has not been decided."""
    method, path = route.split(" ", 1)
    best: Optional[tuple[int, str]] = None
    for rule_method, prefix, reason in RULES:
        if rule_method not in ("*", method):
            continue
        matches = path == prefix[:-1] if prefix.endswith("$") else path.startswith(prefix)
        if matches and (best is None or len(prefix) > best[0]):
            best = (len(prefix), reason)
    return best[1] if best else None
