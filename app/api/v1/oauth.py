"""
OAuth endpoints — connect, callback, disconnect, list accounts.
Handles all platform OAuth flows through one unified router.
"""

import json
import logging
from contextvars import ContextVar
import re
import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from html import escape
from pydantic import BaseModel
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
import httpx
from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.pipelines.publish.registry import get_publisher
from app.core.config import settings
from app.db.mongo import users
from app.db.redis import get_redis
from app.pipelines.publish.token_store import (
    save_token,
    delete_token,
    get_all_tokens,
)

router = APIRouter()
logger = logging.getLogger(__name__)

# QA-004: was an in-process dict — leaked memory forever for any state that
# never got a callback (abandoned OAuth flows), and broke entirely the moment
# more than one app instance was running (state created on instance A, callback
# hits instance B, lookup fails). Redis with a TTL fixes both: entries expire
# on their own, and any instance can consume a state created by any other.
_OAUTH_STATE_TTL_SECONDS = 15 * 60
_OAUTH_STATE_KEY_PREFIX = "oauth:state:"


class BlueskyConnectRequest(BaseModel):
    handle: str
    app_password: str


# The website that started the connection, so the pop-up reports back to it (the app can be opened from more than one address).
_popup_origin: ContextVar[str | None] = ContextVar("oauth_popup_origin", default=None)


def _known_origin(origin: str | None) -> str | None:
    """The origin when it is one of the addresses allowed to call the API, else None."""
    from app.core.origins import build_origins, clean

    value = clean(origin or "")
    allowed = build_origins(
        production=settings.ENVIRONMENT == "production", production_domain=settings.PRODUCTION_DOMAIN,
        frontend_url=settings.FRONTEND_URL, allowed=settings.ALLOWED_ORIGINS,
    )
    return value if value and value in allowed else None


def _opener_origin(request: Request) -> str | None:
    """The website that is making this request (the one that will open the pop-up), when it is an allowed one."""
    return _known_origin(request.headers.get("origin"))


async def _create_state(user_id: str, platform: str, workspace_id: str, origin: str | None = None) -> str:
    state = secrets.token_urlsafe(32)
    client = await get_redis()
    await client.set(
        f"{_OAUTH_STATE_KEY_PREFIX}{state}",
        json.dumps({
            "user_id": user_id,
            "platform": platform,
            "workspace_id": workspace_id,
            "origin": origin,
        }),
        ex=_OAUTH_STATE_TTL_SECONDS,
    )
    return state


async def _consume_state(state: str) -> dict | None:
    client = await get_redis()
    # GETDEL: atomic read-and-delete, so a state token can never be replayed
    # even under concurrent callback requests.
    raw = await client.getdel(f"{_OAUTH_STATE_KEY_PREFIX}{state}")
    if raw is None:
        logger.error("State NOT FOUND or already consumed/expired — state=%s", state[:20])
        return None
    data = json.loads(raw)
    # From here on this request's pop-up page reports back to the website that started the connection.
    _popup_origin.set(_known_origin(data.get("origin")))
    return data


# A Google account has no public profile page; this is the person's own account page, the closest thing to one.
GOOGLE_ACCOUNT_URL = "https://myaccount.google.com/"


def _derive_profile_url(platform: str, username: str, platform_user_id: str = "") -> str | None:
    """Best-effort public profile URL from data already returned by the OAuth
    flow — no extra API calls. None where the platform doesn't expose a
    reliable public-profile pattern from what we have (LinkedIn's OAuth
    response has no public vanity URL; Google's "username" is an email/name,
    not a public profile slug)."""
    name = _clean_profile_part(username)
    ident = _clean_profile_part(platform_user_id)
    if platform == "instagram" and name:
        return f"https://www.instagram.com/{quote(name, safe='')}"
    if platform == "facebook" and ident:
        return f"https://www.facebook.com/{quote(ident, safe='')}"
    if platform == "threads" and name:
        return f"https://www.threads.com/@{quote(name, safe='')}"
    if platform == "youtube" and ident:
        return f"https://www.youtube.com/channel/{quote(ident, safe='')}"
    if platform == "bluesky" and name:
        return f"https://bsky.app/profile/{quote(name, safe='')}"
    if platform == "reddit" and name:
        return f"https://www.reddit.com/user/{quote(name, safe='')}"
    if platform == "twitter" and name:
        return f"https://x.com/{quote(name, safe='')}"
    return None


_PROFILE_PART_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _clean_profile_part(value: str | None) -> str | None:
    """A username or id with surrounding whitespace and a leading "@" removed,
    or None when it is empty or holds anything beyond letters, digits, dot,
    underscore and hyphen."""
    cleaned = (value or "").strip().lstrip("@").strip()
    return cleaned if _PROFILE_PART_RE.match(cleaned) else None


_PROFILE_HOST_REWRITES = {
    "threads.net": "www.threads.com",
    "www.threads.net": "www.threads.com",
    "threads.com": "www.threads.com",
    "instagram.com": "www.instagram.com",
    "facebook.com": "www.facebook.com",
    "reddit.com": "www.reddit.com",
}


