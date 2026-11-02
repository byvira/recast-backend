"""Billing settings and invoices.

Settings (one document, edited in Ops): the seller details printed on invoices, whether the business is registered for GST, and whether
checkout is open. The seller details start as placeholders; real charging stays locked until they are replaced (`seller_ready`).

Invoices are numbered `EKV-<financial year>-0001`, counting up within each Indian financial year (April to March). One invoice per
successful payment, written once: a webhook delivered twice never makes a second invoice. While not registered for GST, an invoice
carries no tax line and says so; once registered, the same setting switches it to a GST invoice with no code change.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.billing import prices as PR
from app.db.mongo import get_db

logger = logging.getLogger(__name__)

PLACEHOLDER_NAME = "Your business name"
PLACEHOLDER_ADDRESS = "Your business address"
NOT_REGISTERED_NOTE = "Supplier not registered under GST"

DEFAULT_SETTINGS: dict[str, Any] = {
    "seller": {"name": PLACEHOLDER_NAME, "address": PLACEHOLDER_ADDRESS, "state": "", "email": "hello@mail.byvirastudio.com", "gstin": ""},
    "gst_registered": False,
    "invoice_prefix": "EKV",
    "checkout_open": False,
}


def _settings_col():
    return get_db()["billing_settings"]


def _invoices_col():
    return get_db()["billing_invoices"]


def _counters_col():
    return get_db()["billing_counters"]


async def load_settings() -> dict[str, Any]:
    saved = await _settings_col().find_one({"_id": "billing"}, {"_id": 0}) or {}
    out = {**DEFAULT_SETTINGS, **{k: v for k, v in saved.items() if k in DEFAULT_SETTINGS}}
    out["seller"] = {**DEFAULT_SETTINGS["seller"], **(saved.get("seller") or {})}
    return out


async def save_settings(changes: dict[str, Any]) -> dict[str, Any]:
    current = await load_settings()
    merged = {**current, **{k: v for k, v in changes.items() if k in DEFAULT_SETTINGS and k != "seller"}}
    if "seller" in changes:
        merged["seller"] = {**current["seller"], **{k: v for k, v in (changes["seller"] or {}).items() if k in DEFAULT_SETTINGS["seller"]}}
    await _settings_col().update_one({"_id": "billing"}, {"$set": {**merged, "updated_at": datetime.now(timezone.utc)}}, upsert=True)
    return merged


def seller_ready(settings_doc: dict[str, Any]) -> bool:
    """Whether the seller details are real: a name and an address other than the placeholders."""
    seller = settings_doc.get("seller") or {}
    name, address = str(seller.get("name") or "").strip(), str(seller.get("address") or "").strip()
    return bool(name and address and name != PLACEHOLDER_NAME and address != PLACEHOLDER_ADDRESS)


def financial_year(when: datetime) -> str:
    """The Indian financial year a date falls in, as 2026-27."""
    start = when.year if when.month >= 4 else when.year - 1
    return f"{start}-{str(start + 1)[-2:]}"


async def next_number(when: datetime, prefix: str = "EKV") -> str:
    fy = financial_year(when)
    doc = await _counters_col().find_one_and_update(
        {"_id": f"invoice:{fy}"}, {"$inc": {"seq": 1}}, upsert=True, return_document=ReturnDocument.AFTER,
    )
    return f"{prefix}-{fy}-{int(doc['seq']):04d}"


def _line_label(sub: dict[str, Any]) -> str:
    plan = str(sub.get("plan") or "").title()
    period = "yearly" if sub.get("period") == "yearly" else "monthly"
    founding = ", founding price" if sub.get("founding") else ""
    return f"Ekavo {plan}, {period}{founding}"


async def issue_for_payment(sub: dict[str, Any], payment: dict[str, Any], *, label: Optional[str] = None) -> Optional[dict[str, Any]]:
    """The invoice for one successful payment: a plan's (`sub` is its subscription), or a credit pack's (`label` names it). Returns
    the existing one when it was already issued."""
    payment_id = str(payment.get("id") or "")
    if not payment_id:
        return None
    existing = await _invoices_col().find_one({"payment_id": payment_id}, {"_id": 0})
    if existing:
        return existing
    cfg = await load_settings()
    now = datetime.now(timezone.utc)
    amount = int(payment.get("amount") or sub.get("amount") or 0)
    currency = str(payment.get("currency") or sub.get("currency") or "INR")
    quantity = 1 if label else PR.quantity_for(str(sub.get("plan") or ""), int(sub.get("seats") or 1))
    # the payment's row is taken first under a holding name, and only then the next number, so two workers issuing the same receipt
    # never use up a number between them and the series has no gaps
    doc = {
        "id": uuid4().hex,
        "number": f"pending-{payment_id}",
        "workspace_id": sub.get("workspace_id"),
        "subscription_id": sub.get("id"),
        "payment_id": payment_id,
        "mode": sub.get("mode"),
        "seller": dict(cfg["seller"]),
        "customer": dict(sub.get("customer") or {}),
        "lines": [{"label": label or _line_label(sub), "quantity": quantity, "unit_amount": amount // max(1, quantity), "amount": amount}],
        "subtotal": amount,
        "tax": 0,
        "total": amount,
        "currency": currency,
        # Registered invoices (CGST and SGST, or IGST) are worked out here once the business registers; until then no tax is charged.
        "gst_mode": "registered" if cfg.get("gst_registered") else "not_registered",
        "note": None if cfg.get("gst_registered") else NOT_REGISTERED_NOTE,
        "period_start": sub.get("current_start"),
        "period_end": sub.get("current_end"),
        "issued_at": now,
    }
    try:
        await _invoices_col().insert_one(dict(doc))
    except DuplicateKeyError:
        return await _invoices_col().find_one({"payment_id": payment_id}, {"_id": 0})
    doc["number"] = await next_number(now, str(cfg.get("invoice_prefix") or "EKV"))
    await _invoices_col().update_one({"id": doc["id"]}, {"$set": {"number": doc["number"]}})
    await _email_receipt(doc)
    return doc


async def _email_receipt(doc: dict[str, Any]) -> None:
    """Email the receipt once, when it is issued: to the email given at checkout, or the workspace owner's. A failed email never
    undoes the receipt, which is always in Billing."""
    from app.core.config import settings
    from app.core.notifications import send_templated_email
    from app.db.mongo import users, workspaces

    try:
        to = str((doc.get("customer") or {}).get("email") or "")
        ws = await workspaces.find_one({"id": doc.get("workspace_id")}, {"owner_id": 1, "name": 1}) or {}
        if not to and ws.get("owner_id"):
            to = str((await users.find_one({"id": ws["owner_id"]}, {"email": 1}) or {}).get("email") or "")
        if not to:
            return
        sent = await send_templated_email("billing-receipt", to, {
            "number": doc["number"], "amount": PR.format_amount(int(doc["total"]), doc["currency"]),
            "item": doc["lines"][0]["label"], "date": f"{doc['issued_at'].day} {doc['issued_at'].strftime('%b %Y')}",
            "workspace_name": ws.get("name") or "your workspace",
            "link": f"{(settings.FRONTEND_URL or '').rstrip('/')}/dashboard/settings?tab=billing&receipt={doc['id']}",
        })
        await _invoices_col().update_one({"id": doc["id"]}, {"$set": {"emailed_to": to if sent else None, "emailed_at": datetime.now(timezone.utc) if sent else None}})
    except Exception as exc:  # noqa: BLE001
        logger.warning("Receipt %s was not emailed: %s", doc.get("number"), exc)


async def list_for_workspace(workspace_id: str, limit: int = 50) -> list[dict[str, Any]]:
    rows = [d async for d in _invoices_col().find({"workspace_id": workspace_id}, {"_id": 0}).sort("issued_at", -1).limit(limit)]
    for row in rows:
        row["status"] = row.get("status") or "paid"
    return rows


async def create_indexes() -> None:
    await _invoices_col().create_index("payment_id", unique=True)
    await _invoices_col().create_index("number", unique=True)
    await _invoices_col().create_index([("workspace_id", 1), ("issued_at", -1)])
