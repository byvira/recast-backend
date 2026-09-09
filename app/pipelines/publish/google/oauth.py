"""
Google OAuth — shared for YouTube and other Google products.
Uses OAuth 2.0 authorization code flow with offline access for refresh tokens.
"""

import logging
import httpx
from urllib.parse import urlencode
from datetime import datetime, timezone, timedelta

from app.core.config import settings

logger = logging.getLogger(__name__)

GOOGLE_AUTH_URL  = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USER_URL  = "https://www.googleapis.com/oauth2/v2/userinfo"

GOOGLE_SCOPES = [
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]


def build_auth_url(state: str, platform: str = "google") -> str:
    """
    Build the Google OAuth URL.
    access_type=offline ensures we get a refresh token.
    prompt=consent forces the consent screen every time so refresh token is always returned.
    """
    params = {
        "client_id":     settings.GOOGLE_CLIENT_ID,
        "redirect_uri":  settings.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope":         " ".join(GOOGLE_SCOPES),
        "access_type":   "offline",
        "prompt":        "consent",
        "state":         f"{state}|{platform}",
    }
    url = f"{GOOGLE_AUTH_URL}?{urlencode(params)}"
    logger.info(
        "Google auth URL built — client_id=%s redirect=%s",
        settings.GOOGLE_CLIENT_ID,
        settings.GOOGLE_REDIRECT_URI,
    )
    return url


async def exchange_code(code: str, platform: str = "google") -> dict:
    """
    Exchange an authorization code for tokens, then fetch user profile.

    Unlike Meta, Google returns a refresh_token directly in the token response
    (when access_type=offline and prompt=consent are set).

    Raises:
        ValueError: user-actionable failures.
        httpx.HTTPStatusError: unexpected HTTP failures.
    """
    logger.info("=" * 60)
    logger.info("GOOGLE TOKEN EXCHANGE START — platform=%s", platform)
    logger.info("=" * 60)

    async with httpx.AsyncClient(timeout=30.0) as client:

        # ── Step 1 — authorization code → tokens ─────────────────────────
        logger.info("[Step 1] Exchanging code for tokens")
        token_resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id":     settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "redirect_uri":  settings.GOOGLE_REDIRECT_URI,
                "code":          code,
                "grant_type":    "authorization_code",
            },
        )
        logger.info(
            "[Step 1] status=%d body=%s",
            token_resp.status_code,
            token_resp.text[:300],
        )
        token_resp.raise_for_status()
        token_data = token_resp.json()

        if "error" in token_data:
            raise ValueError(
                f"Step 1 failed: {token_data.get('error_description', token_data['error'])}"
            )

        access_token  = token_data["access_token"]
        refresh_token = token_data.get("refresh_token")
        expires_in    = token_data.get("expires_in", 3600)
        expires_at    = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

        logger.info(
            "[Step 1] ✅ tokens obtained — expires_in=%ds has_refresh=%s",
            expires_in,
            bool(refresh_token),
        )

        if not refresh_token:
            logger.warning(
                "[Step 1] ⚠️  No refresh_token returned. "
                "User may have already authorized this app — "
                "revoke access at myaccount.google.com/permissions and reconnect."
            )

        # ── Step 2 — fetch user profile ───────────────────────────────────
        logger.info("[Step 2] Getting user profile")
        profile_resp = await client.get(
            GOOGLE_USER_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        logger.info(
            "[Step 2] status=%d body=%s",
            profile_resp.status_code,
            profile_resp.text[:300],
        )
        profile_resp.raise_for_status()
        profile = profile_resp.json()

        if "error" in profile:
            raise ValueError(
                f"Step 2 failed: {profile['error'].get('message', 'unknown error')}"
            )

        google_user_id = profile.get("id", "")
        username       = profile.get("name", "")
        email          = profile.get("email", "")
        logger.info("[Step 2] ✅ id=%s name=%s email=%s", google_user_id, username, email)

        # ── Step 3 — fetch YouTube channel ───────────────────────────────
        logger.info("[Step 3] Getting YouTube channel")
        yt_resp = await client.get(
            "https://www.googleapis.com/youtube/v3/channels",
            params={"part": "id,snippet", "mine": "true"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        logger.info(
            "[Step 3] status=%d body=%s",
            yt_resp.status_code,
            yt_resp.text[:300],
        )

        youtube_channel_id = None
        youtube_channel_name = None

        if yt_resp.status_code == 200:
            yt_data  = yt_resp.json()
            channels = yt_data.get("items", [])
            if channels:
                youtube_channel_id   = channels[0].get("id")
                youtube_channel_name = channels[0].get("snippet", {}).get("title")
                logger.info(
                    "[Step 3] ✅ YouTube channel — id=%s name=%s",
                    youtube_channel_id,
                    youtube_channel_name,
                )
            else:
                logger.warning("[Step 3] ⚠️  No YouTube channel found for this account.")
        else:
            logger.warning(
                "[Step 3] ⚠️  YouTube API returned %d — user may not have a channel.",
                yt_resp.status_code,
            )

        logger.info("=" * 60)
        logger.info(
            "GOOGLE EXCHANGE COMPLETE — user=%s  email=%s  youtube=%s",
            username,
            email,
            youtube_channel_id or "NOT FOUND",
        )
        logger.info("=" * 60)

        return {
            "access_token":        access_token,
            "refresh_token":       refresh_token,
            "expires_at":          expires_at,
            "platform_user_id":    google_user_id,
            "username":            username,
            "email":               email,
            "youtube_channel_id":  youtube_channel_id,
            "youtube_channel_name": youtube_channel_name,
        }


async def refresh_google_token(refresh_token: str) -> dict:
    """
    Use the refresh token to get a new access token.
    Google access tokens expire in 1 hour — this should be called before expiry.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id":     settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "refresh_token": refresh_token,
                "grant_type":    "refresh_token",
            },
        )
        resp.raise_for_status()
        data = resp.json()

        if "error" in data:
            raise ValueError(
                f"Token refresh failed: {data.get('error_description', data['error'])}"
            )

        expires_in = data.get("expires_in", 3600)
        logger.info("Google token refreshed — expires in %ds", expires_in)

        return {
            "access_token": data["access_token"],
            "expires_at":   datetime.now(timezone.utc) + timedelta(seconds=expires_in),
        }