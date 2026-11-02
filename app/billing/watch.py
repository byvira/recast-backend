"""The payments watch: fixed checks over the payment records, run every quarter hour, with a deeper check against the provider each night.

Each check that finds something opens a finding (`billing_watch_findings`, one row per problem, kept open while the problem lasts and
closed by itself once it is gone). New findings that need a person are sent to Ops through the same email and Slack settings as the
error and AI health alerts, at most once an hour each. No AI model is called: these are facts, checked the same way every time.

Checks:
- webhooks that failed to apply, are stuck, were refused for a wrong signature, or name a subscription Ekavo does not know;
- failed payments, and workspaces whose renewal is being retried or has stopped;
- payments a customer's bank disputed, while the dispute is open;
- a successful payment with no invoice, and an invoice a restart left without its number;
- a workspace whose plan is not the plan its subscription pays for;
- checkouts opened and never paid;
- live charging while the seller details on invoices are still placeholders;
- nightly: each live subscription compared with the provider's own record, and brought back in step where they differ.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.billing import invoices
from app.billing import prices as PR
from app.billing import subscriptions as S
from app.billing.provider import PaymentProvider, ProviderError, get_provider
from app.db.mongo import get_db, workspaces
from app.core.scheduler_lock import distributed_job_lock

logger = logging.getLogger(__name__)

STUCK_AFTER = timedelta(minutes=10)
WINDOW = timedelta(hours=24)
ALERT_COOLDOWN = timedelta(hours=1)
SEVERITIES = ("info", "warning", "critical")


@dataclass
class Finding:
    key: str
    kind: str
    severity: str
    title: str
    detail: str
    workspace_id: Optional[str] = None


def findings_col():
    return get_db()["billing_watch_findings"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _webhook_findings(now: datetime) -> list[Finding]:
    out: list[Finding] = []
    failed = await S.events_col().count_documents({"processed_at": None, "error": {"$ne": None}})
    if failed:
        out.append(Finding("webhooks:failed", "webhook_failed", "critical", f"{failed} payment updates could not be applied",
                           "Razorpay told Ekavo about a payment or subscription change and applying it failed. They are retried; check the error on each."))
    stuck = await S.events_col().count_documents({"processed_at": None, "received_at": {"$lt": now - STUCK_AFTER}})
    if stuck:
        out.append(Finding("webhooks:stuck", "webhook_stuck", "warning", f"{stuck} payment updates waiting over 10 minutes",
                           "Stored but not applied yet. The minute sweep should pick them up; if this stays, the background jobs are not running."))
    unmatched = await S.events_col().count_documents({"outcome": "unmatched", "received_at": {"$gte": now - WINDOW}})
    if unmatched:
        out.append(Finding("webhooks:unmatched", "webhook_unmatched", "warning", f"{unmatched} payment updates for a subscription Ekavo does not know",
                           "Usually a subscription made by hand in the Razorpay dashboard, or test and live keys mixed up."))
    rejected = await S.rejects_col().count_documents({"at": {"$gte": now - timedelta(hours=1)}})
    if rejected:
        out.append(Finding("webhooks:rejected", "signature_rejected", "critical", f"{rejected} payment messages refused in the last hour",
                           "Their signature did not match. Either the webhook secret in Render differs from the one in Razorpay, or someone is sending fake payment messages."))
    return out


async def _payment_findings(now: datetime) -> list[Finding]:
    out: list[Finding] = []
    async for p in S.payments_col().find({"status": "failed", "at": {"$gte": now - WINDOW}}, {"_id": 0}).limit(50):
        out.append(Finding(f"payment:failed:{p['id']}", "payment_failed", "info", "A payment failed",
                           f"{p.get('method') or 'Payment'}: {p.get('error') or 'no reason given'}. Razorpay retries renewals by itself.",
                           p.get("workspace_id")))
    async for sub in S.subs_col().find({"status": {"$in": ["pending", "halted"]}}, {"_id": 0}).limit(200):
        halted = sub["status"] == "halted"
        out.append(Finding(f"sub:{sub['status']}:{sub['id']}", "renewal_failing", "critical" if halted else "warning",
                           "A renewal stopped after every retry failed" if halted else "A renewal is being retried",
                           f"{sub.get('plan', '').title()} plan. " + ("The member has to pay or change the payment method." if halted else "Razorpay is trying again."),
                           sub.get("workspace_id")))
    async for p in S.payments_col().find({"status": "captured", "event": "subscription.charged", "at": {"$gte": now - WINDOW}}, {"_id": 0, "id": 1, "workspace_id": 1}).limit(500):
        if not await get_db()["billing_invoices"].find_one({"payment_id": p["id"]}, {"_id": 1}):
            out.append(Finding(f"invoice:missing:{p['id']}", "invoice_missing", "warning", "A payment has no invoice",
                               "The payment went through but no invoice was written for it.", p.get("workspace_id")))
    async for d in S.disputes_col().find({"status": {"$in": list(S.OPEN_DISPUTE_STATES)}}, {"_id": 0}).limit(100):
        by = S._aware(d.get("respond_by"))
        when = f" Answer in Razorpay by {by.day} {by.strftime('%b %Y')}, or it is lost." if by else " Answer it in Razorpay."
        out.append(Finding(f"dispute:{d['id']}", "payment_disputed", "critical", "A customer's bank disputed a payment",
                           f"{PR.format_amount(int(d.get('amount') or 0), d.get('currency') or 'INR')}, reason: {d.get('reason') or d.get('reason_code') or 'not given'}.{when}",
                           d.get("workspace_id")))
    unnumbered = await get_db()["billing_invoices"].count_documents({"number": {"$regex": "^pending-"}, "issued_at": {"$lt": now - STUCK_AFTER}})
    if unnumbered:
        out.append(Finding("invoice:unnumbered", "invoice_unnumbered", "critical", f"{unnumbered} receipts have no number",
                           "A restart came between saving a receipt and giving it its number. Give each the next number by hand, in order."))
    stale = await S.subs_col().count_documents({"status": "created", "created_at": {"$lt": now - timedelta(hours=S.ABANDON_AFTER_HOURS)}})
    if stale:
        out.append(Finding("checkout:abandoned", "checkout_abandoned", "info", f"{stale} checkouts opened and never paid",
                           "People started paying and stopped. Nothing to fix; worth watching if the number grows."))
    return out


async def _plan_findings() -> list[Finding]:
    out: list[Finding] = []
    # a workspace may hold two subscriptions in force for a while (a plan and the bigger one replacing it); it is on the newest,
    # the same one `apply_for_workspace` picks
    newest: dict[str, dict[str, Any]] = {}
    async for sub in S.subs_col().find({"status": {"$in": [*S.PAID_STATES, "authenticated", "paused"]}}, {"_id": 0}).sort("created_at", -1).limit(2000):
        if S.is_paid(sub) and sub["workspace_id"] not in newest:
            newest[sub["workspace_id"]] = sub
    for sub in newest.values():
        ws = await workspaces.find_one({"id": sub["workspace_id"]}, {"plan": 1, "plan_seats": 1})
        if ws and (ws.get("plan") != sub.get("plan") or int(ws.get("plan_seats") or 0) != int(sub.get("seats") or 0)):
            out.append(Finding(f"plan:mismatch:{sub['workspace_id']}", "plan_mismatch", "critical", "A workspace is not on the plan it pays for",
                               f"Pays for {sub.get('plan')} with {sub.get('seats')} seats; the workspace says {ws.get('plan')} with {ws.get('plan_seats')}.",
                               sub["workspace_id"]))
    return out


async def _setup_findings(provider: PaymentProvider) -> list[Finding]:
    cfg = await invoices.load_settings()
    if provider.mode == "live" and cfg.get("checkout_open") and not invoices.seller_ready(cfg):
        return [Finding("setup:seller", "seller_placeholder", "critical", "Live payments are open with placeholder invoice details",
                        "Enter the business name and address in Ops billing settings.")]
    return []


async def run_checks(provider: Optional[PaymentProvider] = None) -> list[Finding]:
    provider = provider or get_provider()
    now = _now()
    found = [
        *await _webhook_findings(now), *await _payment_findings(now), *await _plan_findings(), *await _setup_findings(provider),
    ]
    await _record(found, now, scope="quick")
    return found


async def reconcile(provider: Optional[PaymentProvider] = None) -> list[Finding]:
    """Nightly: compare each live subscription with the provider and bring the mirror back in step. A difference is a finding, because
    it means a webhook was missed."""
    provider = provider or get_provider()
    if not provider.configured():
        return []
    found: list[Finding] = []
    # checkouts never confirmed are asked about too: one paid with its message lost is brought in step, and one a day old that was
    # never paid is let go
    unpaid_since = _now() - timedelta(hours=S.ABANDON_AFTER_HOURS)
    async for sub in S.subs_col().find({"status": "created", "mode": provider.mode}, {"_id": 0}).limit(500):
        try:
            entity = await provider.fetch_subscription(sub["provider_subscription_id"])
            if str(entity.get("status") or "created") != "created":
                found.append(Finding(f"reconcile:drift:{sub['id']}", "reconcile_drift", "warning", "A checkout was paid but Ekavo had not heard",
                                     f"Razorpay has {entity.get('status')}. A payment message was probably missed; it is now applied.", sub.get("workspace_id")))
                await S.sync(entity, seen_at=None, source="reconcile")
            elif (S._aware(sub.get("created_at")) or _now()) < unpaid_since:
                await S._let_go(provider, sub)
        except Exception as exc:  # noqa: BLE001 - one checkout that cannot be read never stops the others
            logger.warning("Could not check checkout %s: %s", sub.get("id"), exc)
    async for sub in S.subs_col().find({"status": {"$in": [s for s in S.LIVE_STATES if s != "created"]}, "mode": provider.mode}, {"_id": 0}).limit(2000):
        try:
            entity = await provider.fetch_subscription(sub["provider_subscription_id"])
        except Exception as exc:  # noqa: BLE001 - one subscription that cannot be read never stops the check of the others
            message = exc.message if isinstance(exc, ProviderError) else "Unexpected answer while reading it."
            found.append(Finding(f"reconcile:unreadable:{sub['id']}", "reconcile_failed", "warning", "Could not check a subscription with Razorpay",
                                 message, sub.get("workspace_id")))
            continue
        theirs = str(entity.get("status") or "")
        if theirs and theirs != sub.get("status"):
            found.append(Finding(f"reconcile:drift:{sub['id']}", "reconcile_drift", "warning", "A subscription differed from Razorpay and was corrected",
                                 f"Ekavo had {sub.get('status')}, Razorpay has {theirs}. A payment message was probably missed.", sub.get("workspace_id")))
            await S.sync(entity, seen_at=None, source="reconcile")
    await _record(found, _now(), scope="nightly")
    return found


async def _record(found: list[Finding], now: datetime, *, scope: str) -> None:
    """Open or refresh a row per finding; close the rows of this scope that were not found again; alert on new ones."""
    keys = [f.key for f in found]
    new: list[Finding] = []
    for f in found:
        res = await findings_col().update_one(
            {"key": f.key},
            {"$set": {"kind": f.kind, "severity": f.severity, "title": f.title, "detail": f.detail, "workspace_id": f.workspace_id,
                      "last_seen": now, "open": True, "resolved_at": None, "scope": scope},
             "$setOnInsert": {"key": f.key, "first_seen": now}},
            upsert=True,
        )
        if res.upserted_id is not None:
            new.append(f)
    await findings_col().update_many(
        {"scope": scope, "open": True, "key": {"$nin": keys}}, {"$set": {"open": False, "resolved_at": now}},
    )
    await _alert([f for f in new if f.severity != "info"])


async def _alert(new: list[Finding]) -> None:
    if not new:
        return
    from app.core.config import settings
    from app.shared.llm_health import alerts
    from app.shared.llm_health.service import get_alert_rules

    link = f"{(settings.FRONTEND_URL or '').rstrip('/')}/ops/billing?view=watch"
    candidates = [
        alerts.Alert(key=f"billing:{f.key}", kind="payments", severity="critical" if f.severity == "critical" else "high",
                     subject=f"Payments: {f.title}", body=f"{f.detail}\n{link}")
        for f in new
    ]
    sent = await alerts.recently_sent([a.key for a in candidates], ALERT_COOLDOWN)
    rules = await get_alert_rules()
    for alert in candidates:
        if alert.key not in sent:
            try:
                await alerts.deliver(alert, rules)
            except Exception as exc:  # noqa: BLE001 - a failed alert never stops the watch
                logger.warning("Payments alert not sent: %s", exc)


async def open_findings(limit: int = 200) -> list[dict[str, Any]]:
    order = {s: i for i, s in enumerate(reversed(SEVERITIES))}
    rows = [r async for r in findings_col().find({"open": True}, {"_id": 0}).sort("last_seen", -1).limit(limit)]
    return sorted(rows, key=lambda r: order.get(r.get("severity"), 9))


@distributed_job_lock("billing_watch", ttl_seconds=600)
async def watch_tick() -> None:
    await run_checks()


@distributed_job_lock("billing_reconcile", ttl_seconds=3600)
async def reconcile_tick() -> None:
    await reconcile()


@distributed_job_lock("billing_webhooks", ttl_seconds=120)
async def webhooks_tick() -> None:
    await S.process_pending()


async def create_indexes() -> None:
    await findings_col().create_index("key", unique=True)
    await findings_col().create_index([("open", 1), ("last_seen", -1)])
    await findings_col().create_index("resolved_at", expireAfterSeconds=180 * 24 * 3600)
