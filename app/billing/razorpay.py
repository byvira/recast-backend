"""Razorpay, through its REST API: plans, subscriptions with an autopay mandate (UPI Autopay or a card), one-time orders and refunds.

The key id and secret come from the environment only. A test key (`rzp_test_`) and a live key are never mixed: the mode follows the key.
Signatures are checked with a constant-time compare: a checkout result with the key secret, a webhook with the webhook secret.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any, Optional

import httpx

from app.billing.provider import ProviderError
from app.core.config import settings

logger = logging.getLogger(__name__)

API = "https://api.razorpay.com/v1"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)


def _sign(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


class RazorpayProvider:
    name = "razorpay"

    def __init__(self, key_id: Optional[str] = None, key_secret: Optional[str] = None, webhook_secret: Optional[str] = None) -> None:
        # a space or line break pasted into the dashboard with a key makes Razorpay answer "Authentication failed"
        self._key_id = (key_id if key_id is not None else settings.RAZORPAY_KEY_ID or "").strip()
        self._key_secret = (key_secret if key_secret is not None else settings.RAZORPAY_KEY_SECRET or "").strip()
        self._webhook_secret = (webhook_secret if webhook_secret is not None else settings.RAZORPAY_WEBHOOK_SECRET or "").strip()

    @property
    def mode(self) -> str:
        return "live" if self._key_id.startswith("rzp_live_") else "test"

    @property
    def public_key(self) -> str:
        return self._key_id

    def configured(self) -> bool:
        return bool(self._key_id and self._key_secret)

    async def _call(self, method: str, path: str, body: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        if not self.configured():
            raise ProviderError("Payments are not set up yet.", code="not_configured")
        try:
            async with httpx.AsyncClient(base_url=API, timeout=TIMEOUT, auth=(self._key_id, self._key_secret)) as client:
                res = await client.request(method, path, json=body)
        except httpx.HTTPError as exc:
            logger.warning("Razorpay %s %s could not be reached: %s", method, path, exc)
            raise ProviderError("The payment service could not be reached. Try again in a moment.", code="unreachable") from exc
        try:
            data = res.json()
        except ValueError:
            data = {}
        if res.status_code >= 400:
            err = (data or {}).get("error") or {}
            if res.status_code == 401:
                # the keys in the server settings are wrong; the member can't fix that, so they get plain words and Ops gets the log
                logger.error("Razorpay %s %s refused the keys (401, %s mode): check RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET", method, path, self.mode)
                raise ProviderError("Payments aren't working right now. We've been told and are fixing it. Try again later.",
                                    status=401, code="auth_failed")
            logger.warning("Razorpay %s %s refused (%s): %s", method, path, res.status_code, err)
            raise ProviderError(
                str(err.get("description") or "The payment service refused this. Try again."), status=res.status_code, code=err.get("code"),
            )
        return data

    async def create_plan(self, *, period: str, amount: int, currency: str, name: str, description: str, notes: dict[str, str]) -> str:
        data = await self._call("POST", "/plans", {
            "period": period, "interval": 1,
            "item": {"name": name, "amount": int(amount), "currency": currency, "description": description},
            "notes": notes,
        })
        return str(data["id"])

    async def create_subscription(
        self, *, plan_id: str, quantity: int, total_count: int, notes: dict[str, str], start_at: Optional[int] = None,
        upfront: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"plan_id": plan_id, "quantity": int(quantity), "total_count": int(total_count), "customer_notify": 1, "notes": notes}
        if start_at:
            body["start_at"] = int(start_at)
        if upfront:
            # an add-on is charged together with the mandate's first payment, before start_at
            body["addons"] = [{"item": {"name": upfront["name"], "amount": int(upfront["amount"]), "currency": upfront["currency"]}}]
        return await self._call("POST", "/subscriptions", body)

    async def fetch_subscription(self, subscription_id: str) -> dict[str, Any]:
        return await self._call("GET", f"/subscriptions/{subscription_id}")

    async def cancel_subscription(self, subscription_id: str, *, at_cycle_end: bool) -> dict[str, Any]:
        return await self._call("POST", f"/subscriptions/{subscription_id}/cancel", {"cancel_at_cycle_end": 1 if at_cycle_end else 0})

    async def pause_subscription(self, subscription_id: str) -> dict[str, Any]:
        return await self._call("POST", f"/subscriptions/{subscription_id}/pause", {"pause_at": "now"})

    async def resume_subscription(self, subscription_id: str) -> dict[str, Any]:
        return await self._call("POST", f"/subscriptions/{subscription_id}/resume", {"resume_at": "now"})

    async def update_subscription(
        self, subscription_id: str, *, plan_id: Optional[str] = None, quantity: Optional[int] = None, at_cycle_end: bool,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"schedule_change_at": "cycle_end" if at_cycle_end else "now", "customer_notify": 1}
        if plan_id:
            body["plan_id"] = plan_id
        if quantity is not None:
            body["quantity"] = int(quantity)
        return await self._call("PATCH", f"/subscriptions/{subscription_id}", body)

    async def create_order(self, *, amount: int, currency: str, receipt: str, notes: dict[str, str]) -> dict[str, Any]:
        return await self._call("POST", "/orders", {"amount": int(amount), "currency": currency, "receipt": receipt[:40], "notes": notes})

    async def refund(self, payment_id: str, *, amount: Optional[int] = None, notes: Optional[dict[str, str]] = None) -> dict[str, Any]:
        body: dict[str, Any] = {"notes": notes or {}}
        if amount is not None:
            body["amount"] = int(amount)
        return await self._call("POST", f"/payments/{payment_id}/refund", body)

    async def fetch_payment(self, payment_id: str) -> dict[str, Any]:
        return await self._call("GET", f"/payments/{payment_id}")

    async def capture_payment(self, payment_id: str, *, amount: int, currency: str) -> dict[str, Any]:
        return await self._call("POST", f"/payments/{payment_id}/capture", {"amount": int(amount), "currency": currency})

    async def latest_invoice(self, subscription_id: str) -> Optional[dict[str, Any]]:
        data = await self._call("GET", f"/invoices?subscription_id={subscription_id}&count=1")
        items = data.get("items") or []
        return items[0] if items else None

    def verify_checkout(self, *, payment_id: str, subscription_id: str, signature: str) -> bool:
        if not (self._key_secret and payment_id and subscription_id and signature):
            return False
        expected = _sign(self._key_secret, f"{payment_id}|{subscription_id}".encode())
        return hmac.compare_digest(expected, signature)

    def verify_order_payment(self, *, order_id: str, payment_id: str, signature: str) -> bool:
        if not (self._key_secret and order_id and payment_id and signature):
            return False
        return hmac.compare_digest(_sign(self._key_secret, f"{order_id}|{payment_id}".encode()), signature)

    def verify_webhook(self, body: bytes, signature: str) -> bool:
        if not (self._webhook_secret and signature):
            return False
        return hmac.compare_digest(_sign(self._webhook_secret, body), signature)
