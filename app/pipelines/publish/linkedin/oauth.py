"""
LinkedIn OAuth 2.0 flow.
Scopes: w_member_social (post), r_liteprofile (name), r_emailaddress (email)
"""

import logging
import httpx
from urllib.parse import urlencode
from datetime import datetime, timezone, timedelta

from app.core.config import settings

logger = logging.getLogger(__name__)

LINKEDIN_AUTH_URL    = "https://www.linkedin.com/oauth/v2/authorization"
LINKEDIN_TOKEN_URL   = "https://www.linkedin.com/oauth/v2/accessToken"
LINKEDIN_PROFILE_URL = "https://api.linkedin.com/v2/userinfo"

SCOPES = ["openid", "profile", "email", "w_member_social"]


def build_auth_url(state: str) -> str:
    """
    Build the LinkedIn OAuth authorization URL.
    Redirect user here to start the OAuth flow.
    """
    params = {
        "response_type": "code",
        "client_id":     settings.LINKEDIN_CLIENT_ID,
        "redirect_uri":  settings.LINKEDIN_REDIRECT_URI,
        "scope":         " ".join(SCOPES),
        "state":         state,
    }
    return f"{LINKEDIN_AUTH_URL}?{urlencode(params)}"


async def exchange_code(code: str) -> dict:
    """
    Exchange OAuth authorization code for access token.

    Returns:
        {
          access_token:      str
          refresh_token:     str | None
          expires_at:        datetime
          platform_user_id:  str
          username:          str
          email:             str
        }
    """
    async with httpx.AsyncClient() as client:
        # Exchange code for token
        token_response = await client.post(
            LINKEDIN_TOKEN_URL,
            data={
                "grant_type":    "authorization_code",
                "code":          code,
                "redirect_uri":  settings.LINKEDIN_REDIRECT_URI,
                "client_id":     settings.LINKEDIN_CLIENT_ID,
                "client_secret": settings.LINKEDIN_CLIENT_SECRET,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        token_response.raise_for_status()
        token_data = token_response.json()

        access_token  = token_data["access_token"]
        expires_in    = token_data.get("expires_in", 5184000)  # default 60 days
        refresh_token = token_data.get("refresh_token")
        expires_at    = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

        # Get user profile
        profile_response = await client.get(
            LINKEDIN_PROFILE_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        profile_response.raise_for_status()
        profile = profile_response.json()

        platform_user_id = profile.get("sub", "")
        username         = profile.get("name", "")
        email            = profile.get("email", "")

        logger.info(
            "LinkedIn token exchanged for user %s (%s)",
            username, platform_user_id,
        )

        return {
            "access_token":     access_token,
            "refresh_token":    refresh_token,
            "expires_at":       expires_at,
            "platform_user_id": platform_user_id,
            "username":         username,
            "email":            email,
        }


async def refresh_access_token(refresh_token: str) -> dict:
    """
    Refresh an expiring LinkedIn access token.

    Returns:
        {
          access_token: str
          expires_at:   datetime
        }
    """
    async with httpx.AsyncClient() as client:
        response = await client.post(
            LINKEDIN_TOKEN_URL,
            data={
                "grant_type":    "refresh_token",
                "refresh_token": refresh_token,
                "client_id":     settings.LINKEDIN_CLIENT_ID,
                "client_secret": settings.LINKEDIN_CLIENT_SECRET,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        data = response.json()

        expires_in = data.get("expires_in", 5184000)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

        return {
            "access_token": data["access_token"],
            "expires_at":   expires_at,
        }