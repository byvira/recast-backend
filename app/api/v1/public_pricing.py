"""The website's pricing page: every plan, its limits and its prices as set in Ops, and whether sign-up and checkout are open.

No sign-in. Only what the pricing page shows: no provider ids, no founding counts beyond whether places are left.
"""
from __future__ import annotations

from fastapi import APIRouter, Request, Response

from app.core.middleware import limiter

router = APIRouter()

# the limits the website lists, in its order
SHOWN = ("seats", "owned_workspaces", "social_accounts", "brand_voices", "credits_month", "render_minutes_month", "pictures_day",
         "storage_mb", "scheduled_ahead", "campaigns_active", "version_history_days", "analytics_history_days", "support_priority")


@router.get("")
@limiter.limit("120/minute")
async def pricing(request: Request, response: Response) -> dict:
    from app.billing import invoices
    from app.billing import plans as P
    from app.billing import prices as PR
    from app.billing import subscriptions as S
    from app.billing.provider import get_provider
    from app.shared import entitlements
    from app.shared.invites import signup_mode

    await PR.load_book()
    table = await entitlements.load_plans()
    left = await S.founding_left()
    cfg = await invoices.load_settings()
    response.headers["Cache-Control"] = "public, max-age=60"
    return {
        "plans": {
            pid: {
                "name": spec.get("name"), "min_seats": spec.get("min_seats"), "max_seats": spec.get("max_seats"),
                "per_seat": bool(spec.get("per_seat")),
                "limits": {k: v for k, v in P.limits_for(spec, spec.get("min_seats")).items() if k in SHOWN},
                "limits_max": {k: v for k, v in P.limits_for(spec, spec.get("max_seats")).items() if k in SHOWN},
            }
            for pid, spec in table.items()
        },
        "labels": {k: P.LIMITS[k]["label"] for k in SHOWN},
        "prices": {cur: PR.table(cur, founding=left > 0) for cur in PR.CURRENCIES},
        "list_prices": {cur: PR.table(cur) for cur in PR.CURRENCIES},
        "founding_open": left > 0,
        "volume_from_seats": PR.volume_from_seats(),
        "yearly_months": PR.yearly_months(),
        "checkout_open": bool(cfg.get("checkout_open")) and get_provider().configured(),
        "checkout_currencies": list(PR.CHECKOUT_CURRENCIES),
        "signup_mode": await signup_mode(),
    }
