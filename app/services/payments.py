"""A stand-in for a payment provider.

Mirrors how real providers' test modes work: the token decides the outcome.
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal

DECLINED_TOKEN = "tok_declined"


@dataclass(frozen=True)
class ChargeResult:
    succeeded: bool
    provider_ref: str


def charge(amount: Decimal, payment_token: str) -> ChargeResult:
    """Charge `amount`. The token `tok_declined` is always declined; others succeed."""
    if amount <= 0:
        raise ValueError("amount must be positive")
    return ChargeResult(
        succeeded=payment_token != DECLINED_TOKEN,
        provider_ref=f"mock_{uuid.uuid4().hex[:16]}",
    )