def normalize_profile_url(url: str | None) -> str | None:
    """Rewrite a saved profile address from an older form to the current one.
    Returns None for anything that is not an http(s) address."""
    if not url or not isinstance(url, str):
        return None
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    if re.search(r"%20|\s", parts.path):
        return None
    host = parts.netloc.lower()
    host = _PROFILE_HOST_REWRITES.get(host, host)
    return urlunsplit(("https", host, parts.path, parts.query, parts.fragment))


async def _require_room_for_any(platforms: list[str], workspace_id: str) -> None:
    """Before a sign-in starts: there must be room for at least one account it would add, unless every one is already connected."""
    from app.shared.entitlements import connection_room_message, require_connection_room

    for platform in platforms:
        if await connection_room_message(workspace_id, platform) is None:
            return
    await require_connection_room(workspace_id, platforms[0])


async def _room_note(workspace_id: str, platform: str) -> str | None:
    from app.shared.entitlements import connection_room_message

    return await connection_room_message(workspace_id, platform)


def _js(value: str) -> str:
    """A value as a JavaScript string literal that is safe inside a script tag: every angle bracket is written as an escape, so
    nothing in it can end the script or start a tag."""
    return json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e")


def _oauth_popup_response(success: bool, message: str) -> HTMLResponse:
    """The OAuth callback runs inside the small popup window
    openOAuthPopup() (Frontend/Recast/lib/api/social.connect.ts) opened —
    that helper never reads the callback's response body, it only polls
    popup.closed and then re-fetches the real connected-accounts list. A
    bare JSON dict left the popup permanently showing raw JSON with
    nothing to close it. This renders a minimal self-closing page instead,
    for both the success and every error path in the four OAuth callback
    routes (meta/threads/google/generic-LinkedIn)."""
    title = "Connected" if success else "Connection failed"
    color = "#16a34a" if success else "#dc2626"
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title></head>
<body style="font-family: system-ui, sans-serif; display:flex; align-items:center; justify-content:center; height:100vh; margin:0; background:#0D0D12; color:#e5e7eb;">
  <div style="text-align:center; max-width:420px; padding:0 24px;">
    <p style="color:{color}; font-size:16px; font-weight:600;">{escape(message)}</p>
    <p style="color:#6b7280; font-size:13px;">This window will close automatically.</p>
  </div>
  <script>
    try {{
      if (window.opener) window.opener.postMessage({{ type: "recast_oauth", success: {json.dumps(success)}, message: {_js(message)} }}, {_js(_popup_origin.get() or settings.FRONTEND_URL.rstrip("/"))});
    }} catch (e) {{}}
    setTimeout(function() {{ window.close(); }}, 1500);
  </script>
