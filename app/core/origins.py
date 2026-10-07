"""Which website addresses may call the API from a browser (CORS)."""
from __future__ import annotations

from typing import Iterable

#: The app's own addresses besides FRONTEND_URL, so the site works from each of them without a setting having to be remembered.
APP_ORIGINS = ("https://recast.byvirastudio.com",)


def clean(origin: str) -> str:
    """An origin without a trailing slash, which browsers never send."""
    return (origin or "").strip().rstrip("/")


def build_origins(
    *, production: bool, production_domain: str, frontend_url: str, allowed: Iterable[str], app_origins: Iterable[str] = APP_ORIGINS,
) -> list[str]:
    """The origins the API answers. Outside production only the allowed list is used. In production the primary domain (or the frontend
    address when none is set), the frontend address, the app's own addresses and the allowed list are all included, once each."""
    ordered = [clean(o) for o in allowed]
    if production:
        ordered = [clean(production_domain or frontend_url), clean(frontend_url), *[clean(o) for o in app_origins], *ordered]
    seen: set[str] = set()
    result: list[str] = []
    for origin in ordered:
        if origin and origin not in seen:
            seen.add(origin)
            result.append(origin)
    return result
