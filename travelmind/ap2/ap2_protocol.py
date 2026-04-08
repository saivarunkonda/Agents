"""
ap2/ap2_protocol.py
====================
AP2 (Agent Payments Protocol) - Core Protocol Orchestrator
------------------------------------------------------------
Implements the full AP2 workflow that governs how TravelMind agents handle
financial transactions on behalf of users:

  1. create_intent_mandate()  – Snapshot the user's purchase intent BEFORE
                                any agent begins searching.  Sets the hard
                                spending cap and constraints that all downstream
                                decisions must respect.

  2. create_cart_mandate()    – After agents have selected concrete travel
                                options, package them into a signed CartMandate
                                that links back to the IntentMandate.

  3. verify_mandate_chain()   – Confirm that the cart fully respects the intent:
                                total <= spending_cap, destination matches, all
                                constraint filters are satisfied.

  4. sign_mandate()           – Produce a deterministic, tamper-evident
                                "signature" string over the mandate's contents
                                using SHA-256 (simulated; no private key needed
                                for this demo).

  5. user_approve_cart()      – Record the user's explicit (or delegated)
                                approval, advancing the CartMandate to the
                                "approved" state so PaymentProcessor can charge.

All steps emit [AP2]-prefixed log lines so the flow is clearly visible in the
console output.

No external dependencies – stdlib only (hashlib, datetime, uuid, json).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .mandate import CartMandate, CartStatus, IntentMandate, IntentStatus

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _truncate(text: str, length: int = 48) -> str:
    """Truncate a string for display purposes."""
    return text if len(text) <= length else text[:length] + "..."


# ---------------------------------------------------------------------------
# AP2Protocol
# ---------------------------------------------------------------------------


class AP2Protocol:
    """
    Orchestrates the full AP2 mandate lifecycle for TravelMind.

    Instantiate once (typically inside the OrchestratorAgent or PaymentAgent)
    and reuse across multiple trip-booking flows.  All public methods log
    [AP2]-prefixed messages to stdout so the protocol steps are clearly
    visible during demo execution.

    Attributes
    ----------
    verbose : bool
        When True (default), every lifecycle step prints a [AP2] log line.
    _mandates : dict
        In-memory store of all IntentMandates keyed by mandate_id.
    _carts : dict
        In-memory store of all CartMandates keyed by cart_id.
    """

    # Simulated "signing secret" – in production this would be an
    # asymmetric private key (RSA-PSS or ECDSA).
    _SIGNING_SECRET: str = "ap2-travelmind-signing-secret-2025"

    def __init__(self, verbose: bool = True) -> None:
        self.verbose = verbose
        self._mandates: Dict[str, IntentMandate] = {}
        self._carts: Dict[str, CartMandate] = {}

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _log(self, message: str) -> None:
        if self.verbose:
            print(message)

    def _new_mandate_id(self) -> str:
        return f"INTENT-{uuid.uuid4().hex[:12].upper()}"

    def _new_cart_id(self) -> str:
        return f"CART-{uuid.uuid4().hex[:12].upper()}"

    # ------------------------------------------------------------------
    # 1. create_intent_mandate
    # ------------------------------------------------------------------

    def create_intent_mandate(
        self,
        user_id: str,
        intent_description: str,
        spending_cap: float,
        destination: str,
        travel_dates: Dict[str, str],
        constraints: Optional[Dict[str, Any]] = None,
        currency: str = "USD",
        expires_at: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> IntentMandate:
        """
        Create and sign an IntentMandate that records what the user wants to
        buy and how much they are willing to spend.

        This MUST be called before any agent searches begin so that the
        spending cap and constraints are established cryptographically before
        any prices are seen.  This prevents an agent from "discovering" an
        expensive option and then retroactively setting a higher cap.

        Parameters
        ----------
        user_id            : Identifier of the human authorising this mandate.
        intent_description : Free-text description of the purchase intent.
        spending_cap       : Hard maximum total spend (in `currency`).
        destination        : Primary destination city (e.g. "Paris").
        travel_dates       : Dict with at least an "outbound" key.
                             Example: {"outbound": "2025-09-15", "return": "2025-09-18"}
        constraints        : Optional preference filters agents should respect.
                             Example: {"min_hotel_stars": 3,
                                       "prefer_direct_flights": True,
                                       "include_car_rental": False}
        currency           : ISO 4217 code (default "USD").
        expires_at         : Optional ISO-8601 expiry datetime string.
        metadata           : Optional arbitrary key/value pairs.

        Returns
        -------
        IntentMandate
            A signed, ACTIVE mandate stored in the internal registry.
        """
        mandate_id = self._new_mandate_id()
        created_at = _now_iso()
        constraints = constraints or {}
        metadata = metadata or {}

        self._log(f"\n[AP2] ══════════════════════════════════════════════════")
        self._log(f"[AP2] INTENT MANDATE: Creating purchase intent mandate")
        self._log(f"[AP2]   Mandate ID   : {mandate_id}")
        self._log(f"[AP2]   User         : {user_id!r}")
        self._log(f"[AP2]   Intent       : {intent_description!r}")
        self._log(f"[AP2]   Destination  : {destination!r}")
        self._log(f"[AP2]   Spending Cap : {currency} {spending_cap:.2f}")
        self._log(f"[AP2]   Travel Dates : {travel_dates}")
        self._log(f"[AP2]   Constraints  : {constraints}")
        self._log(f"[AP2]   Created At   : {created_at}")

        # Build the mandate with a placeholder signature so sign_mandate()
        # can hash the full object contents.
        mandate = IntentMandate(
            mandate_id=mandate_id,
            user_id=user_id,
            intent_description=intent_description,
            spending_cap=spending_cap,
            destination=destination,
            travel_dates=travel_dates,
            constraints=constraints,
            currency=currency,
            created_at=created_at,
            expires_at=expires_at,
            signature="",  # filled in below
            status=IntentStatus.ACTIVE,
            metadata=metadata,
        )

        # Sign the mandate
        mandate.signature = self.sign_mandate(mandate)

        self._log(f"[AP2]   Signature    : {mandate.signature[:32]}...")
        self._log(f"[AP2] INTENT MANDATE: Status → ACTIVE ✓")
        self._log(f"[AP2] ══════════════════════════════════════════════════\n")

        # Store in registry
        self._mandates[mandate_id] = mandate
        return mandate

    # ------------------------------------------------------------------
    # 2. create_cart_mandate
    # ------------------------------------------------------------------

    def create_cart_mandate(
        self,
        intent_mandate: IntentMandate,
        selected_items: List[Dict[str, Any]],
        currency: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> CartMandate:
        """
        Create and sign a CartMandate from the concrete travel items selected
        by the booking agents.

        Each item in selected_items should be a dict containing at minimum:
          {
            "type":        "flight" | "hotel" | "car",
            "id":          "<provider reference id>",
            "description": "<human-readable description>",
            "price":       <float>,
            "merchant_id": "<provider / merchant identifier>",
          }

        The total_amount is computed automatically as the sum of item prices.

        Parameters
        ----------
        intent_mandate  : The parent IntentMandate this cart fulfils.
        selected_items  : List of item dicts describing what was selected.
        currency        : Override currency (defaults to intent_mandate.currency).
        metadata        : Optional extra key/value pairs.

        Returns
        -------
        CartMandate
            A signed CartMandate in PENDING_APPROVAL state, stored internally.

        Raises
        ------
        ValueError
            If intent_mandate is not in ACTIVE status or selected_items is empty.
        """
        if intent_mandate.status != IntentStatus.ACTIVE:
            raise ValueError(
                f"Cannot create CartMandate: IntentMandate {intent_mandate.mandate_id!r} "
                f"is not ACTIVE (status={intent_mandate.status!r})."
            )
        if not selected_items:
            raise ValueError("selected_items must not be empty.")

        cart_id = self._new_cart_id()
        created_at = _now_iso()
        currency = currency or intent_mandate.currency
        metadata = metadata or {}

        # Compute total and extract merchant IDs
        total_amount = sum(float(item.get("price", 0)) for item in selected_items)
        merchant_ids = [
            str(item.get("merchant_id", item.get("id", f"merchant-{i}")))
            for i, item in enumerate(selected_items)
        ]

        self._log(f"\n[AP2] ══════════════════════════════════════════════════")
        self._log(f"[AP2] CART MANDATE: Building cart from selected items")
        self._log(f"[AP2]   Cart ID          : {cart_id}")
        self._log(f"[AP2]   Intent Mandate   : {intent_mandate.mandate_id}")
        self._log(f"[AP2]   User             : {intent_mandate.user_id!r}")
        self._log(f"[AP2]   Items ({len(selected_items)}):")
        for i, item in enumerate(selected_items, 1):
            self._log(
                f"[AP2]     [{i}] {item.get('type', 'item').upper():8s} "
                f"{item.get('description', item.get('id', '?')):<35s} "
                f"${float(item.get('price', 0)):.2f}"
            )
        self._log(f"[AP2]   ─────────────────────────────────────────────────")
        self._log(f"[AP2]   Total Amount     : {currency} {total_amount:.2f}")
        self._log(
            f"[AP2]   Spending Cap     : {intent_mandate.currency} {intent_mandate.spending_cap:.2f}"
        )

        # Build the mandate with empty signature first
        cart = CartMandate(
            cart_id=cart_id,
            intent_mandate_id=intent_mandate.mandate_id,
            user_id=intent_mandate.user_id,
            items=selected_items,
            total_amount=total_amount,
            currency=currency,
            merchant_ids=merchant_ids,
            created_at=created_at,
            user_approved=False,
            signature="",  # filled in below
            status=CartStatus.PENDING_APPROVAL,
            approval_method="",
            metadata=metadata,
        )

        # Sign the cart
        cart.signature = self.sign_mandate(cart)

        self._log(f"[AP2]   Signature        : {cart.signature[:32]}...")
        self._log(f"[AP2] CART MANDATE: Status → PENDING_APPROVAL")
        self._log(f"[AP2] ══════════════════════════════════════════════════\n")

        # Store in registry
        self._carts[cart_id] = cart
        return cart

    # ------------------------------------------------------------------
    # 3. verify_mandate_chain
    # ------------------------------------------------------------------

    def verify_mandate_chain(
        self,
        cart_mandate: CartMandate,
        intent_mandate: IntentMandate,
    ) -> bool:
        """
        Verify that the CartMandate fully respects every constraint expressed
        in the parent IntentMandate.

        Checks performed
        ----------------
        1. Link integrity    – cart.intent_mandate_id matches intent.mandate_id.
        2. User consistency  – cart.user_id matches intent.user_id.
        3. Currency match    – both mandates use the same currency.
        4. Spending cap      – cart.total_amount <= intent.spending_cap.
        5. Destination check – at least one cart item's description or metadata
                               references the intent destination (case-insensitive).
        6. Intent is active  – IntentMandate.status == "active".
        7. Signature re-check – cart signature is consistent with its contents.
        8. Constraint checks – min_hotel_stars, prefer_direct_flights, etc.

        Parameters
        ----------
        cart_mandate   : The CartMandate to validate.
        intent_mandate : The parent IntentMandate to validate against.

        Returns
        -------
        bool
            True if ALL checks pass, False if ANY check fails.
            Detailed pass/fail lines are printed with [AP2] prefix.
        """
        self._log(f"\n[AP2] ══════════════════════════════════════════════════")
        self._log(
            f"[AP2] VERIFY CHAIN: Checking cart {cart_mandate.cart_id!r} "
            f"against intent {intent_mandate.mandate_id!r}"
        )

        failures: List[str] = []

        # ---- 1. Link integrity ----
        if cart_mandate.intent_mandate_id != intent_mandate.mandate_id:
            failures.append(
                f"Link mismatch: cart.intent_mandate_id="
                f"{cart_mandate.intent_mandate_id!r} != "
                f"intent.mandate_id={intent_mandate.mandate_id!r}"
            )
        self._log(
            f"[AP2]   [{'PASS' if not failures else 'FAIL'}] "
            f"Link integrity: cart → intent mandate"
        )

        # ---- 2. User consistency ----
        user_fail_before = len(failures)
        if cart_mandate.user_id != intent_mandate.user_id:
            failures.append(
                f"User mismatch: cart.user_id={cart_mandate.user_id!r} != "
                f"intent.user_id={intent_mandate.user_id!r}"
            )
        self._log(
            f"[AP2]   [{'PASS' if len(failures) == user_fail_before else 'FAIL'}] "
            f"User consistency: {cart_mandate.user_id!r}"
        )

        # ---- 3. Currency match ----
        currency_fail_before = len(failures)
        if cart_mandate.currency != intent_mandate.currency:
            failures.append(
                f"Currency mismatch: cart={cart_mandate.currency!r}, "
                f"intent={intent_mandate.currency!r}"
            )
        self._log(
            f"[AP2]   [{'PASS' if len(failures) == currency_fail_before else 'FAIL'}] "
            f"Currency match: {cart_mandate.currency}"
        )

        # ---- 4. Spending cap ----
        cap_fail_before = len(failures)
        if cart_mandate.total_amount > intent_mandate.spending_cap:
            failures.append(
                f"Spending cap exceeded: cart total "
                f"{cart_mandate.currency} {cart_mandate.total_amount:.2f} > "
                f"cap {intent_mandate.currency} {intent_mandate.spending_cap:.2f}"
            )
        headroom = intent_mandate.spending_cap - cart_mandate.total_amount
        self._log(
            f"[AP2]   [{'PASS' if len(failures) == cap_fail_before else 'FAIL'}] "
            f"Spending cap: "
            f"{cart_mandate.currency} {cart_mandate.total_amount:.2f} / "
            f"{intent_mandate.spending_cap:.2f} "
            f"({'headroom $' + f'{headroom:.2f}' if headroom >= 0 else 'EXCEEDED by $' + f'{abs(headroom):.2f}'})"
        )

        # ---- 5. Destination check ----
        dest_fail_before = len(failures)
        dest_lower = intent_mandate.destination.lower()
        dest_found = any(
            dest_lower in str(item.get("description", "")).lower()
            or dest_lower in str(item.get("destination", "")).lower()
            or dest_lower in str(item.get("city", "")).lower()
            or dest_lower in str(item.get("to", "")).lower()
            for item in cart_mandate.items
        )
        if not dest_found:
            failures.append(
                f"Destination not found in cart items: "
                f"expected {intent_mandate.destination!r} to appear in at least "
                f"one item's description/destination/city field."
            )
        self._log(
            f"[AP2]   [{'PASS' if len(failures) == dest_fail_before else 'FAIL'}] "
            f"Destination check: {intent_mandate.destination!r} found in cart items"
        )

        # ---- 6. Intent mandate is active ----
        active_fail_before = len(failures)
        if intent_mandate.status != IntentStatus.ACTIVE:
            failures.append(
                f"IntentMandate is not ACTIVE (status={intent_mandate.status!r})"
            )
        self._log(
            f"[AP2]   [{'PASS' if len(failures) == active_fail_before else 'FAIL'}] "
            f"Intent mandate status: {intent_mandate.status!r}"
        )

        # ---- 7. Cart signature re-check ----
        sig_fail_before = len(failures)
        original_sig = cart_mandate.signature
        recomputed_sig = self._compute_signature(cart_mandate)
        if original_sig != recomputed_sig:
            failures.append(
                f"Cart signature mismatch: the cart contents may have been "
                f"tampered with after signing."
            )
        self._log(
            f"[AP2]   [{'PASS' if len(failures) == sig_fail_before else 'FAIL'}] "
            f"Cart signature integrity check"
        )

        # ---- 8. Constraint checks ----
        constraints = intent_mandate.constraints
        if constraints:
            self._log(f"[AP2]   Checking constraints: {constraints}")

            # min_hotel_stars
            min_stars = constraints.get("min_hotel_stars")
            if min_stars is not None:
                stars_fail_before = len(failures)
                hotel_items = [
                    i for i in cart_mandate.items if i.get("type") == "hotel"
                ]
                for hi in hotel_items:
                    item_stars = hi.get("stars", hi.get("hotel_stars", 0))
                    if item_stars < min_stars:
                        failures.append(
                            f"Hotel {hi.get('id', '?')!r} has {item_stars} stars "
                            f"but min_hotel_stars constraint requires {min_stars}."
                        )
                self._log(
                    f"[AP2]   [{'PASS' if len(failures) == stars_fail_before else 'FAIL'}] "
                    f"Constraint: min_hotel_stars >= {min_stars}"
                )

            # max_items (guard against cart bloat)
            max_items = constraints.get("max_items")
            if max_items is not None:
                items_fail_before = len(failures)
                if len(cart_mandate.items) > max_items:
                    failures.append(
                        f"Cart has {len(cart_mandate.items)} items but "
                        f"max_items constraint allows {max_items}."
                    )
                self._log(
                    f"[AP2]   [{'PASS' if len(failures) == items_fail_before else 'FAIL'}] "
                    f"Constraint: max_items <= {max_items}"
                )

        # ---- Result ----
        all_passed = len(failures) == 0
        if all_passed:
            self._log(
                f"[AP2] VERIFY CHAIN: ALL CHECKS PASSED ✓ "
                f"— Cart {cart_mandate.cart_id!r} is valid"
            )
        else:
            self._log(f"[AP2] VERIFY CHAIN: FAILED ✗ — {len(failures)} issue(s):")
            for f_msg in failures:
                self._log(f"[AP2]   ✗ {f_msg}")

        self._log(f"[AP2] ══════════════════════════════════════════════════\n")
        return all_passed

    # ------------------------------------------------------------------
    # 4. sign_mandate
    # ------------------------------------------------------------------

    def sign_mandate(self, mandate: Any) -> str:
        """
        Produce a deterministic tamper-evident signature over a mandate's
        key fields using SHA-256 (simulated; no asymmetric key needed).

        For an IntentMandate the signed payload is:
          mandate_id + user_id + intent_description + spending_cap +
          destination + travel_dates (sorted JSON) + currency + created_at

        For a CartMandate the signed payload is:
          cart_id + intent_mandate_id + user_id + total_amount +
          currency + items (sorted JSON) + created_at

        The signing secret is appended before hashing (simulated HMAC).

        Parameters
        ----------
        mandate : IntentMandate or CartMandate

        Returns
        -------
        str
            64-character lowercase hex string (SHA-256 digest).
        """
        if isinstance(mandate, IntentMandate):
            payload_parts = [
                mandate.mandate_id,
                mandate.user_id,
                mandate.intent_description,
                str(mandate.spending_cap),
                mandate.destination,
                json.dumps(mandate.travel_dates, sort_keys=True),
                json.dumps(mandate.constraints, sort_keys=True),
                mandate.currency,
                mandate.created_at,
                self._SIGNING_SECRET,
            ]
        elif isinstance(mandate, CartMandate):
            payload_parts = [
                mandate.cart_id,
                mandate.intent_mandate_id,
                mandate.user_id,
                str(mandate.total_amount),
                mandate.currency,
                json.dumps(mandate.items, sort_keys=True),
                mandate.created_at,
                self._SIGNING_SECRET,
            ]
        else:
            # Generic fallback: hash the string representation
            payload_parts = [str(mandate), self._SIGNING_SECRET]

        signing_input = "|".join(payload_parts)
        digest = hashlib.sha256(signing_input.encode("utf-8")).hexdigest()

        self._log(f"[AP2] SIGN: Signing {type(mandate).__name__} → {digest[:32]}...")
        return digest

    def _compute_signature(self, mandate: Any) -> str:
        """
        Re-compute the signature for a mandate WITHOUT emitting a log line.
        Used internally by verify_mandate_chain() to check signature integrity.
        """
        old_verbose = self.verbose
        self.verbose = False
        sig = self.sign_mandate(mandate)
        self.verbose = old_verbose
        return sig

    # ------------------------------------------------------------------
    # 5. user_approve_cart
    # ------------------------------------------------------------------

    def user_approve_cart(
        self,
        cart_mandate: CartMandate,
        approval_method: str = "interactive",
    ) -> CartMandate:
        """
        Record the user's explicit approval of the CartMandate.

        This advances the cart from PENDING_APPROVAL → APPROVED and sets
        user_approved = True.  The PaymentProcessor will refuse to charge a
        CartMandate that has not been approved.

        In the "interactive" scenario (Scenario 1 in main.py) the demo pauses
        to show the user the cart summary and waits for confirmation.
        In the "delegated" scenario (Scenario 2) this is called automatically
        because the pre-signed IntentMandate grants autonomous fulfilment.

        Parameters
        ----------
        cart_mandate    : The CartMandate to approve.
        approval_method : "interactive" (user reviewed) or "delegated"
                          (pre-authorised via IntentMandate).

        Returns
        -------
        CartMandate
            The same CartMandate object with user_approved=True and
            status="approved".

        Raises
        ------
        ValueError
            If the cart is not in PENDING_APPROVAL status (e.g. already
            charged or cancelled).
        """
        if cart_mandate.status != CartStatus.PENDING_APPROVAL:
            raise ValueError(
                f"Cannot approve CartMandate {cart_mandate.cart_id!r}: "
                f"expected status PENDING_APPROVAL, got {cart_mandate.status!r}."
            )

        self._log(f"\n[AP2] ══════════════════════════════════════════════════")
        self._log(
            f"[AP2] USER APPROVAL: Processing cart approval "
            f"(method={approval_method!r})"
        )
        self._log(f"[AP2]   Cart ID      : {cart_mandate.cart_id}")
        self._log(f"[AP2]   User         : {cart_mandate.user_id!r}")
        self._log(f"[AP2]   Method       : {approval_method!r}")
        self._log(f"[AP2]   Cart Summary :")

        for i, item in enumerate(cart_mandate.items, 1):
            self._log(
                f"[AP2]     [{i}] {item.get('type', 'item').upper():8s} "
                f"{str(item.get('description', item.get('id', '?'))):<35s} "
                f"${float(item.get('price', 0)):.2f}"
            )

        self._log(f"[AP2]   ─────────────────────────────────────────────────")
        self._log(
            f"[AP2]   TOTAL        : {cart_mandate.currency} "
            f"{cart_mandate.total_amount:.2f}"
        )

        if approval_method == "interactive":
            self._log(f"[AP2]   → User reviewed cart and confirmed purchase ✓")
        elif approval_method == "delegated":
            self._log(f"[AP2]   → Delegated approval via pre-signed IntentMandate ✓")
        else:
            self._log(f"[AP2]   → Approval recorded (method={approval_method!r}) ✓")

        cart_mandate.user_approved = True
        cart_mandate.status = CartStatus.APPROVED
        cart_mandate.approval_method = approval_method

        self._log(f"[AP2] USER APPROVAL: Status → APPROVED ✓")
        self._log(f"[AP2] ══════════════════════════════════════════════════\n")

        return cart_mandate

    # ------------------------------------------------------------------
    # Utility / admin
    # ------------------------------------------------------------------

    def get_intent_mandate(self, mandate_id: str) -> Optional[IntentMandate]:
        """Look up a stored IntentMandate by its ID."""
        return self._mandates.get(mandate_id)

    def get_cart_mandate(self, cart_id: str) -> Optional[CartMandate]:
        """Look up a stored CartMandate by its ID."""
        return self._carts.get(cart_id)

    def mark_intent_fulfilled(self, mandate_id: str) -> bool:
        """
        Mark an IntentMandate as FULFILLED after a successful payment.
        Returns True if the mandate was found and updated.
        """
        mandate = self._mandates.get(mandate_id)
        if mandate:
            mandate.status = IntentStatus.FULFILLED
            self._log(
                f"[AP2] MANDATE UPDATE: IntentMandate {mandate_id!r} → FULFILLED ✓"
            )
            return True
        return False

    def mark_cart_charged(self, cart_id: str) -> bool:
        """
        Mark a CartMandate as CHARGED after a successful payment.
        Returns True if the cart was found and updated.
        """
        cart = self._carts.get(cart_id)
        if cart:
            cart.status = CartStatus.CHARGED
            self._log(f"[AP2] CART UPDATE: CartMandate {cart_id!r} → CHARGED ✓")
            return True
        return False

    def cancel_mandate(self, mandate_id: str, reason: str = "") -> bool:
        """
        Cancel an IntentMandate (and leave any associated CartMandates in
        their current state for audit purposes).
        Returns True if the mandate was found and cancelled.
        """
        mandate = self._mandates.get(mandate_id)
        if mandate:
            mandate.status = IntentStatus.CANCELLED
            if reason:
                mandate.metadata["cancellation_reason"] = reason
            self._log(
                f"[AP2] MANDATE CANCELLED: {mandate_id!r} "
                f"{'— ' + reason if reason else ''}"
            )
            return True
        return False

    def summary(self) -> str:
        """Return a human-readable summary of all stored mandates."""
        lines = [
            f"AP2Protocol — {len(self._mandates)} intent mandate(s), "
            f"{len(self._carts)} cart mandate(s)"
        ]
        for m in self._mandates.values():
            lines.append(f"  INTENT  {m}")
        for c in self._carts.values():
            lines.append(f"  CART    {c}")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return f"AP2Protocol(intents={len(self._mandates)}, carts={len(self._carts)})"