</body></html>"""
    return HTMLResponse(content=html, status_code=200 if success else 400)

# ─────────────────────────────────────────────────────────────────────────────
# SPECIFIC ROUTES FIRST — before any /{platform} wildcards
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/accounts")
@limiter.limit("30/minute")
async def list_accounts(
    request: Request,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    """List all connected social accounts for the active workspace."""
    accounts = await get_all_tokens(ctx.workspace_id)

    connector_ids = [a["connected_by"] for a in accounts if a.get("connected_by")]
    connectors = {
        u["id"]: u
        for u in await users.find(
            {"id": {"$in": connector_ids}}, {"id": 1, "name": 1}
        ).to_list(length=100)
    }
    for a in accounts:
        a["connected_by_name"] = connectors.get(a.get("connected_by", ""), {}).get("name", "")
        a["profile_url"] = normalize_profile_url(a.get("profile_url"))
        if a["platform"] == "instagram" and not a["profile_url"]:
            await _heal_instagram(ctx.workspace_id, a)
    # A Google account has no public profile: its icon opens the YouTube channel when there is one, else the account page.
    youtube = next((a for a in accounts if a["platform"] == "youtube" and a.get("profile_url")), None)
    for a in accounts:
        if a["platform"] == "google" and not a["profile_url"]:
            a["profile_url"] = youtube["profile_url"] if youtube else GOOGLE_ACCOUNT_URL

    # Accounts with no picture yet are read from their platform in the background, so this list is never held up.
    from app.pipelines.publish.profile import needs_refresh, schedule_refresh

    schedule_refresh(ctx.workspace_id, [a["platform"] for a in accounts if needs_refresh(a)])
    for a in accounts:
        a.pop("profile_checked_at", None)

    return {
        "accounts": accounts,
        "total":    len(accounts),
    }


async def _heal_instagram(workspace_id: str, account: dict) -> None:
    """An Instagram connection saved before the handle was looked up carries the Facebook display name and no usable address. Ask Instagram
    for the real handle once, save it, and show it. Best effort: the list is returned either way."""
    try:
        from app.db.mongo import workspace_connections
        from app.pipelines.publish.meta.oauth import fetch_instagram_username
        from app.pipelines.publish.token_store import get_token

        token = await get_token(workspace_id, "instagram")
        handle = await fetch_instagram_username(account.get("platform_user_id") or "", token["access_token"]) if token else None
        url = _derive_profile_url("instagram", handle or "")
        if handle and url:
            await workspace_connections.update_one(
                {"workspace_id": workspace_id, "platform": "instagram"}, {"$set": {"username": handle, "profile_url": url}},
            )
            account["username"], account["profile_url"] = handle, url
    except Exception as exc:  # noqa: BLE001
        logger.warning("Instagram handle repair failed for %s: %s", workspace_id, exc)


@router.post("/bluesky/connect")
@limiter.limit("10/minute")
async def connect_bluesky(
    request: Request,
    body: BlueskyConnectRequest,
    ctx: WorkspaceContext = Depends(require("manage_connections")),
) -> dict:
    """
    Connect a Bluesky account to the active workspace using handle + app password.
    No OAuth redirect needed — ATP handles auth directly.
    """
    await _require_room_for_any(["bluesky"], ctx.workspace_id)
    from app.pipelines.publish.bluesky.publisher import BlueSkyPublisher

    publisher = BlueSkyPublisher()

    try:
        token_data = await publisher.exchange_token(
            f"{body.handle}|{body.app_password}"
        )
    except Exception as exc:
        logger.error("Bluesky connect failed for workspace %s: %s", ctx.workspace_id, exc)
        raise HTTPException(
            status_code=400,
            detail="Failed to connect Bluesky. Check your handle and app password.",
        )

    await save_token(
        workspace_id=ctx.workspace_id,
        platform="bluesky",
        access_token=token_data["access_token"],
        refresh_token=token_data.get("refresh_token"),
        expires_at=token_data.get("expires_at"),
        platform_user_id=token_data.get("platform_user_id", ""),
        username=token_data.get("username", body.handle),
        connected_by=ctx.user_id,
        profile_url=_derive_profile_url("bluesky", token_data.get("username", body.handle)),
    )

    return {
        "platform":  "bluesky",
        "connected": True,
        "username":  token_data.get("username", body.handle),
        "did":       token_data.get("platform_user_id", ""),
        "message":   "Bluesky connected successfully.",
    }


@router.get("/meta/connect")
@limiter.limit("10/minute")
async def connect_meta(
    request: Request,
    ctx: WorkspaceContext = Depends(require("manage_connections")),
) -> dict:
    """
    Start Meta OAuth flow — covers Instagram + Threads + Facebook.
    One connect flow, three platforms connected simultaneously.
    """
    await _require_room_for_any(["instagram", "facebook"], ctx.workspace_id)
    from app.pipelines.publish.meta.oauth import build_auth_url
    state    = await _create_state(ctx.user_id, "meta", ctx.workspace_id, _opener_origin(request))
    auth_url = build_auth_url(state, platform="meta")

    return {
        "platform": "meta",
        "covers":   ["instagram", "threads", "facebook"],
        "auth_url": auth_url,
        "message":  "Redirect user to auth_url to connect Instagram, Threads, and Facebook",
    }


@router.get("/meta/callback")
@limiter.limit("10/minute")
async def meta_callback(
    request: Request,
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None),
    error_code: str = Query(None),
    error_message: str = Query(None),
) -> HTMLResponse:
    """
    Handle Meta OAuth callback.
    Saves tokens for Instagram and Facebook only.
    Threads handled separately via /api/v1/oauth/threads/connect.
    """
    # Handle Meta error response
    if error_code or error:
        return _oauth_popup_response(False, f"Meta OAuth error {error_code}: {error_message or error}")

    if not code or not state:
        return _oauth_popup_response(False, "Missing code or state from Meta callback.")

    # State format is "token|platform" — extract token only
    state_parts = state.split("|", 1)
    state_token = state_parts[0]

    state_data = await _consume_state(state_token)
    if not state_data:
        return _oauth_popup_response(False, "Invalid or expired OAuth state. Please try connecting again.")

    user_id = state_data["user_id"]
    workspace_id = state_data.get("workspace_id", "")

    try:
        from app.pipelines.publish.meta.oauth import exchange_code
        token_data = await exchange_code(code, platform="meta")
    except Exception as exc:
        logger.error("Meta token exchange failed for user %s: %s", user_id, exc)
        return _oauth_popup_response(False, "Failed to connect Meta. Please try again.")

    connected = []
    limit_notes: list[str] = []

    # ── Instagram — only if Business account linked to Facebook Page ──────
    ig_full = await _room_note(workspace_id, "instagram") if token_data.get("ig_user_id") else None
    if ig_full:
        limit_notes.append(f"Instagram was not added. {ig_full}")
    if token_data.get("ig_user_id") and not ig_full:
        await save_token(
            workspace_id=workspace_id,
            platform="instagram",
            access_token=token_data["access_token"],
            refresh_token=None,
            expires_at=token_data.get("expires_at"),
            platform_user_id=token_data["ig_user_id"],
            # The Instagram handle, not the Facebook person's display name (the display name is only the fallback label).
            username=token_data.get("ig_username") or token_data.get("username", ""),
            connected_by=user_id,
            profile_url=_derive_profile_url("instagram", token_data.get("ig_username") or ""),
        )
        connected.append("instagram")
        logger.info("Instagram connected for user %s", user_id)
    else:
        logger.info(
            "No Instagram Business account found for user %s — "
            "user must link Instagram Business account to their Facebook Page",
            user_id,
        )

    # ── Facebook — only if user manages at least one Page ────────────────
    pages = token_data.get("pages", [])
    fb_full = await _room_note(workspace_id, "facebook") if pages else None
    if fb_full:
        limit_notes.append(f"Facebook was not added. {fb_full}")
    if pages and not fb_full:
        first_page = pages[0]
        await save_token(
            workspace_id=workspace_id,
            platform="facebook",
            access_token=first_page["access_token"],
            refresh_token=None,
            expires_at=token_data.get("expires_at"),
            platform_user_id=first_page["id"],
            username=first_page.get("name", ""),
            connected_by=user_id,
            profile_url=_derive_profile_url("facebook", first_page.get("name", ""), first_page["id"]),
        )
        connected.append("facebook")
        # Every Page this sign-in can post to is kept (with its own token and linked Instagram account), so the member can switch
        # to another one later without signing in again. The first Page is used until they choose.
        from app.db.mongo import workspace_connections
        from app.pipelines.publish.token_store import encrypt_token

        await workspace_connections.update_one(
            {"workspace_id": workspace_id, "platform": "facebook"},
            {"$set": {"meta_pages": [
                {
                    "id": p["id"], "name": p.get("name", ""), "access_token": encrypt_token(p["access_token"]),
                    "ig_user_id": (p.get("instagram_business_account") or {}).get("id"),
                }
                for p in pages if p.get("id") and p.get("access_token")
            ]}},
        )
        logger.info(
            "Facebook connected for user %s — page: %s (%d Pages available)",
            user_id, first_page.get("name"), len(pages),
        )
    else:
        logger.info(
            "No Facebook Pages found for user %s — "
            "user must create or manage a Facebook Page",
            user_id,
        )

   

    logger.info("Meta callback complete for user %s — connected: %s", user_id, connected)

    if connected:
        return _oauth_popup_response(True, f"Connected: {', '.join(connected)}" + (f". {' '.join(limit_notes)}" if limit_notes else ""))
    if limit_notes:
        return _oauth_popup_response(False, " ".join(limit_notes))

    notes = []
    if "instagram" not in connected:
        notes.append(
            "Instagram: switch to a Business/Creator account and link it to your "
            "Facebook Page under Instagram Settings → Account → Linked Accounts."
        )
    if "facebook" not in connected:
        notes.append("Facebook: no Facebook Pages found — create one at facebook.com/pages/create.")
    return _oauth_popup_response(False, "No platforms connected. " + " ".join(notes))

class ChooseFacebookPageRequest(BaseModel):
    page_id: str


@router.put("/facebook/page")
@limiter.limit("20/minute")
async def choose_facebook_page(
    request: Request,
    body: ChooseFacebookPageRequest,
    ctx: WorkspaceContext = Depends(require("manage_connections")),
) -> dict:
    """Switch which Facebook Page posts go to, among the Pages this sign-in can post to. If that Page has an Instagram Business account
    linked, Instagram follows it; if not, Instagram stays as it was and the answer says so."""
    from app.db.mongo import workspace_connections
    from app.pipelines.publish.token_store import decrypt_token, get_token

    connection = await workspace_connections.find_one({"workspace_id": ctx.workspace_id, "platform": "facebook"})
    page = next((p for p in (connection or {}).get("meta_pages") or [] if p.get("id") == body.page_id), None)
    if not connection or not page:
        raise HTTPException(status_code=404, detail="That Page isn't one this sign-in can post to. Connect Facebook again to refresh the list.")

    await save_token(
        workspace_id=ctx.workspace_id, platform="facebook", access_token=decrypt_token(page["access_token"]), refresh_token=None,
        expires_at=connection.get("expires_at"), platform_user_id=page["id"], username=page.get("name", ""),
        connected_by=connection.get("connected_by", ""), profile_url=_derive_profile_url("facebook", page.get("name", ""), page["id"]),
    )
    # The name and picture shown for the account are read again for the new Page.
    await workspace_connections.update_one(
        {"workspace_id": ctx.workspace_id, "platform": "facebook"},
        {"$unset": {"display_name": "", "avatar_url": "", "followers": "", "profile_checked_at": ""}},
    )
    note = None
    instagram = await get_token(ctx.workspace_id, "instagram")
    if instagram and page.get("ig_user_id"):
        from app.pipelines.publish.meta.oauth import fetch_instagram_username

        handle = await fetch_instagram_username(page["ig_user_id"], instagram["access_token"])
        await workspace_connections.update_one(
            {"workspace_id": ctx.workspace_id, "platform": "instagram"},
            {"$set": {
                "platform_user_id": page["ig_user_id"], **({"username": handle, "profile_url": _derive_profile_url("instagram", handle)} if handle else {}),
            }, "$unset": {"display_name": "", "avatar_url": "", "followers": "", "profile_checked_at": ""}},
        )
    elif instagram:
        note = "That Page has no Instagram Business account linked, so Instagram stays on the account it had."
    return {"platform": "facebook", "page_id": page["id"], "page_name": page.get("name", ""), "note": note}


@router.get("/threads/connect")
@limiter.limit("10/minute")
async def connect_threads(
    request: Request,
    ctx: WorkspaceContext = Depends(require("manage_connections")),
) -> dict:
    """
    Start Threads OAuth flow — separate from Facebook Login.
    Threads uses threads.net/oauth/authorize not Facebook.
    """
    await _require_room_for_any(["threads"], ctx.workspace_id)
    state    = await _create_state(ctx.user_id, "threads", ctx.workspace_id, _opener_origin(request))
    params   = {
        "client_id":     settings.THREADS_APP_ID,
        "redirect_uri":  settings.THREADS_REDIRECT_URI,
        # threads_manage_insights — without it, the analytics fetcher's
        # /threads_insights calls 4xx for every workspace connected before
        # this scope was added (they need to reconnect for it to apply).
        "scope":         "threads_basic,threads_content_publish,threads_manage_insights",
        "response_type": "code",
        "state":         state,
    }
    auth_url = f"https://threads.net/oauth/authorize?{urlencode(params)}"

    return {
        "platform": "threads",
        "auth_url": auth_url,
        "message":  "Redirect user to auth_url to connect Threads",
    }


@router.get("/threads/callback")
@limiter.limit("10/minute")
async def threads_callback(
    request: Request,
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None),
) -> HTMLResponse:
    """Handle Threads OAuth callback."""
    if error:
        return _oauth_popup_response(False, f"Threads OAuth denied: {error}")

    if not code or not state:
        return _oauth_popup_response(False, "Missing code or state.")

    state_data = await _consume_state(state)
    if not state_data:
        return _oauth_popup_response(False, "Invalid or expired state.")

    user_id = state_data["user_id"]
    workspace_id = state_data.get("workspace_id", "")

    try:
        async with httpx.AsyncClient() as client:
            # Step 1 — Exchange code for short-lived token
            token_response = await client.post(
                "https://graph.threads.net/oauth/access_token",
                data={
                    "client_id":     settings.THREADS_APP_ID,
                    "client_secret": settings.THREADS_APP_SECRET,
                    "redirect_uri":  settings.THREADS_REDIRECT_URI,
                    "code":          code,
                    "grant_type":    "authorization_code",
                },
            )
            token_response.raise_for_status()
            token_data = token_response.json()

            # Step 2 — Exchange short-lived token for long-lived token (60 days)
            ll_response = await client.get(
                "https://graph.threads.net/access_token",
                params={
                    "grant_type":    "th_exchange_token",
                    "client_secret": settings.THREADS_APP_SECRET,
                    "access_token":  token_data["access_token"],
                },
            )
            ll_response.raise_for_status()
            ll_data = ll_response.json()

            access_token = ll_data["access_token"]
            expires_in   = ll_data.get("expires_in", 5184000)  # default 60 days
            expires_at   = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

            # Step 3 — Get Threads profile
            profile_response = await client.get(
                "https://graph.threads.net/v1.0/me",
                params={
                    "fields":       "id,username,name",
                    "access_token": access_token,
                },
            )
            profile_response.raise_for_status()
            profile = profile_response.json()

            username        = profile.get("username", profile.get("name", ""))
            user_id_threads = str(profile.get("id", token_data.get("user_id", "")))

    except httpx.HTTPStatusError as exc:
        logger.error("Threads HTTP error: %s — %s", exc.response.status_code, exc.response.text)
        return _oauth_popup_response(False, "Failed to connect Threads.")
    except Exception as exc:
        logger.error("Threads token exchange failed: %s", exc)
        return _oauth_popup_response(False, "Failed to connect Threads.")

    full = await _room_note(workspace_id, "threads")
    if full:
        return _oauth_popup_response(False, f"Threads was not added. {full}")
    await save_token(
        workspace_id=workspace_id,
        platform="threads",
        access_token=access_token,
        refresh_token=None,
        expires_at=expires_at,
        platform_user_id=user_id_threads,
        username=username,
        connected_by=user_id,
        profile_url=_derive_profile_url("threads", username),
    )

    return _oauth_popup_response(True, "Threads connected successfully.")


# ── Add these two routes to oauth.py, before the /{platform} wildcards ──────


@router.get("/google/connect")
@limiter.limit("10/minute")
async def connect_google(
    request: Request,
    ctx: WorkspaceContext = Depends(require("manage_connections")),
) -> dict:
    """
    Start Google OAuth flow.
    Covers YouTube (and other Google products as scopes are added).
    """
    await _require_room_for_any(["youtube"], ctx.workspace_id)
    from app.pipelines.publish.google.oauth import build_auth_url
    state    = await _create_state(ctx.user_id, "google", ctx.workspace_id, _opener_origin(request))
    auth_url = build_auth_url(state, platform="google")

    return {
        "platform": "google",
        "covers":   ["youtube"],
        "auth_url": auth_url,
        "message":  "Redirect user to auth_url to connect Google / YouTube",
    }


@router.get("/google/callback")
@limiter.limit("10/minute")
async def google_callback(
    request: Request,
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None),
) -> HTMLResponse:
    """Handle Google OAuth callback."""
    if error:
        return _oauth_popup_response(False, f"Google OAuth denied: {error}")

    if not code or not state:
        return _oauth_popup_response(False, "Missing code or state from Google callback.")

    # State format is "token|platform" — extract token only
    state_parts = state.split("|", 1)
    state_token = state_parts[0]

    state_data = await _consume_state(state_token)
    if not state_data:
        return _oauth_popup_response(False, "Invalid or expired OAuth state. Please try connecting again.")

    user_id = state_data["user_id"]
    workspace_id = state_data.get("workspace_id", "")

    try:
        from app.pipelines.publish.google.oauth import exchange_code
        token_data = await exchange_code(code, platform="google")
    except Exception as exc:
        logger.error("Google token exchange failed for user %s: %s", user_id, exc)
        return _oauth_popup_response(False, "Failed to connect Google. Please try again.")

    connected = []

    await save_token(
        workspace_id=workspace_id,
        platform="google",
        access_token=token_data["access_token"],
        refresh_token=token_data.get("refresh_token"),
        expires_at=token_data.get("expires_at"),
        platform_user_id=token_data["platform_user_id"],
        username=token_data.get("username", ""),
        connected_by=user_id,
        scopes=token_data.get("scopes"),
        profile_url=(
            _derive_profile_url("youtube", "", token_data["youtube_channel_id"]) if token_data.get("youtube_channel_id") else GOOGLE_ACCOUNT_URL
        ),
    )
    connected.append("google")
    logger.info("Google connected for user %s — %s", user_id, token_data.get("email"))

    yt_full = await _room_note(workspace_id, "youtube") if token_data.get("youtube_channel_id") else None
    if yt_full:
        return _oauth_popup_response(False, f"Google is connected, but YouTube was not added. {yt_full}")
    if token_data.get("youtube_channel_id"):
        await save_token(
            workspace_id=workspace_id,
            platform="youtube",
            access_token=token_data["access_token"],
            refresh_token=token_data.get("refresh_token"),
            expires_at=token_data.get("expires_at"),
            platform_user_id=token_data["youtube_channel_id"],
            username=token_data.get("youtube_channel_name", token_data.get("username", "")),
            connected_by=user_id,
            profile_url=_derive_profile_url("youtube", "", token_data["youtube_channel_id"]),
        )
        connected.append("youtube")
        logger.info(
            "YouTube connected for user %s — channel: %s",
            user_id, token_data.get("youtube_channel_name"),
        )
    else:
        logger.info(
            "No YouTube channel found for user %s — "
            "user must create a YouTube channel to connect YouTube.",
            user_id,
        )

    logger.info("Google callback complete for user %s — connected: %s", user_id, connected)

    if connected:
        return _oauth_popup_response(True, f"Connected: {', '.join(connected)}")
    return _oauth_popup_response(
        False,
        "No platforms connected. YouTube: no channel found on this Google account — "
        "create one at youtube.com and reconnect.",
    )
# Platforms that do not share a public profile address when an account connects. The member can add it once, and it is kept on the
# connection so every screen that links to the account uses it. Only addresses on the platform's own site are accepted.
_MANUAL_PROFILE_HOSTS: dict[str, tuple[str, ...]] = {
    "linkedin": ("linkedin.com", "www.linkedin.com"),
}


_MANUAL_PROFILE_LABELS = {"linkedin": "LinkedIn"}


class ProfileLinkRequest(BaseModel):
    url: str


def _clean_manual_profile_url(platform: str, url: str) -> str:
    parts = urlsplit((url or "").strip())
    host = parts.netloc.lower()
    allowed = _MANUAL_PROFILE_HOSTS[platform]
    if parts.scheme not in ("http", "https") or host not in allowed or len(parts.path.strip("/")) < 3:
        raise HTTPException(status_code=400, detail=f"Enter the link to your {_MANUAL_PROFILE_LABELS[platform]} profile, for example https://www.linkedin.com/in/yourname.")
    if re.search(r"\s", url.strip()):
        raise HTTPException(status_code=400, detail="That link has spaces in it. Copy it again from your browser.")
    return urlunsplit(("https", host, parts.path.rstrip("/"), "", ""))


@router.put("/{platform}/profile-link")
@limiter.limit("20/minute")
async def set_profile_link(
    request: Request,
    platform: str,
    body: ProfileLinkRequest,
    ctx: WorkspaceContext = Depends(require("manage_connections")),
) -> dict:
    """Save the public profile address of a connected account whose platform does not share one (LinkedIn)."""
    if platform not in _MANUAL_PROFILE_HOSTS:
        raise HTTPException(status_code=400, detail="This platform's link is added automatically when you connect.")
    from app.db.mongo import workspace_connections

    url = _clean_manual_profile_url(platform, body.url)
    res = await workspace_connections.update_one(
        {"workspace_id": ctx.workspace_id, "platform": platform, "is_active": True},
        {"$set": {"profile_url": url}},
    )
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail=f"Connect your {_MANUAL_PROFILE_LABELS[platform]} account first.")
    return {"platform": platform, "profile_url": url}


# ─────────────────────────────────────────────────────────────────────────────
# GENERIC ROUTES LAST — wildcard /{platform} catches everything else
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/{platform}/connect")
@limiter.limit("10/minute")
async def connect_platform(
    request: Request,
    platform: str,
    ctx: WorkspaceContext = Depends(require("manage_connections")),
) -> dict:
    """Start OAuth flow for a platform."""
    try:
        publisher = get_publisher(platform)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Platform '{platform}' not supported.",
        )

    await _require_room_for_any([platform], ctx.workspace_id)
    state    = await _create_state(ctx.user_id, platform, ctx.workspace_id, _opener_origin(request))
    if platform == "linkedin":
        from app.pipelines.publish.destinations_support import destination_enabled

        # Company Page permissions are asked for only once Ops has opened Company Pages (LinkedIn rejects the sign-in page otherwise).
        auth_url = publisher.build_auth_url(state, include_org=await destination_enabled("linkedin", "organization"))
    else:
        auth_url = publisher.build_auth_url(state)

    return {
        "platform": platform,
        "auth_url": auth_url,
        "message":  f"Redirect user to auth_url to connect {platform}",
    }


@router.get("/{platform}/callback")
@limiter.limit("10/minute")
async def oauth_callback(
    request: Request,
    platform: str,
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None),
) -> HTMLResponse:
    """Handle OAuth callback from platform. A denial comes back with no code, so code and state are optional here and the denial is
    answered with the usual popup message instead of a raw validation error."""
    if error:
        return _oauth_popup_response(False, f"OAuth denied: {error}")
    if not code or not state:
        return _oauth_popup_response(False, "The sign-in didn't finish. Please try connecting again.")

    state_data = await _consume_state(state)
    if not state_data:
        return _oauth_popup_response(False, "Invalid or expired OAuth state. Please try connecting again.")

    if state_data["platform"] != platform:
        return _oauth_popup_response(False, "Platform mismatch in OAuth state.")

    user_id = state_data["user_id"]
    workspace_id = state_data.get("workspace_id", "")

    try:
        publisher  = get_publisher(platform)
        token_data = await publisher.exchange_token(code)
    except Exception as exc:
        logger.error("Token exchange failed for %s user %s: %s", platform, user_id, exc)
        return _oauth_popup_response(False, f"Failed to connect {platform}. Please try again.")

    full = await _room_note(workspace_id, platform)
    if full:
        return _oauth_popup_response(False, f"{platform.title()} was not added. {full}")
    await save_token(
        workspace_id=workspace_id,
        platform=platform,
        access_token=token_data["access_token"],
        refresh_token=token_data.get("refresh_token"),
        expires_at=token_data.get("expires_at"),
        platform_user_id=token_data.get("platform_user_id", ""),
        username=token_data.get("username", ""),
        connected_by=user_id,
        profile_url=_derive_profile_url(
            platform, token_data.get("username", ""), token_data.get("platform_user_id", "")
        ),
        scopes=token_data.get("scopes"),
    )

    return _oauth_popup_response(True, f"{platform} connected successfully.")


@router.delete("/{platform}/disconnect")
@limiter.limit("10/minute")
async def disconnect_platform(
    request: Request,
    platform: str,
    ctx: WorkspaceContext = Depends(require("manage_connections")),
) -> dict:
    """Remove a platform connection from the active workspace. YouTube and Google are one sign-in, so both go together and Google is
    told to stop honouring it. Posts still scheduled for the platform are counted, so the member knows they will not go out."""
    from app.db.mongo import content_pieces, workspace_connections

    group = ("youtube", "google") if platform in ("youtube", "google") else (platform,)
    existing = await workspace_connections.find_one({"workspace_id": ctx.workspace_id, "platform": {"$in": list(group)}})
    if not existing:
        raise HTTPException(status_code=404, detail=f"{platform.capitalize()} isn't connected.")

    if group != (platform,):
        await _revoke_google(ctx.workspace_id)
    elif platform == "linkedin":
        await _revoke_linkedin(ctx.workspace_id)
    for name in group:
        await delete_token(ctx.workspace_id, name)

    waiting = await content_pieces.count_documents({
        "workspace_id": ctx.workspace_id, "publish_status": "queued", "publish_target": {"$in": list(group)},
    })
    try:
        from app.shared.activity import record_system

        await record_system(
            workspace_id=ctx.workspace_id, key=f"disconnect:{ctx.workspace_id}:{platform}", actor_name=ctx.user.get("name") or "A member",
            actor_user_id=ctx.user_id, category="account_connected", title=f"{platform.capitalize()} was disconnected",
            description=(f"{waiting} scheduled post{'s' if waiting != 1 else ''} for it will not go out until it is connected again."
                         if waiting else "No posts were waiting for it."),
            status="warning" if waiting else "success", channel=platform, href="/dashboard/settings",
        )
    except Exception as exc:  # noqa: BLE001 - the disconnect itself is done
        logger.warning("Could not write the Activity Log entry for disconnecting %s: %s", platform, exc)
    return {
        "platform":    platform,
        "disconnected": True,
        "scheduled_posts_affected": waiting,
        "message":     f"{platform} disconnected."
                       + (f" {waiting} scheduled post{'s' if waiting != 1 else ''} will not go out until you connect it again." if waiting else ""),
    }


async def _provider_post(url: str, data: dict) -> None:
    """One form post to a provider, kept apart so tests can stand in for the provider."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        (await client.post(url, data=data)).raise_for_status()


