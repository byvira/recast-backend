"""
Meta OAuth — shared for Instagram and Facebook.
Threads uses separate OAuth via threads.net.

App type: Business (Facebook Login for Business)
"""

import logging
import httpx
from urllib.parse import urlencode
from datetime import datetime, timezone, timedelta
from typing import Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

_V = "v25.0"
META_AUTH_URL       = f"https://www.facebook.com/{_V}/dialog/oauth"
META_TOKEN_URL      = f"https://graph.facebook.com/{_V}/oauth/access_token"
META_LONG_TOKEN_URL = f"https://graph.facebook.com/{_V}/oauth/access_token"
GRAPH_BASE          = f"https://graph.facebook.com/{_V}"

# NOTE: scopes are NOT passed in the URL for Business Login.
# They are controlled entirely by the Business Login Configuration
# (META_CONFIG_ID). This list is kept only for reference/logging.
ALL_META_SCOPES = [
    "pages_show_list",
    "pages_read_engagement",
    "pages_manage_posts",
    "instagram_basic",
    "instagram_content_publish",
]


def build_auth_url(state: str, platform: str = "meta") -> str:
    """
    Build the Meta OAuth URL for Facebook Login for Business.

    For Business apps, the consent screen is driven entirely by config_id.
    - NO scope parameter — scopes come from the Business Login Configuration.
    - NO enable_fb_login / enable_profile_selector — Consumer Login params,
      ignored by Business Login.
    - config_id is REQUIRED — it tells Meta which permissions and asset
      types (Pages, Instagram accounts) to show in the consent screen.

    Set META_CONFIG_ID in your .env — get the value from:
    developers.facebook.com → your app → Facebook Login for Business
    → Configurations → your config → copy the Configuration ID.
    """
    params = {
        "client_id":     settings.META_APP_ID,
        "redirect_uri":  settings.META_REDIRECT_URI,
        "response_type": "code",
        "state":         f"{state}|{platform}",
        "config_id":     settings.META_CONFIG_ID,
    }
    url = f"{META_AUTH_URL}?{urlencode(params)}"
    logger.info(
        "Meta Business auth URL built — app_id=%s config_id=%s redirect=%s",
        settings.META_APP_ID,
        settings.META_CONFIG_ID,
        settings.META_REDIRECT_URI,
    )
    return url


