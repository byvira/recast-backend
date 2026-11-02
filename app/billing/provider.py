"""The payment provider, behind one interface, so a second one (a merchant of record abroad) can be added without touching the rest.

Everything about money that leaves Ekavo goes through here. The provider is the source of truth for money; Ekavo keeps a mirror of
each subscription and payment so screens never wait on a provider call.
"""
from __future__ import annotations

from typing import Any, Optional, Protocol


class ProviderError(Exception):
    """The provider refused or could not be reached. `message` is safe to show a member."""

    def __init__(self, message: str, *, status: Optional[int] = None, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


class PaymentProvider(Protocol):
    name: str

    @property
    def mode(self) -> str:
        """"test" or "live", from the keys in use."""
        ...

    @property
    def public_key(self) -> str:
        """The key the browser's checkout opens with. Never the secret."""
        ...

    def configured(self) -> bool: ...

    async def create_plan(self, *, period: str, amount: int, currency: str, name: str, description: str, notes: dict[str, str]) -> str:
        """A recurring price at the provider. Returns its id. A provider plan's amount can never change."""
        ...

    async def create_subscription(
        self, *, plan_id: str, quantity: int, total_count: int, notes: dict[str, str], start_at: Optional[int] = None,
        upfront: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Open a subscription. `start_at` delays the first regular charge to that moment; `upfront` ({name, amount, currency})
        is charged when the mandate is set up, for the days until then."""
        ...

    async def fetch_subscription(self, subscription_id: str) -> dict[str, Any]: ...

    async def cancel_subscription(self, subscription_id: str, *, at_cycle_end: bool) -> dict[str, Any]: ...

    async def pause_subscription(self, subscription_id: str) -> dict[str, Any]: ...

    async def resume_subscription(self, subscription_id: str) -> dict[str, Any]: ...

    async def update_subscription(
        self, subscription_id: str, *, plan_id: Optional[str] = None, quantity: Optional[int] = None, at_cycle_end: bool,
    ) -> dict[str, Any]: ...

    async def create_order(self, *, amount: int, currency: str, receipt: str, notes: dict[str, str]) -> dict[str, Any]: ...

    async def refund(self, payment_id: str, *, amount: Optional[int] = None, notes: Optional[dict[str, str]] = None) -> dict[str, Any]: ...

    async def latest_invoice(self, subscription_id: str) -> Optional[dict[str, Any]]:
        """The subscription's newest invoice, with the link the customer pays or approves it at (`short_url`), or None."""
        ...

    def verify_checkout(self, *, payment_id: str, subscription_id: str, signature: str) -> bool: ...

    async def fetch_payment(self, payment_id: str) -> dict[str, Any]: ...

    async def capture_payment(self, payment_id: str, *, amount: int, currency: str) -> dict[str, Any]: ...

    def verify_order_payment(self, *, order_id: str, payment_id: str, signature: str) -> bool: ...

    def verify_webhook(self, body: bytes, signature: str) -> bool: ...


_override: Optional[PaymentProvider] = None


def get_provider() -> PaymentProvider:
    """The provider in use. Tests swap it with `use_provider`."""
    if _override is not None:
        return _override
    from app.billing.razorpay import RazorpayProvider

    return RazorpayProvider()


def use_provider(provider: Optional[PaymentProvider]) -> None:
    global _override
    _override = provider