async def _revoke_linkedin(workspace_id: str) -> None:
    """Best effort: ask LinkedIn to stop honouring the saved token. The connection is removed whether or not this works."""
    try:
        from app.pipelines.publish.linkedin.oauth import LINKEDIN_REVOKE_URL
        from app.pipelines.publish.token_store import get_token

        token = await get_token(workspace_id, "linkedin")
        if token and token.get("access_token"):
            await _provider_post(LINKEDIN_REVOKE_URL, {
                "client_id": settings.LINKEDIN_CLIENT_ID, "client_secret": settings.LINKEDIN_CLIENT_SECRET, "token": token["access_token"],
            })
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not revoke the LinkedIn sign-in for workspace %s: %s", workspace_id, exc)


async def _revoke_google(workspace_id: str) -> None:
    """Best effort: ask Google to stop honouring the saved sign-in. The connection is removed whether or not this works."""
    try:
        import httpx

        from app.pipelines.publish.token_store import get_token

        token = await get_token(workspace_id, "google") or await get_token(workspace_id, "youtube")
        secret = (token or {}).get("refresh_token") or (token or {}).get("access_token")
        if secret:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post("https://oauth2.googleapis.com/revoke", data={"token": secret})
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not revoke the Google sign-in for workspace %s: %s", workspace_id, exc)