async def exchange_code(code: str, platform: str) -> dict:
    """
    Exchange an authorization code for tokens, then fetch Pages and
    Instagram Business account details.

    Raises:
        ValueError: user-actionable failures (no pages, API errors).
        httpx.HTTPStatusError: unexpected HTTP failures.
    """
    logger.info("=" * 60)
    logger.info("META TOKEN EXCHANGE START — platform=%s", platform)
    logger.info("=" * 60)

    async with httpx.AsyncClient(timeout=30.0) as client:

        # ── Step 1 — authorization code → short-lived token ──────────────
        logger.info("[Step 1] Exchanging code for short-lived token")
        token_resp = await client.get(
            META_TOKEN_URL,
            params={
                "client_id":     settings.META_APP_ID,
                "client_secret": settings.META_APP_SECRET,
                "redirect_uri":  settings.META_REDIRECT_URI,
                "code":          code,
            },
        )
        logger.info(
            "[Step 1] status=%d body=%s",
            token_resp.status_code,
            token_resp.text[:300],
        )
        token_resp.raise_for_status()
        step1 = token_resp.json()
        if "error" in step1:
            raise ValueError(
                f"Step 1 failed: {step1['error'].get('message', 'unknown error')}"
            )
        short_token = step1["access_token"]
        logger.info("[Step 1] ✅ short-lived token obtained")

        # ── Step 2 — short-lived → long-lived token (60 days) ────────────
        logger.info("[Step 2] Upgrading to long-lived token")
        long_resp = await client.get(
            META_LONG_TOKEN_URL,
            params={
                "grant_type":        "fb_exchange_token",
                "client_id":         settings.META_APP_ID,
                "client_secret":     settings.META_APP_SECRET,
                "fb_exchange_token": short_token,
            },
        )
        logger.info(
            "[Step 2] status=%d body=%s",
            long_resp.status_code,
            long_resp.text[:300],
        )
        long_resp.raise_for_status()
        long_data = long_resp.json()
        if "error" in long_data:
            raise ValueError(
                f"Step 2 failed: {long_data['error'].get('message', 'unknown error')}"
            )
        access_token = long_data["access_token"]
        expires_in   = long_data.get("expires_in", 5_184_000)
        expires_at   = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        logger.info(
            "[Step 2] ✅ long-lived token — expires in %d days",
            expires_in // 86400,
        )

        # ── Step 3 — fetch profile ────────────────────────────────────────
        logger.info("[Step 3] Getting profile")
        profile_resp = await client.get(
            f"{GRAPH_BASE}/me",
            params={"fields": "id,name", "access_token": access_token},
        )
        logger.info(
            "[Step 3] status=%d body=%s",
            profile_resp.status_code,
            profile_resp.text[:300],
        )
        profile_resp.raise_for_status()
        profile = profile_resp.json()
        if "error" in profile:
            raise ValueError(
                f"Step 3 failed: {profile['error'].get('message', 'unknown error')}"
            )
        fb_user_id = profile.get("id", "")
        user_name  = profile.get("name", "")
        logger.info("[Step 3] ✅ id=%s name=%s", fb_user_id, user_name)

        result: dict = {
            "access_token":     access_token,
            "refresh_token":    None,
            "expires_at":       expires_at,
            "platform_user_id": fb_user_id,
            "username":         user_name,
            "email":            "",
            "pages":            [],
            "ig_user_id":       None,
        }

        # ── Step 4 — fetch authorized Pages ──────────────────────────────
        logger.info("[Step 4] Getting authorized Pages via /me/accounts")
        pages_resp = await client.get(
            f"{GRAPH_BASE}/me/accounts",
            params={
                "fields":       "id,name,access_token,category,instagram_business_account",
                "access_token": access_token,
            },
        )
        logger.info(
            "[Step 4] status=%d body=%s",
            pages_resp.status_code,
            pages_resp.text[:800],
        )

        pages_data = pages_resp.json()

        if "error" in pages_data:
            err = pages_data["error"]
            logger.error(
                "[Step 4] Graph API error — code=%s subcode=%s message=%s",
                err.get("code"),
                err.get("error_subcode"),
                err.get("message"),
            )
            raise ValueError(
                f"Meta API error fetching Pages: {err.get('message', 'unknown error')} "
                f"(code {err.get('code')})"
            )

        pages: list[dict] = pages_data.get("data", [])
        result["pages"] = pages
        logger.info("[Step 4] pages found: %d", len(pages))

        if pages:
            for p in pages:
                ig = p.get("instagram_business_account") or {}
                logger.info(
                    "[Step 4]   page=%s  id=%s  instagram_id=%s",
                    p.get("name"),
                    p.get("id"),
                    ig.get("id", "NONE"),
                )
        else:
            logger.error(
                "[Step 4] ❌ NO PAGES FOUND\n"
                "Business Login checklist:\n"
                "  1. Is META_CONFIG_ID set correctly in .env?\n"
                "  2. Does the config include Pages as an asset type?\n"
                "  3. Did the user select a Page in the consent screen?\n"
                "  4. Does the user actually manage a Facebook Page?"
            )
            raise ValueError(
                "No Facebook Pages were authorized. "
                "In the connect screen, make sure you select a Facebook Page "
                "when prompted. If you don't have a Page, create one at "
                "facebook.com/pages/create and reconnect."
            )

        # ── Step 5 — extract Instagram Business account ───────────────────
        logger.info(
            "[Step 5] Looking for Instagram Business account across %d page(s)",
            len(pages),
        )
        ig_id: Optional[str] = None

        for page in pages:
            ig_account   = page.get("instagram_business_account")
            ig_candidate = ig_account.get("id") if isinstance(ig_account, dict) else None
            if ig_candidate:
                ig_id = ig_candidate
                result["ig_user_id"] = ig_id
                logger.info(
                    "[Step 5] ✅ Instagram found — ig_id=%s on page=%s (%s)",
                    ig_id,
                    page.get("name"),
                    page.get("id"),
                )
                break

        if not ig_id:
            logger.warning(
                "[Step 5] ⚠️  No Instagram Business account found on any Page.\n"
                "Normal if the user only wants Facebook.\n"
                "To also connect Instagram:\n"
                "  1. Switch Instagram to Business/Creator\n"
                "  2. Link it to the Facebook Page\n"
                "  3. Reconnect Meta"
            )

        logger.info("=" * 60)
        logger.info(
            "META EXCHANGE COMPLETE — user=%s  pages=%d  instagram=%s",
            user_name,
            len(pages),
            ig_id or "NOT FOUND",
        )
        logger.info("=" * 60)

        return result


async def refresh_meta_token(access_token: str) -> dict:
    """
    Extend a long-lived token for another 60 days.
    Should be called ~10 days before expiry.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            META_LONG_TOKEN_URL,
            params={
                "grant_type":        "fb_exchange_token",
                "client_id":         settings.META_APP_ID,
                "client_secret":     settings.META_APP_SECRET,
                "fb_exchange_token": access_token,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise ValueError(
                f"Token refresh failed: {data['error'].get('message', 'unknown error')}"
            )
        expires_in = data.get("expires_in", 5_184_000)
        logger.info(
            "Meta token refreshed — expires in %d days", expires_in // 86400
        )
        return {
            "access_token": data["access_token"],
            "expires_at":   datetime.now(timezone.utc) + timedelta(seconds=expires_in),
        }