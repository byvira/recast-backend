"""
OAuth endpoints — connect, callback, disconnect, list accounts.
Handles all platform OAuth flows through one unified router.
"""

import logging
import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
import httpx
from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.pipelines.publish.registry import get_publisher
from app.core.config import settings
from app.pipelines.publish.token_store import (
    save_token,
    delete_token,
    get_all_tokens,
)

router = APIRouter()
logger = logging.getLogger(__name__)

_oauth_states: dict[str, dict] = {}


class BlueskyConnectRequest(BaseModel):
    handle: str
    app_password: str


def _create_state(user_id: str, platform: str, workspace_id: str) -> str:
    state = secrets.token_urlsafe(32)
    _oauth_states[state] = {
        "user_id": user_id,
        "platform": platform,
        "workspace_id": workspace_id,
    }
    return state


def _consume_state(state: str) -> dict | None:
    data = _oauth_states.pop(state, None)
    if not data:
        logger.error(
            "State NOT FOUND — state=%s available_states=%d",
            state[:20], len(_oauth_states),
        )
        logger.error(
            "Available state keys: %s",
            [k[:10] for k in _oauth_states.keys()]
        )
    return data

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
    return {
        "accounts": accounts,
        "total":    len(accounts),
    }


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
    from app.pipelines.publish.meta.oauth import build_auth_url
    state    = _create_state(ctx.user_id, "meta", ctx.workspace_id)
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
) -> dict:
    """
    Handle Meta OAuth callback.
    Saves tokens for Instagram and Facebook only.
    Threads handled separately via /api/v1/oauth/threads/connect.
    """
    # Handle Meta error response
    if error_code or error:
        raise HTTPException(
            status_code=400,
            detail=f"Meta OAuth error {error_code}: {error_message or error}",
        )

    if not code or not state:
        raise HTTPException(
            status_code=400,
            detail="Missing code or state from Meta callback.",
        )

    # State format is "token|platform" — extract token only
    state_parts = state.split("|", 1)
    state_token = state_parts[0]

    state_data = _consume_state(state_token)
    if not state_data:
        raise HTTPException(
            status_code=400,
            detail="Invalid or expired OAuth state. Please try connecting again.",
        )

    user_id = state_data["user_id"]
    workspace_id = state_data.get("workspace_id", "")

    try:
        from app.pipelines.publish.meta.oauth import exchange_code
        token_data = await exchange_code(code, platform="meta")
    except Exception as exc:
        logger.error("Meta token exchange failed for user %s: %s", user_id, exc)
        raise HTTPException(
            status_code=500,
            detail="Failed to connect Meta. Please try again.",
        )

    connected = []

    # ── Instagram — only if Business account linked to Facebook Page ──────
    if token_data.get("ig_user_id"):
        await save_token(
            workspace_id=workspace_id,
            platform="instagram",
            access_token=token_data["access_token"],
            refresh_token=None,
            expires_at=token_data.get("expires_at"),
            platform_user_id=token_data["ig_user_id"],
            username=token_data.get("username", ""),
            connected_by=user_id,
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
    if pages:
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
        )
        connected.append("facebook")
        logger.info(
            "Facebook connected for user %s — page: %s",
            user_id, first_page.get("name"),
        )
    else:
        logger.info(
            "No Facebook Pages found for user %s — "
            "user must create or manage a Facebook Page",
            user_id,
        )

   

    logger.info("Meta callback complete for user %s — connected: %s", user_id, connected)

    return {
        "connected":  len(connected) > 0,
        "platforms":  connected,
        "username":   token_data.get("username", ""),
        "pages":      [{"id": p["id"], "name": p["name"]} for p in pages],
        "instagram_connected": "instagram" in connected,
        "instagram_note": (
            ""
            if "instagram" in connected
            else
            "Instagram not connected. To connect Instagram: "
            "switch to a Business/Creator account in the Instagram app, "
            "then link it to your Facebook Page under "
            "Instagram Settings → Account → Linked Accounts → Facebook. "
            "Then reconnect Meta."
        ),
        "facebook_connected": "facebook" in connected,
        "facebook_note": (
            ""
            if "facebook" in connected
            else
            "Facebook not connected. No Facebook Pages found. "
            "Create a Facebook Page at facebook.com/pages/create "
            "then reconnect Meta."
        ),
        "threads_note": (
            "Threads uses a separate OAuth flow. "
            "Connect at GET /api/v1/oauth/threads/connect"
        ),
        "message": (
            f"Connected: {', '.join(connected)}"
            if connected
            else "No platforms connected. See notes above."
        ),
    }

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
    state    = _create_state(ctx.user_id, "threads", ctx.workspace_id)
    params   = {
        "client_id":     settings.THREADS_APP_ID,
        "redirect_uri":  settings.THREADS_REDIRECT_URI,
        "scope":         "threads_basic,threads_content_publish",
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
) -> dict:
    """Handle Threads OAuth callback."""
    if error:
        raise HTTPException(status_code=400, detail=f"Threads OAuth denied: {error}")

    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state.")

    state_data = _consume_state(state)
    if not state_data:
        raise HTTPException(status_code=400, detail="Invalid or expired state.")

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
        raise HTTPException(status_code=500, detail="Failed to connect Threads.")
    except Exception as exc:
        logger.error("Threads token exchange failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to connect Threads.")

    await save_token(
        workspace_id=workspace_id,
        platform="threads",
        access_token=access_token,
        refresh_token=None,
        expires_at=expires_at,
        platform_user_id=user_id_threads,
        username=username,
        connected_by=user_id,
    )

    return {
        "platform":  "threads",
        "connected": True,
        "username":  username,
        "message":   "Threads connected successfully.",
    }


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
    from app.pipelines.publish.google.oauth import build_auth_url
    state    = _create_state(ctx.user_id, "google", ctx.workspace_id)
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
) -> dict:
    """Handle Google OAuth callback."""
    if error:
        raise HTTPException(status_code=400, detail=f"Google OAuth denied: {error}")

    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state from Google callback.")

    # State format is "token|platform" — extract token only
    state_parts = state.split("|", 1)
    state_token = state_parts[0]

    state_data = _consume_state(state_token)
    if not state_data:
        raise HTTPException(
            status_code=400,
            detail="Invalid or expired OAuth state. Please try connecting again.",
        )

    user_id = state_data["user_id"]
    workspace_id = state_data.get("workspace_id", "")

    try:
        from app.pipelines.publish.google.oauth import exchange_code
        token_data = await exchange_code(code, platform="google")
    except Exception as exc:
        logger.error("Google token exchange failed for user %s: %s", user_id, exc)
        raise HTTPException(
            status_code=500,
            detail="Failed to connect Google. Please try again.",
        )

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
    )
    connected.append("google")
    logger.info("Google connected for user %s — %s", user_id, token_data.get("email"))

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

    return {
        "connected":         len(connected) > 0,
        "platforms":         connected,
        "username":          token_data.get("username", ""),
        "email":             token_data.get("email", ""),
        "google_connected":  "google" in connected,
        "youtube_connected": "youtube" in connected,
        "youtube_note": (
            ""
            if "youtube" in connected
            else
            "YouTube not connected. No YouTube channel found on this Google account. "
            "Create a channel at youtube.com and reconnect Google."
        ),
        "message": (
            f"Connected: {', '.join(connected)}"
            if connected
            else "No platforms connected. See notes above."
        ),
    }
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

    state    = _create_state(ctx.user_id, platform, ctx.workspace_id)
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
    code: str = Query(...),
    state: str = Query(...),
    error: str = Query(None),
) -> dict:
    """Handle OAuth callback from platform."""
    if error:
        raise HTTPException(status_code=400, detail=f"OAuth denied: {error}")

    state_data = _consume_state(state)
    if not state_data:
        raise HTTPException(
            status_code=400,
            detail="Invalid or expired OAuth state. Please try connecting again.",
        )

    if state_data["platform"] != platform:
        raise HTTPException(status_code=400, detail="Platform mismatch in OAuth state.")

    user_id = state_data["user_id"]
    workspace_id = state_data.get("workspace_id", "")

    try:
        publisher  = get_publisher(platform)
        token_data = await publisher.exchange_token(code)
    except Exception as exc:
        logger.error("Token exchange failed for %s user %s: %s", platform, user_id, exc)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to connect {platform}. Please try again.",
        )

    await save_token(
        workspace_id=workspace_id,
        platform=platform,
        access_token=token_data["access_token"],
        refresh_token=token_data.get("refresh_token"),
        expires_at=token_data.get("expires_at"),
        platform_user_id=token_data.get("platform_user_id", ""),
        username=token_data.get("username", ""),
        connected_by=user_id,
    )

    return {
        "platform":  platform,
        "connected": True,
        "username":  token_data.get("username", ""),
        "message":   f"{platform} connected successfully.",
    }


@router.delete("/{platform}/disconnect")
@limiter.limit("10/minute")
async def disconnect_platform(
    request: Request,
    platform: str,
    ctx: WorkspaceContext = Depends(require("manage_connections")),
) -> dict:
    """Remove a platform connection from the active workspace."""
    await delete_token(ctx.workspace_id, platform)
    return {
        "platform":    platform,
        "disconnected": True,
        "message":     f"{platform} disconnected.",
    }


@router.get("/meta/debug")
@limiter.limit("10/minute")
async def debug_meta(
    request: Request,
    ctx: WorkspaceContext = Depends(require("manage_connections")),
) -> dict:
    """
    Debug Meta token step by step.
    Pass access_token as query param to skip steps 1-2,
    or leave empty to start fresh OAuth (requires hitting /meta/connect first).
    """
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