"""
ap2/mandate.py
==============
AP2 Mandate Dataclasses
------------------------
Defines the two core data structures of the Agent Payments Protocol (AP2):

  IntentMandate  – Captures the user's purchase intent before any agent
                   searches begin.  Think of it as the "outer envelope" that
                   sets hard limits (spending cap, destination, dates,
                   preferences) that every downstream decision must respect.

  CartMandate    – Created after concrete travel options have been selected.
                   It lists every item with its exact price, links back to
                   the parent IntentMandate, and carries its own signature
                   so the full chain of custody can be verified.

Both dataclasses are intentionally plain (no methods beyond __post_init__
validation), keeping business logic in AP2Protocol where it belongs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------


class IntentStatus:
    ACTIVE = "active"  # Mandate is live; agents may search / select
    FULFILLED = "fulfilled"  # A CartMandate was successfully charged
    EXPIRED = "expired"  # TTL elapsed without fulfilment
    CANCELLED = "cancelled"  # Explicitly cancelled by the user or system


class CartStatus:
    PENDING_APPROVAL = "pending_approval"  # Awaiting explicit user confirmation
    APPROVED = "approved"  # User confirmed; ready to charge
    CHARGED = "charged"  # Payment processor succeeded
    FAILED = "failed"  # Payment processor rejected
    CANCELLED = "cancelled"  # Cancelled before charging


# ---------------------------------------------------------------------------
# IntentMandate
# ---------------------------------------------------------------------------


@dataclass
class IntentMandate:
    """
    AP2 Intent Mandate
    ------------------
    Captures the user's high-level purchase intent *before* any agent begins
    searching for travel options.  It establishes hard constraints that every
    subsequent CartMandate must satisfy (verified by AP2Protocol.verify_mandate_chain).

    Creating an IntentMandate first is the cornerstone of the AP2 protocol: it
    prevents autonomous agents from making open-ended purchases and gives the
    user (and auditors) a cryptographically-anchored record of what was
    originally authorised.

    Fields
    ------
    mandate_id         : Globally unique identifier for this mandate.
                         Format: "INTENT-<hex>" (assigned by AP2Protocol).
    user_id            : Identifier of the human (or upstream agent) who
                         authorised this mandate.
    intent_description : Free-text description of what the user wants to buy.
                         Example: "Book round trip to Paris, 3 nights, under $2000"
    spending_cap       : Maximum total amount (in `currency`) that may be
                         charged under this mandate.  Any CartMandate whose
                         total_amount exceeds this value will fail
                         verify_mandate_chain().
    destination        : Primary destination city / region.
                         Example: "Paris", "London"
    travel_dates       : Dict describing the outbound and (optional) return dates.
                         Example: {"outbound": "2025-09-15", "return": "2025-09-18"}
    constraints        : Dict of preference / filter constraints that agents
                         should respect when searching.
                         Example: {"min_hotel_stars": 3,
                                   "prefer_direct_flights": True,
                                   "include_car_rental": False}
    currency           : ISO 4217 currency code for spending_cap and amounts.
                         Defaults to "USD".
    created_at         : ISO-8601 datetime string when this mandate was created
                         (assigned by AP2Protocol.create_intent_mandate).
    expires_at         : ISO-8601 datetime string after which the mandate is
                         considered expired and cannot be fulfilled.  Optional;
                         if None, the mandate does not expire automatically.
    signature          : Simulated cryptographic signature over the mandate
                         contents (assigned by AP2Protocol.sign_mandate).
                         In production this would be an ECDSA or RSA-PSS
                         signature verifiable with the user's public key.
    status             : Lifecycle status string (see IntentStatus constants).
    metadata           : Arbitrary extra key/value pairs for extensibility
                         (e.g. loyalty programme number, trip purpose).
    """

    mandate_id: str
    user_id: str
    intent_description: str
    spending_cap: float
    destination: str
    travel_dates: Dict[str, str]
    constraints: Dict[str, Any]
    created_at: str
    signature: str
    status: str = IntentStatus.ACTIVE
    currency: str = "USD"
    expires_at: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def __post_init__(self) -> None:
        if self.spending_cap <= 0:
            raise ValueError(
                f"IntentMandate.spending_cap must be positive; got {self.spending_cap}"
            )
        if not self.destination.strip():
            raise ValueError("IntentMandate.destination must not be empty.")
        if not self.travel_dates:
            raise ValueError(
                "IntentMandate.travel_dates must contain at least one entry."
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def is_active(self) -> bool:
        """Return True if the mandate is in the ACTIVE state."""
        return self.status == IntentStatus.ACTIVE

    def to_dict(self) -> dict:
        """Serialise to a plain dictionary (for logging and wire transfer)."""
        return {
            "mandate_id": self.mandate_id,
            "user_id": self.user_id,
            "intent_description": self.intent_description,
            "spending_cap": self.spending_cap,
            "destination": self.destination,
            "travel_dates": self.travel_dates,
            "constraints": self.constraints,
            "currency": self.currency,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "signature": self.signature[:16] + "..." if self.signature else "",
            "status": self.status,
            "metadata": self.metadata,
        }

    def __str__(self) -> str:
        return (
            f"IntentMandate("
            f"id={self.mandate_id!r}, "
            f"user={self.user_id!r}, "
            f"dest={self.destination!r}, "
            f"cap={self.currency} {self.spending_cap:.2f}, "
            f"status={self.status!r})"
        )

    def __repr__(self) -> str:
        return self.__str__()


# ---------------------------------------------------------------------------
# CartMandate
# ---------------------------------------------------------------------------


@dataclass
class CartMandate:
    """
    AP2 Cart Mandate
    ----------------
    The final, itemised purchase basket produced after agents have searched for
    and selected concrete travel components.  It links back to the originating
    IntentMandate (via intent_mandate_id) and carries its own cryptographic
    signature over the item list and total so neither can be altered after
    creation without invalidating the signature.

    Before the PaymentProcessor will accept a CartMandate it must:
      1. Pass AP2Protocol.verify_mandate_chain() — total <= spending_cap,
         destination matches, and all other intent constraints are met.
      2. Have user_approved set to True (either by explicit user interaction
         or by a pre-authorised delegated mandate).

    Fields
    ------
    cart_id            : Globally unique identifier for this cart.
                         Format: "CART-<hex>" (assigned by AP2Protocol).
    intent_mandate_id  : The mandate_id of the parent IntentMandate.
                         Links the cart back to the user's original authorisation.
    user_id            : Must match IntentMandate.user_id.
    items              : Ordered list of item dicts, each describing one
                         travel component.  Each item should contain at minimum:
                           {"type": "flight"|"hotel"|"car",
                            "id": "<provider id>",
                            "description": "<human readable>",
                            "price": <float>,
                            "currency": "USD"}
    total_amount       : Sum of all item prices.  AP2Protocol computes this
                         automatically when creating the CartMandate.
    currency           : ISO 4217 currency code (must match IntentMandate).
    merchant_ids       : List of merchant / provider identifiers for every
                         item in the cart (one per item, in the same order).
    created_at         : ISO-8601 datetime string when this cart was created.
    user_approved      : Whether the user (or a pre-authorised delegate) has
                         explicitly confirmed this cart.  Must be True before
                         PaymentProcessor.process_payment() will proceed.
    signature          : Cryptographic signature over the concatenation of
                         cart_id + items + total_amount (assigned by
                         AP2Protocol.sign_mandate).
    status             : Lifecycle status string (see CartStatus constants).
    approval_method    : How user_approved was set.
                         "interactive"  – user reviewed and confirmed manually.
                         "delegated"    – pre-authorised via a signed IntentMandate
                                          that allows autonomous fulfilment.
                         ""             – not yet approved.
    metadata           : Arbitrary extra key/value pairs (trace IDs, etc.).
    """

    cart_id: str
    intent_mandate_id: str
    user_id: str
    items: List[Dict[str, Any]]
    total_amount: float
    currency: str
    merchant_ids: List[str]
    created_at: str
    user_approved: bool
    signature: str
    status: str = CartStatus.PENDING_APPROVAL
    approval_method: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def __post_init__(self) -> None:
        if not self.items:
            raise ValueError("CartMandate.items must not be empty.")
        if self.total_amount < 0:
            raise ValueError(
                f"CartMandate.total_amount must be >= 0; got {self.total_amount}"
            )
        if len(self.merchant_ids) != len(self.items):
            raise ValueError(
                f"CartMandate.merchant_ids length ({len(self.merchant_ids)}) "
                f"must match items length ({len(self.items)})."
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def is_approved(self) -> bool:
        """Return True if the cart has been explicitly approved by the user."""
        return self.user_approved and self.status in (
            CartStatus.APPROVED,
            CartStatus.CHARGED,
        )

    def item_summary(self) -> str:
        """Return a compact one-line description of cart contents."""
        parts = []
        for item in self.items:
            item_type = item.get("type", "item")
            item_desc = item.get("description", item.get("id", "?"))
            item_price = item.get("price", 0.0)
            parts.append(f"{item_type.capitalize()}: {item_desc} (${item_price:.2f})")
        return " | ".join(parts)

    def to_dict(self) -> dict:
        """Serialise to a plain dictionary (for logging and wire transfer)."""
        return {
            "cart_id": self.cart_id,
            "intent_mandate_id": self.intent_mandate_id,
            "user_id": self.user_id,
            "items": self.items,
            "total_amount": self.total_amount,
            "currency": self.currency,
            "merchant_ids": self.merchant_ids,
            "created_at": self.created_at,
            "user_approved": self.user_approved,
            "approval_method": self.approval_method,
            "signature": self.signature[:16] + "..." if self.signature else "",
            "status": self.status,
            "metadata": self.metadata,
        }

    def __str__(self) -> str:
        approved_tag = "APPROVED" if self.user_approved else "PENDING APPROVAL"
        return (
            f"CartMandate("
            f"id={self.cart_id!r}, "
            f"intent={self.intent_mandate_id!r}, "
            f"items={len(self.items)}, "
            f"total={self.currency} {self.total_amount:.2f}, "
            f"status={self.status!r}, "
            f"{approved_tag})"
        )

    def __repr__(self) -> str:
        return self.__str__()
