"""
ap2/payment_processor.py
=========================
AP2 Payment Processor
----------------------
Simulates the secure payment processing layer of the Agent Payments Protocol.

The PaymentProcessor is the final step in the AP2 workflow.  It receives a
fully-approved CartMandate and performs the following verifications before
any charge is simulated:

  1. User approval gate   – cart_mandate.user_approved must be True.
  2. Status gate          – cart_mandate.status must be "approved".
  3. Signature integrity  – the cart's SHA-256 signature is re-derived from
                            its contents and compared against the stored value
                            to detect any post-signing tampering.
  4. Amount sanity check  – total_amount must be > 0 and below the hard ceiling.
  5. Item integrity       – every item must have a type, id, and positive price.

Only when all five checks pass does the processor simulate a charge and return
a transaction receipt.  Any failure returns a structured error dict with a
human-readable reason so the orchestrator can surface it to the user.

All steps emit [AP2] PAYMENT-prefixed log lines.

No external dependencies – stdlib only (hashlib, uuid, datetime, json).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .mandate import CartMandate, CartStatus, IntentMandate

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Hard ceiling on a single transaction (safety net against runaway agents)
_MAX_CHARGE_AMOUNT: float = 50_000.00

# Signing secret – must match the one in AP2Protocol so re-verification works
_SIGNING_SECRET: str = "ap2-travelmind-signing-secret-2025"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Return current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_txn_id() -> str:
    """Generate a unique transaction reference number."""
    return f"TXN-{uuid.uuid4().hex[:12].upper()}"


def _new_receipt_id() -> str:
    """Generate a unique receipt reference number."""
    return f"RCP-{uuid.uuid4().hex[:10].upper()}"


def _recompute_cart_signature(cart: CartMandate) -> str:
    """
    Re-derive the CartMandate signature using the same algorithm as
    AP2Protocol.sign_mandate() so we can detect post-signing tampering
    without importing AP2Protocol (avoids circular imports).
    """
    payload_parts = [
        cart.cart_id,
        cart.intent_mandate_id,
        cart.user_id,
        str(cart.total_amount),
        cart.currency,
        json.dumps(cart.items, sort_keys=True),
        cart.created_at,
        _SIGNING_SECRET,
    ]
    signing_input = "|".join(payload_parts)
    return hashlib.sha256(signing_input.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# PaymentProcessor
# ---------------------------------------------------------------------------


class PaymentProcessor:
    """
    Simulates a secure payment gateway that charges a fully-approved
    CartMandate.

    Usage
    -----
    processor = PaymentProcessor()
    receipt   = processor.process_payment(cart_mandate)

    if receipt["success"]:
        print(f"Charged {receipt['amount']} — TXN: {receipt['transaction_id']}")
    else:
        print(f"Payment failed: {receipt['error']}")

    Attributes
    ----------
    verbose : bool
        When True (default), lifecycle steps are printed with [AP2] prefix.
    _transaction_log : list[dict]
        Append-only in-memory log of every attempted transaction (success or
        failure) for audit purposes.
    """

    def __init__(self, verbose: bool = True) -> None:
        self.verbose = verbose
        self._transaction_log: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _log(self, message: str) -> None:
        if self.verbose:
            print(message)

    def _record(self, entry: Dict[str, Any]) -> None:
        """Append a transaction record to the in-memory audit log."""
        self._transaction_log.append(entry)

    def _fail(
        self,
        cart: CartMandate,
        reason: str,
        txn_id: str,
        started_at: str,
    ) -> Dict[str, Any]:
        """
        Build a failure receipt, mark the cart as FAILED, log the result,
        and append to the transaction log.
        """
        cart.status = CartStatus.FAILED

        self._log(f"[AP2] PAYMENT ERROR: {reason}")
        self._log(f"[AP2] PAYMENT: Status → FAILED ✗")
        self._log(f"[AP2] ══════════════════════════════════════════════════\n")

        result: Dict[str, Any] = {
            "success": False,
            "transaction_id": txn_id,
            "cart_id": cart.cart_id,
            "user_id": cart.user_id,
            "amount": cart.total_amount,
            "currency": cart.currency,
            "error": reason,
            "attempted_at": started_at,
            "receipt": None,
        }
        self._record(result)
        return result

    # ------------------------------------------------------------------
    # Core method
    # ------------------------------------------------------------------

    def process_payment(
        self,
        cart_mandate: CartMandate,
        intent_mandate: Optional[IntentMandate] = None,
    ) -> Dict[str, Any]:
        """
        Process a payment for an approved CartMandate.

        The method runs five pre-charge verification steps and only simulates
        a charge if all pass.  On success the CartMandate status is advanced
        to CHARGED and a detailed receipt dict is returned.

        Parameters
        ----------
        cart_mandate    : The CartMandate to charge.  Must be in APPROVED
                          status with user_approved == True.
        intent_mandate  : Optional parent IntentMandate.  If supplied, an
                          extra spending-cap re-check is performed for defence
                          in depth (belt-and-suspenders after verify_mandate_chain).

        Returns
        -------
        dict with keys:
          success        : bool
          transaction_id : str   e.g. "TXN-4F2A1C..."
          receipt_id     : str   e.g. "RCP-9E3B..."
          cart_id        : str
          user_id        : str
          amount         : float  (total charged)
          currency       : str
          items_charged  : list[dict]  (one entry per cart item)
          charged_at     : str   ISO-8601 UTC timestamp
          approval_method: str   how the cart was approved
          error          : str | None  (None on success)
          receipt        : dict | None (None on failure)
        """
        started_at = _now_iso()
        txn_id = _new_txn_id()

        self._log(f"\n[AP2] ══════════════════════════════════════════════════")
        self._log(f"[AP2] PAYMENT: Initiating payment processing")
        self._log(f"[AP2]   Transaction ID : {txn_id}")
        self._log(f"[AP2]   Cart ID        : {cart_mandate.cart_id}")
        self._log(f"[AP2]   User           : {cart_mandate.user_id!r}")
        self._log(
            f"[AP2]   Amount         : "
            f"{cart_mandate.currency} {cart_mandate.total_amount:.2f}"
        )
        self._log(f"[AP2]   Approval Method: {cart_mandate.approval_method or 'N/A'}")
        self._log(f"[AP2] ────────────────────────────────────────────────────")
        self._log(f"[AP2] PAYMENT: Running pre-charge verification checks...")

        # ----------------------------------------------------------------
        # Check 1 – User approval gate
        # ----------------------------------------------------------------
        if not cart_mandate.user_approved:
            return self._fail(
                cart_mandate,
                "Payment refused: CartMandate has not been approved by the user. "
                "Call AP2Protocol.user_approve_cart() before processing payment.",
                txn_id,
                started_at,
            )
        self._log(f"[AP2]   [PASS] Check 1/5: User approval gate ✓")

        # ----------------------------------------------------------------
        # Check 2 – Status gate
        # ----------------------------------------------------------------
        if cart_mandate.status != CartStatus.APPROVED:
            return self._fail(
                cart_mandate,
                f"Payment refused: CartMandate status is {cart_mandate.status!r}; "
                f"expected 'approved'.  Only APPROVED carts may be charged.",
                txn_id,
                started_at,
            )
        self._log(f"[AP2]   [PASS] Check 2/5: Cart status = 'approved' ✓")

        # ----------------------------------------------------------------
        # Check 3 – Signature integrity
        # ----------------------------------------------------------------
        expected_sig = _recompute_cart_signature(cart_mandate)
        if cart_mandate.signature != expected_sig:
            return self._fail(
                cart_mandate,
                "Payment refused: CartMandate signature mismatch — the cart "
                "contents appear to have been tampered with after signing.  "
                "The transaction cannot proceed.",
                txn_id,
                started_at,
            )
        self._log(
            f"[AP2]   [PASS] Check 3/5: Signature integrity ✓ "
            f"({cart_mandate.signature[:20]}...)"
        )

        # ----------------------------------------------------------------
        # Check 4 – Amount sanity check
        # ----------------------------------------------------------------
        if cart_mandate.total_amount <= 0:
            return self._fail(
                cart_mandate,
                f"Payment refused: total_amount must be > 0 "
                f"(got {cart_mandate.total_amount:.2f}).",
                txn_id,
                started_at,
            )
        if cart_mandate.total_amount > _MAX_CHARGE_AMOUNT:
            return self._fail(
                cart_mandate,
                f"Payment refused: total_amount "
                f"{cart_mandate.currency} {cart_mandate.total_amount:.2f} "
                f"exceeds the hard ceiling of "
                f"{cart_mandate.currency} {_MAX_CHARGE_AMOUNT:.2f}.",
                txn_id,
                started_at,
            )
        # Optional belt-and-suspenders cap check against IntentMandate
        if intent_mandate is not None:
            if cart_mandate.total_amount > intent_mandate.spending_cap:
                return self._fail(
                    cart_mandate,
                    f"Payment refused: total_amount "
                    f"{cart_mandate.currency} {cart_mandate.total_amount:.2f} "
                    f"exceeds IntentMandate spending cap of "
                    f"{intent_mandate.currency} {intent_mandate.spending_cap:.2f}.",
                    txn_id,
                    started_at,
                )
        self._log(
            f"[AP2]   [PASS] Check 4/5: Amount sanity "
            f"(${cart_mandate.total_amount:.2f} within limits) ✓"
        )

        # ----------------------------------------------------------------
        # Check 5 – Item integrity
        # ----------------------------------------------------------------
        item_errors: List[str] = []
        for idx, item in enumerate(cart_mandate.items):
            if not item.get("type"):
                item_errors.append(f"Item [{idx}] missing 'type' field.")
            if not item.get("id"):
                item_errors.append(f"Item [{idx}] missing 'id' field.")
            price = float(item.get("price", -1))
            if price < 0:
                item_errors.append(
                    f"Item [{idx}] has invalid price: {item.get('price')!r}."
                )
        if item_errors:
            return self._fail(
                cart_mandate,
                "Payment refused: item integrity check failed — "
                + "; ".join(item_errors),
                txn_id,
                started_at,
            )
        self._log(
            f"[AP2]   [PASS] Check 5/5: Item integrity "
            f"({len(cart_mandate.items)} item(s) valid) ✓"
        )

        # ----------------------------------------------------------------
        # All checks passed — simulate the charge
        # ----------------------------------------------------------------
        self._log(f"[AP2] ────────────────────────────────────────────────────")
        self._log(
            f"[AP2] PAYMENT: All {5} verification checks passed. "
            f"Initiating charge simulation..."
        )

        receipt_id = _new_receipt_id()
        charged_at = _now_iso()

        # Build per-item charge records
        items_charged: List[Dict[str, Any]] = []
        for item in cart_mandate.items:
            items_charged.append(
                {
                    "type": item.get("type", "item"),
                    "id": item.get("id", "?"),
                    "description": item.get("description", item.get("id", "?")),
                    "merchant_id": item.get(
                        "merchant_id", item.get("id", "unknown-merchant")
                    ),
                    "amount_charged": float(item.get("price", 0)),
                    "currency": cart_mandate.currency,
                    "status": "charged",
                }
            )
            self._log(
                f"[AP2]   → Charged {item.get('type', 'item').upper():8s} "
                f"{str(item.get('description', item.get('id', '?'))):<35s} "
                f"${float(item.get('price', 0)):.2f}"
            )

        # Advance cart status
        cart_mandate.status = CartStatus.CHARGED

        # Build the receipt
        receipt: Dict[str, Any] = {
            "receipt_id": receipt_id,
            "transaction_id": txn_id,
            "cart_id": cart_mandate.cart_id,
            "intent_mandate_id": cart_mandate.intent_mandate_id,
            "user_id": cart_mandate.user_id,
            "items": items_charged,
            "subtotals": {
                item_type: sum(
                    i["amount_charged"] for i in items_charged if i["type"] == item_type
                )
                for item_type in {i["type"] for i in items_charged}
            },
            "total_charged": cart_mandate.total_amount,
            "currency": cart_mandate.currency,
            "approval_method": cart_mandate.approval_method,
            "charged_at": charged_at,
            "payment_gateway": "TravelMind-AP2-SimGateway-v1",
            "status": "charged",
        }

        # Final log block
        self._log(f"[AP2] ────────────────────────────────────────────────────")
        self._log(f"[AP2] PAYMENT SUCCESS ✓")
        self._log(f"[AP2]   Transaction ID : {txn_id}")
        self._log(f"[AP2]   Receipt ID     : {receipt_id}")
        self._log(
            f"[AP2]   Total Charged  : "
            f"{cart_mandate.currency} {cart_mandate.total_amount:.2f}"
        )
        self._log(f"[AP2]   Charged At     : {charged_at}")
        self._log(f"[AP2] PAYMENT: CartMandate status → CHARGED ✓")
        self._log(f"[AP2] ══════════════════════════════════════════════════\n")

        result: Dict[str, Any] = {
            "success": True,
            "transaction_id": txn_id,
            "receipt_id": receipt_id,
            "cart_id": cart_mandate.cart_id,
            "user_id": cart_mandate.user_id,
            "amount": cart_mandate.total_amount,
            "currency": cart_mandate.currency,
            "items_charged": items_charged,
            "charged_at": charged_at,
            "approval_method": cart_mandate.approval_method,
            "error": None,
            "receipt": receipt,
        }
        self._record(result)
        return result

    # ------------------------------------------------------------------
    # Refund simulation (bonus – not called in main demo but useful)
    # ------------------------------------------------------------------

    def refund_transaction(
        self,
        transaction_id: str,
        reason: str = "User requested refund",
    ) -> Dict[str, Any]:
        """
        Simulate a full refund of a previously charged transaction.

        Looks up the original transaction in the audit log, creates a
        refund record, and returns a refund receipt.

        Parameters
        ----------
        transaction_id : str   The TXN-... id from the original charge receipt.
        reason         : str   Human-readable refund reason.

        Returns
        -------
        dict with keys: success, refund_id, transaction_id, amount, currency,
                        reason, refunded_at, error.
        """
        original = next(
            (
                t
                for t in self._transaction_log
                if t.get("transaction_id") == transaction_id
            ),
            None,
        )

        refund_id = f"REF-{uuid.uuid4().hex[:10].upper()}"
        refunded_at = _now_iso()

        self._log(f"\n[AP2] REFUND: Processing refund for TXN {transaction_id!r}")

        if original is None:
            self._log(f"[AP2] REFUND ERROR: Transaction {transaction_id!r} not found.")
            return {
                "success": False,
                "refund_id": refund_id,
                "transaction_id": transaction_id,
                "amount": 0,
                "currency": "USD",
                "reason": reason,
                "refunded_at": refunded_at,
                "error": f"Transaction {transaction_id!r} not found in payment log.",
            }

        if not original.get("success"):
            self._log(
                f"[AP2] REFUND ERROR: Original transaction was not successful; "
                f"cannot refund."
            )
            return {
                "success": False,
                "refund_id": refund_id,
                "transaction_id": transaction_id,
                "amount": 0,
                "currency": original.get("currency", "USD"),
                "reason": reason,
                "refunded_at": refunded_at,
                "error": "Cannot refund a failed transaction.",
            }

        amount = original.get("amount", 0)
        currency = original.get("currency", "USD")

        self._log(f"[AP2] REFUND SUCCESS ✓")
        self._log(f"[AP2]   Refund ID      : {refund_id}")
        self._log(f"[AP2]   Original TXN   : {transaction_id}")
        self._log(f"[AP2]   Amount         : {currency} {amount:.2f}")
        self._log(f"[AP2]   Reason         : {reason!r}")
        self._log(f"[AP2]   Refunded At    : {refunded_at}\n")

        return {
            "success": True,
            "refund_id": refund_id,
            "transaction_id": transaction_id,
            "amount": amount,
            "currency": currency,
            "reason": reason,
            "refunded_at": refunded_at,
            "error": None,
        }

    # ------------------------------------------------------------------
    # Audit log access
    # ------------------------------------------------------------------

    def get_transaction_log(self) -> List[Dict[str, Any]]:
        """Return a copy of the full transaction audit log."""
        return list(self._transaction_log)

    def get_transaction(self, transaction_id: str) -> Optional[Dict[str, Any]]:
        """Look up a single transaction by its ID."""
        return next(
            (
                t
                for t in self._transaction_log
                if t.get("transaction_id") == transaction_id
            ),
            None,
        )

    def transaction_summary(self) -> str:
        """Return a human-readable summary of all recorded transactions."""
        if not self._transaction_log:
            return "PaymentProcessor: no transactions recorded."
        lines = [f"PaymentProcessor — {len(self._transaction_log)} transaction(s):"]
        for txn in self._transaction_log:
            status_icon = "✓" if txn.get("success") else "✗"
            lines.append(
                f"  {status_icon} {txn.get('transaction_id', '?'):20s} "
                f"{txn.get('currency', 'USD')} {float(txn.get('amount', 0)):>10.2f}  "
                f"user={txn.get('user_id', '?')!r}"
            )
        return "\n".join(lines)

    def __repr__(self) -> str:
        return f"PaymentProcessor(transactions={len(self._transaction_log)})"