@router.get("/meta/debug")
@limiter.limit("10/minute")
async def debug_meta(
    request: Request,
    ctx: WorkspaceContext = Depends(require("manage_connections")),
) -> dict:
    """
    Debug Meta token step by step. Not available in production: it takes a token in the address (which ends up in logs) and returns
    the account's Page tokens.
    Pass access_token as query param to skip steps 1-2,
    or leave empty to start fresh OAuth (requires hitting /meta/connect first).
    """
    from app.core.config import settings as _settings

    if _settings.ENVIRONMENT == "production":
        raise HTTPException(status_code=404, detail="Not found.")
    access_token = request.query_params.get("access_token")

    if not access_token:
        return {
            "error": "Pass ?access_token=YOUR_TOKEN",
            "how_to_get_token": "Copy the long-lived token from your [Step 2] server logs after hitting /meta/connect + /meta/callback",
        }

    async with httpx.AsyncClient(timeout=30.0) as client:

        # Who am I?
        me_resp = await client.get(
            f"https://graph.facebook.com/v21.0/me",
            params={"fields": "id,name,email", "access_token": access_token},
        )
        me = me_resp.json()

        # What permissions were actually granted?
        perms_resp = await client.get(
            f"https://graph.facebook.com/v21.0/me/permissions",
            params={"access_token": access_token},
        )
        perms = perms_resp.json()

        granted = [
            p["permission"]
            for p in perms.get("data", [])
            if p.get("status") == "granted"
        ]
        declined = [
            p["permission"]
            for p in perms.get("data", [])
            if p.get("status") == "declined"
        ]

        # Raw /me/accounts
        accounts_resp = await client.get(
            f"https://graph.facebook.com/v21.0/me/accounts",
            params={
                "fields": "id,name,access_token,category,instagram_business_account",
                "access_token": access_token,
            },
        )
        accounts = accounts_resp.json()

        # Token debug info
        token_debug_resp = await client.get(
            f"https://graph.facebook.com/debug_token",
            params={
                "input_token": access_token,
                "access_token": f"{settings.META_APP_ID}|{settings.META_APP_SECRET}",
            },
        )
        token_debug = token_debug_resp.json()

    return {
        "me":               me,
        "granted_scopes":   granted,
        "declined_scopes":  declined,
        "accounts_raw":     accounts,
        "accounts_status":  accounts_resp.status_code,
        "token_debug":      token_debug.get("data", {}),
        "diagnosis": {
            "pages_show_list_granted": "pages_show_list" in granted,
            "instagram_basic_granted": "instagram_basic" in granted,
            "pages_found":             len(accounts.get("data", [])),
            "token_app_id":            token_debug.get("data", {}).get("app_id"),
            "token_valid":             token_debug.get("data", {}).get("is_valid"),
            "token_user_id":           token_debug.get("data", {}).get("user_id"),
        }
    }