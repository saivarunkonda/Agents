"""
AP2 (Agent Payments Protocol) Package
=======================================
Implements the Agent Payments Protocol for TravelMind.

The AP2 protocol governs how autonomous agents handle financial transactions
on behalf of users.  It introduces a two-mandate model that separates the
user's *intent* (what they want to buy and how much they're willing to spend)
from the *cart* (the specific items that were actually selected and priced),
ensuring that agents can never silently overspend or substitute items without
a verifiable audit trail.

Two-Mandate Model
-----------------
1. IntentMandate  – Created FIRST, before any agent searches begin.
                    Captures the user's high-level purchase intent, spending
                    cap, destination, travel dates, and any preference
                    constraints.  Cryptographically signed so it cannot be
                    tampered with after creation.

2. CartMandate    – Created AFTER agents have found concrete travel options.
                    Lists every item (flight, hotel, car) with its exact price,
                    links back to the IntentMandate via intent_mandate_id, and
                    carries its own cryptographic signature over the full item
                    list and total.  The verify_mandate_chain() check confirms
                    the cart respects every constraint expressed in the intent.

Workflow
--------
  ap2.create_intent_mandate(...)          -> IntentMandate  (signed, status=active)
      |
      |  ... agents search and select items ...
      |
  ap2.create_cart_mandate(intent, items)  -> CartMandate    (signed, status=pending_approval)
      |
  ap2.verify_mandate_chain(cart, intent)  -> bool           (total <= cap, dest matches, …)
      |
  ap2.user_approve_cart(cart)             -> CartMandate    (status=approved, user_approved=True)
      |
  PaymentProcessor.process_payment(cart)  -> receipt dict   (status=charged)

Provides
--------
  IntentMandate      : Dataclass capturing purchase intent with signature.
  CartMandate        : Dataclass capturing the approved cart with signature.
  AP2Protocol        : Orchestrates mandate creation, signing, and verification.
  PaymentProcessor   : Simulates secure charging against an approved CartMandate.
"""

from .ap2_protocol import AP2Protocol
from .mandate import CartMandate, IntentMandate
from .payment_processor import PaymentProcessor

__all__ = [
    "IntentMandate",
    "CartMandate",
    "AP2Protocol",
    "PaymentProcessor",
]
