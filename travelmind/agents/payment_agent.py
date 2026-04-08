"""
agents/payment_agent.py
=======================
PaymentAgent — A2A Server + AP2 Client Bridge Agent
----------------------------------------------------
The PaymentAgent is the financial gateway of the TravelMind system.  It sits
at the intersection of the A2A and AP2 protocols:

  * As an A2A server  : it receives payment-related tasks from the
                        OrchestratorAgent via the standard A2A task dispatch
                        mechanism (discovery → auth → process_task).

  * As an AP2 client  : it orchestrates the full AP2 mandate workflow —
                        mandate verification, chain validation, user approval,
                        and secure payment processing — on behalf of the caller.

Capabilities
------------
  process_payment  : The primary task.  Receives an already-created
                     CartMandate (and optionally its parent IntentMandate)
                     from the orchestrator, runs the full AP2 payment flow,
                     and returns a transaction receipt.

  create_mandate   : Utility task.  Creates a fresh IntentMandate from a
                     plain-dict description of the user's travel intent.
                     Useful when the orchestrator wants to delegate the entire
                     mandate creation step to the payment agent.

  verify_mandate   : Utility task.  Runs AP2Protocol.verify_mandate_chain()
                     on a supplied cart + intent pair and returns the boolean
                     result plus a detailed verdict message.

AP2 Workflow inside process_payment
-------------------------------------
  1. Receive CartMandate (and optionally IntentMandate) from the task payload.
  2. Reconstruct Python dataclass instances from the dict payload.
  3. Run verify_mandate_chain() — reject immediately if it fails.
  4. Simulate user approval (interactive or delegated based on the
     approval_method field in the payload).
  5. Call PaymentProcessor.process_payment() to simulate the charge.
  6. Mark the IntentMandate as FULFILLED (if provided).
  7. Return a rich transaction receipt back to the orchestrator via A2AResponse.

All steps emit [AP2] and [PAYMENT AGENT] prefixed log lines for traceability.

No external dependencies — stdlib only.
"""

from __future__ import annotations

import hashlib
import json
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional

from a2a.a2a_protocol import A2AResponse, A2ATask, get_registry
from a2a.agent_card import AgentCard
from a2a.auth import verify_token
from ap2.ap2_protocol import AP2Protocol
from ap2.mandate import CartMandate, CartStatus, IntentMandate, IntentStatus
from ap2.payment_processor import PaymentProcessor

# ---------------------------------------------------------------------------
# PaymentAgent
# ---------------------------------------------------------------------------


class PaymentAgent:
    """
    A2A server agent that bridges incoming payment task messages to the full
    AP2 mandate-and-payment workflow.

    The agent owns its own AP2Protocol and PaymentProcessor instances so it
    can independently manage and audit all mandates it processes.  Mandate
    objects are reconstructed from the dict payloads supplied in A2A tasks —
    this keeps the A2A wire format JSON-serialisable while still benefiting
    from the full AP2 dataclass logic.

    Parameters
    ----------
    agent_id : str
        Unique identifier for this agent instance.
        Defaults to "payment-agent-01".
    verbose : bool
        When True (default) the agent prints detailed [PAYMENT AGENT] and
        [AP2] diagnostic lines at every step.
    auto_register : bool
        When True (default) the agent registers itself in the shared global
        A2ARegistry on instantiation so the orchestrator can discover it.
    """

    # ----------------------------------------------------------------
    # Class-level constants
    # ----------------------------------------------------------------

    AGENT_ID: str = "payment-agent-01"
    AGENT_NAME: str = "PaymentAgent"
    AGENT_DESCRIPTION: str = (
        "Specialist agent that handles AP2 mandate verification and secure "
        "payment processing for travel bookings.  Accepts process_payment, "
        "create_mandate, and verify_mandate tasks via A2A."
    )
    CAPABILITIES: List[str] = ["process_payment", "create_mandate", "verify_mandate"]
    SUPPORTED_TASKS: List[str] = ["process_payment", "create_mandate", "verify_mandate"]
    ENDPOINT: str = "https://agents.travelmind.internal/payment/v1"

    # ----------------------------------------------------------------
    # Initialisation
    # ----------------------------------------------------------------

    def __init__(
        self,
        agent_id: str = AGENT_ID,
        verbose: bool = True,
        auto_register: bool = True,
    ) -> None:
        self.agent_id = agent_id
        self.verbose = verbose

        # Own AP2 protocol instance — manages mandate lifecycle
        self.ap2 = AP2Protocol(verbose=verbose)

        # Own payment processor — manages transaction audit log
        self.processor = PaymentProcessor(verbose=verbose)

        # Build AgentCard — embed 'instance' reference so A2AClient can
        # call process_task() without importing this module directly.
        self.agent_card = AgentCard(
            agent_id=self.agent_id,
            name=self.AGENT_NAME,
            description=self.AGENT_DESCRIPTION,
            capabilities=list(self.CAPABILITIES),
            endpoint=self.ENDPOINT,
            supported_tasks=list(self.SUPPORTED_TASKS),
            auth_required=True,
            version="1.0.0",
            metadata={"instance": self},
        )

        if auto_register:
            self._register()

    # ----------------------------------------------------------------
    # Private helpers
    # ----------------------------------------------------------------

    def _log(self, message: str) -> None:
        if self.verbose:
            print(message)

    def _register(self) -> None:
        """Register this agent in the shared A2A discovery registry."""
        registry = get_registry()
        registry.register(self.agent_card)
        self._log(
            f"[PAYMENT AGENT] Registered {self.AGENT_NAME!r} in A2ARegistry "
            f"with capabilities: {self.CAPABILITIES}"
        )

    def _validate_auth(self, task: A2ATask) -> bool:
        """
        Verify the auth token carried by the incoming task.

        Returns True if the token is valid (or auth is disabled).
        The token is verified against the sender's own agent_id because the
        A2AClient scopes tokens to the caller's identity, not the receiver's.
        """
        if not self.agent_card.auth_required:
            return True
        return verify_token(task.auth_token, task.sender_id)

    # ----------------------------------------------------------------
    # Mandate reconstruction from dict payloads
    # ----------------------------------------------------------------

    def _dict_to_intent_mandate(self, data: Dict[str, Any]) -> IntentMandate:
        """
        Reconstruct an IntentMandate dataclass instance from a plain dict.

        When the OrchestratorAgent sends a CartMandate to the PaymentAgent
        via A2A, the mandate objects are serialised to dicts (JSON-safe).
        This method re-hydrates the dict back into the typed dataclass so the
        AP2 protocol methods can operate on it normally.

        Parameters
        ----------
        data : dict
            A dict previously produced by IntentMandate.to_dict() or
            constructed directly by the orchestrator.

        Returns
        -------
        IntentMandate

        Raises
        ------
        ValueError  If required fields are missing from the dict.
        """
        required = [
            "mandate_id",
            "user_id",
            "intent_description",
            "spending_cap",
            "destination",
            "travel_dates",
            "constraints",
            "created_at",
            "signature",
        ]
        missing = [f for f in required if f not in data]
        if missing:
            raise ValueError(
                f"Cannot reconstruct IntentMandate: missing fields {missing}. "
                f"Provided keys: {list(data.keys())}"
            )

        # Handle the truncated signature that to_dict() produces ("abc...").
        # If the orchestrator sends the full signature we use it; if truncated
        # we store it as-is (verification will still work if the full sig was
        # stored separately and re-validated before reaching us).
        signature = data.get("signature", "")
        if signature.endswith("..."):
            # Truncated for display; treat as a pre-verified placeholder.
            # In production you'd require the full signature here.
            signature = data.get("_full_signature", signature)

        return IntentMandate(
            mandate_id=data["mandate_id"],
            user_id=data["user_id"],
            intent_description=data["intent_description"],
            spending_cap=float(data["spending_cap"]),
            destination=data["destination"],
            travel_dates=data["travel_dates"],
            constraints=data.get("constraints", {}),
            currency=data.get("currency", "USD"),
            created_at=data["created_at"],
            expires_at=data.get("expires_at"),
            signature=signature,
            status=data.get("status", IntentStatus.ACTIVE),
            metadata=data.get("metadata", {}),
        )

    def _dict_to_cart_mandate(self, data: Dict[str, Any]) -> CartMandate:
        """
        Reconstruct a CartMandate dataclass instance from a plain dict.

        Parameters
        ----------
        data : dict
            A dict previously produced by CartMandate.to_dict() or
            constructed directly by the orchestrator.

        Returns
        -------
        CartMandate

        Raises
        ------
        ValueError  If required fields are missing from the dict.
        """
        required = [
            "cart_id",
            "intent_mandate_id",
            "user_id",
            "items",
            "total_amount",
            "currency",
            "merchant_ids",
            "created_at",
            "user_approved",
            "signature",
        ]
        missing = [f for f in required if f not in data]
        if missing:
            raise ValueError(
                f"Cannot reconstruct CartMandate: missing fields {missing}. "
                f"Provided keys: {list(data.keys())}"
            )

        # Same truncated-signature handling as above
        signature = data.get("signature", "")
        if signature.endswith("..."):
            signature = data.get("_full_signature", signature)

        return CartMandate(
            cart_id=data["cart_id"],
            intent_mandate_id=data["intent_mandate_id"],
            user_id=data["user_id"],
            items=list(data["items"]),
            total_amount=float(data["total_amount"]),
            currency=data.get("currency", "USD"),
            merchant_ids=list(data["merchant_ids"]),
            created_at=data["created_at"],
            user_approved=bool(data.get("user_approved", False)),
            signature=signature,
            status=data.get("status", CartStatus.PENDING_APPROVAL),
            approval_method=data.get("approval_method", ""),
            metadata=data.get("metadata", {}),
        )

    # ----------------------------------------------------------------
    # Task dispatcher  (A2A entry point)
    # ----------------------------------------------------------------

    def process_task(self, task: A2ATask) -> A2AResponse:
        """
        Main A2A entry point — receives a task dispatched by A2AClient and
        routes it to the appropriate handler method.

        Supported intents
        -----------------
        "process_payment"  -> _handle_process_payment(task)
        "create_mandate"   -> _handle_create_mandate(task)
        "verify_mandate"   -> _handle_verify_mandate(task)

        Any unrecognised intent returns an error response.

        Parameters
        ----------
        task : A2ATask
            The incoming task from the orchestrator (or any A2A caller).

        Returns
        -------
        A2AResponse
            Structured response with status "success" or "error" and a
            result payload appropriate to the intent.
        """
        self._log(
            f"\n[PAYMENT AGENT] ══ Received task {task.task_id!r} "
            f"from {task.sender_id!r} | intent={task.intent!r}"
        )

        # Auth check — all payment operations require a valid token
        if not self._validate_auth(task):
            self._log(f"[PAYMENT AGENT] Auth FAILED for task {task.task_id!r}")
            return A2AResponse.error(
                task_id=task.task_id,
                agent_id=self.agent_id,
                message=(
                    f"Authentication failed: invalid or expired token "
                    f"from sender {task.sender_id!r}. "
                    f"All payment operations require a valid A2A bearer token."
                ),
            )

        # Intent dispatch
        if task.intent == "process_payment":
            return self._handle_process_payment(task)
        elif task.intent == "create_mandate":
            return self._handle_create_mandate(task)
        elif task.intent == "verify_mandate":
            return self._handle_verify_mandate(task)
        else:
            return A2AResponse.error(
                task_id=task.task_id,
                agent_id=self.agent_id,
                message=(
                    f"PaymentAgent does not handle intent {task.intent!r}. "
                    f"Supported intents: {self.SUPPORTED_TASKS}"
                ),
            )

    # ----------------------------------------------------------------
    # Intent handlers
    # ----------------------------------------------------------------

    def _handle_process_payment(self, task: A2ATask) -> A2AResponse:
        """
        Execute the full AP2 payment workflow for a travel cart.

        This is the primary handler and the most complex one.  It performs
        the following steps in sequence:

          Step 1  — Reconstruct the CartMandate from the task payload.
          Step 2  — Reconstruct the IntentMandate (if provided) from the payload.
          Step 3  — Register both mandates in this agent's local AP2Protocol
                    instance so verify_mandate_chain() can access them.
          Step 4  — Run AP2Protocol.verify_mandate_chain() to confirm the cart
                    respects the intent's spending cap, destination, and constraints.
                    Abort with an error response if the chain check fails.
          Step 5  — Apply user approval via AP2Protocol.user_approve_cart(),
                    using the approval_method specified in the payload
                    ("interactive" or "delegated").
          Step 6  — Call PaymentProcessor.process_payment() to simulate the charge.
          Step 7  — Mark the IntentMandate as FULFILLED (if provided).
          Step 8  — Return the full transaction receipt to the orchestrator.

        Expected payload keys
        ---------------------
        cart_mandate    : dict   Serialised CartMandate (from CartMandate.to_dict()
                                 or constructed directly; MUST include _full_signature).
        intent_mandate  : dict   Optional serialised IntentMandate.  When omitted,
                                 verify_mandate_chain() is skipped (useful for
                                 testing payment in isolation).
        approval_method : str    "interactive" (default) or "delegated".
                                 Controls which log message AP2Protocol emits
                                 and sets CartMandate.approval_method.
        force_approve   : bool   When True, skip the user-approval step (cart is
                                 treated as already approved).  Useful when the
                                 orchestrator applied approval before sending.

        Returns
        -------
        A2AResponse with result dict:
          {
            "success":          bool,
            "transaction_id":   str,
            "receipt_id":       str,
            "cart_id":          str,
            "user_id":          str,
            "amount":           float,
            "currency":         str,
            "items_charged":    list[dict],
            "charged_at":       str,
            "approval_method":  str,
            "mandate_chain_verified": bool,
            "receipt":          dict | None,
            "error":            str | None,
          }
        """
        payload = task.payload
        self._log(
            f"\n[PAYMENT AGENT] ── Step 1/7: Reconstructing mandates from payload"
        )

        # ------------------------------------------------------------------
        # Step 1 – Reconstruct CartMandate
        # ------------------------------------------------------------------
        cart_data = payload.get("cart_mandate")
        if not cart_data:
            return A2AResponse.error(
                task_id=task.task_id,
                agent_id=self.agent_id,
                message=(
                    "process_payment payload must include a 'cart_mandate' dict. "
                    "Use CartMandate.to_dict() to serialise it before sending."
                ),
            )

        try:
            cart = self._dict_to_cart_mandate(cart_data)
        except (ValueError, KeyError, TypeError) as exc:
            return A2AResponse.error(
                task_id=task.task_id,
                agent_id=self.agent_id,
                message=f"Failed to reconstruct CartMandate: {exc}",
            )

        self._log(f"[PAYMENT AGENT]   CartMandate reconstructed: {cart.cart_id}")

        # ------------------------------------------------------------------
        # Step 2 – Reconstruct IntentMandate (optional)
        # ------------------------------------------------------------------
        intent_data = payload.get("intent_mandate")
        intent: Optional[IntentMandate] = None

        if intent_data:
            try:
                intent = self._dict_to_intent_mandate(intent_data)
                self._log(
                    f"[PAYMENT AGENT]   IntentMandate reconstructed: {intent.mandate_id}"
                )
            except (ValueError, KeyError, TypeError) as exc:
                return A2AResponse.error(
                    task_id=task.task_id,
                    agent_id=self.agent_id,
                    message=f"Failed to reconstruct IntentMandate: {exc}",
                )
        else:
            self._log(
                f"[PAYMENT AGENT]   No IntentMandate supplied — "
                f"mandate chain verification will be skipped."
            )

        # ------------------------------------------------------------------
        # Step 3 – Register mandates in local AP2Protocol
        # ------------------------------------------------------------------
        self._log(
            f"[PAYMENT AGENT] ── Step 2/7: Registering mandates in local AP2Protocol"
        )
        if intent is not None:
            self.ap2._mandates[intent.mandate_id] = intent
        self.ap2._carts[cart.cart_id] = cart

        # ------------------------------------------------------------------
        # Step 4 – Verify mandate chain
        # ------------------------------------------------------------------
        self._log(f"[PAYMENT AGENT] ── Step 3/7: Verifying AP2 mandate chain")
        chain_verified = False

        if intent is not None:
            chain_verified = self.ap2.verify_mandate_chain(cart, intent)
            if not chain_verified:
                return A2AResponse.error(
                    task_id=task.task_id,
                    agent_id=self.agent_id,
                    message=(
                        f"AP2 mandate chain verification FAILED for cart "
                        f"{cart.cart_id!r}. The cart does not satisfy the "
                        f"constraints of IntentMandate {intent.mandate_id!r}. "
                        f"Payment refused."
                    ),
                    result={
                        "success": False,
                        "cart_id": cart.cart_id,
                        "mandate_chain_verified": False,
                        "error": "Mandate chain verification failed.",
                        "transaction_id": None,
                        "receipt": None,
                    },
                )
            self._log(
                f"[PAYMENT AGENT]   Mandate chain verified ✓ "
                f"(${cart.total_amount:.2f} <= cap ${intent.spending_cap:.2f})"
            )
        else:
            # No intent mandate supplied — skip chain check but warn clearly
            chain_verified = True  # vacuously true; no constraints to check
            self._log(
                f"[PAYMENT AGENT]   WARNING: No IntentMandate provided — "
                f"mandate chain verification SKIPPED. "
                f"Proceeding with cart-only payment."
            )

        # ------------------------------------------------------------------
        # Step 5 – Apply user approval
        # ------------------------------------------------------------------
        self._log(f"[PAYMENT AGENT] ── Step 4/7: Applying user approval")
        approval_method: str = str(payload.get("approval_method", "interactive"))
        force_approve: bool = bool(payload.get("force_approve", False))

        if force_approve or cart.user_approved:
            # Cart was already approved by the orchestrator before being sent
            if not cart.user_approved:
                cart.user_approved = True
                cart.status = CartStatus.APPROVED
                cart.approval_method = approval_method
            self._log(
                f"[PAYMENT AGENT]   Cart is pre-approved "
                f"(force_approve={force_approve}, "
                f"user_approved={cart.user_approved}) ✓"
            )
        else:
            # Apply approval through the AP2 protocol (emits AP2 log lines)
            try:
                cart = self.ap2.user_approve_cart(cart, approval_method=approval_method)
            except ValueError as exc:
                return A2AResponse.error(
                    task_id=task.task_id,
                    agent_id=self.agent_id,
                    message=f"Cannot approve cart: {exc}",
                    result={
                        "success": False,
                        "cart_id": cart.cart_id,
                        "mandate_chain_verified": chain_verified,
                        "error": str(exc),
                        "transaction_id": None,
                        "receipt": None,
                    },
                )

        # ------------------------------------------------------------------
        # Step 6 – Process payment
        # ------------------------------------------------------------------
        self._log(f"[PAYMENT AGENT] ── Step 5/7: Invoking PaymentProcessor")
        receipt_result = self.processor.process_payment(
            cart_mandate=cart,
            intent_mandate=intent,
        )

        # ------------------------------------------------------------------
        # Step 7 – Post-payment mandate housekeeping
        # ------------------------------------------------------------------
        self._log(f"[PAYMENT AGENT] ── Step 6/7: Post-payment mandate housekeeping")
        if receipt_result.get("success") and intent is not None:
            self.ap2.mark_intent_fulfilled(intent.mandate_id)
            self.ap2.mark_cart_charged(cart.cart_id)

        # ------------------------------------------------------------------
        # Step 8 – Build and return the response
        # ------------------------------------------------------------------
        self._log(f"[PAYMENT AGENT] ── Step 7/7: Building A2A response")
        success = receipt_result.get("success", False)
        status_icon = "✓" if success else "✗"

        self._log(f"\n[PAYMENT AGENT] Payment processing complete {status_icon}")
        self._log(
            f"[PAYMENT AGENT]   TXN ID         : {receipt_result.get('transaction_id', 'N/A')}"
        )
        self._log(
            f"[PAYMENT AGENT]   Amount Charged : "
            f"{receipt_result.get('currency', 'USD')} "
            f"{float(receipt_result.get('amount', 0)):.2f}"
        )
        self._log(f"[PAYMENT AGENT]   Chain Verified : {chain_verified}")

        result = {
            **receipt_result,
            "mandate_chain_verified": chain_verified,
            "intent_mandate_id": intent.mandate_id if intent else None,
        }

        if success:
            return A2AResponse.success(
                task_id=task.task_id,
                agent_id=self.agent_id,
                result=result,
                message=(
                    f"Payment successful! "
                    f"Transaction {receipt_result.get('transaction_id')}: "
                    f"{receipt_result.get('currency', 'USD')} "
                    f"{float(receipt_result.get('amount', 0)):.2f} charged. "
                    f"Receipt: {receipt_result.get('receipt_id', 'N/A')}."
                ),
            )
        else:
            return A2AResponse.error(
                task_id=task.task_id,
                agent_id=self.agent_id,
                result=result,
                message=(
                    f"Payment FAILED for cart {cart.cart_id!r}: "
                    f"{receipt_result.get('error', 'Unknown error')}."
                ),
            )

    # ----------------------------------------------------------------

    def _handle_create_mandate(self, task: A2ATask) -> A2AResponse:
        """
        Create a fresh IntentMandate from the task payload.

        This utility task lets the orchestrator delegate mandate creation
        entirely to the PaymentAgent.  It is an alternative to the
        orchestrator calling AP2Protocol directly before sending tasks to
        specialist agents.

        Expected payload keys
        ---------------------
        user_id            : str    (required)
        intent_description : str    (required)
        spending_cap       : float  (required)
        destination        : str    (required)
        travel_dates       : dict   (required) e.g. {"outbound": "2025-09-15"}
        constraints        : dict   optional   e.g. {"min_hotel_stars": 3}
        currency           : str    optional   defaults to "USD"
        expires_at         : str    optional   ISO-8601 expiry datetime

        Returns
        -------
        A2AResponse with result dict:
          {
            "mandate":         dict   (IntentMandate.to_dict())
            "_full_signature": str    (the full un-truncated signature)
          }
        """
        payload = task.payload
        self._log(
            f"[PAYMENT AGENT] ── create_mandate: Building IntentMandate from payload"
        )

        required_keys = [
            "user_id",
            "intent_description",
            "spending_cap",
            "destination",
            "travel_dates",
        ]
        missing = [k for k in required_keys if k not in payload]
        if missing:
            return A2AResponse.error(
                task_id=task.task_id,
                agent_id=self.agent_id,
                message=(
                    f"create_mandate payload is missing required keys: {missing}. "
                    f"Required: {required_keys}"
                ),
            )

        try:
            mandate = self.ap2.create_intent_mandate(
                user_id=str(payload["user_id"]),
                intent_description=str(payload["intent_description"]),
                spending_cap=float(payload["spending_cap"]),
                destination=str(payload["destination"]),
                travel_dates=dict(payload["travel_dates"]),
                constraints=dict(payload.get("constraints", {})),
                currency=str(payload.get("currency", "USD")),
                expires_at=payload.get("expires_at"),
                metadata=dict(payload.get("metadata", {})),
            )
        except (ValueError, TypeError) as exc:
            return A2AResponse.error(
                task_id=task.task_id,
                agent_id=self.agent_id,
                message=f"Failed to create IntentMandate: {exc}",
            )

        mandate_dict = mandate.to_dict()

        return A2AResponse.success(
            task_id=task.task_id,
            agent_id=self.agent_id,
            result={
                "mandate": mandate_dict,
                "_full_signature": mandate.signature,
            },
            message=(
                f"IntentMandate {mandate.mandate_id!r} created successfully. "
                f"Spending cap: {mandate.currency} {mandate.spending_cap:.2f} "
                f"for {mandate.destination!r}."
            ),
        )

    # ----------------------------------------------------------------

    def _handle_verify_mandate(self, task: A2ATask) -> A2AResponse:
        """
        Verify that a CartMandate respects its parent IntentMandate.

        This utility task wraps AP2Protocol.verify_mandate_chain() and
        returns the boolean result along with a human-readable verdict.
        It is useful for the orchestrator to perform a pre-flight check
        before sending the cart to be charged.

        Expected payload keys
        ---------------------
        cart_mandate   : dict   Serialised CartMandate (required).
        intent_mandate : dict   Serialised IntentMandate (required).

        Returns
        -------
        A2AResponse with result dict:
          {
            "chain_valid":       bool,
            "cart_id":           str,
            "intent_mandate_id": str,
            "total_amount":      float,
            "spending_cap":      float,
            "headroom":          float,   # spending_cap - total_amount
            "verdict":           str      # human-readable summary
          }
        """
        payload = task.payload
        self._log(f"[PAYMENT AGENT] ── verify_mandate: Running mandate chain check")

        cart_data = payload.get("cart_mandate")
        intent_data = payload.get("intent_mandate")

        if not cart_data or not intent_data:
            return A2AResponse.error(
                task_id=task.task_id,
                agent_id=self.agent_id,
                message=(
                    "verify_mandate payload must include both 'cart_mandate' "
                    "and 'intent_mandate' dicts."
                ),
            )

        try:
            cart = self._dict_to_cart_mandate(cart_data)
            intent = self._dict_to_intent_mandate(intent_data)
        except (ValueError, KeyError, TypeError) as exc:
            return A2AResponse.error(
                task_id=task.task_id,
                agent_id=self.agent_id,
                message=f"Failed to reconstruct mandates for verification: {exc}",
            )

        chain_valid = self.ap2.verify_mandate_chain(cart, intent)
        headroom = intent.spending_cap - cart.total_amount

        if chain_valid:
            verdict = (
                f"VALID — Cart {cart.cart_id!r} satisfies all constraints "
                f"of IntentMandate {intent.mandate_id!r}. "
                f"Total {cart.currency} {cart.total_amount:.2f} is within "
                f"spending cap {intent.currency} {intent.spending_cap:.2f} "
                f"(headroom ${headroom:.2f})."
            )
        else:
            verdict = (
                f"INVALID — Cart {cart.cart_id!r} fails one or more constraints "
                f"of IntentMandate {intent.mandate_id!r}. See [AP2] log lines above "
                f"for specific failure details."
            )

        return A2AResponse.success(
            task_id=task.task_id,
            agent_id=self.agent_id,
            result={
                "chain_valid": chain_valid,
                "cart_id": cart.cart_id,
                "intent_mandate_id": intent.mandate_id,
                "total_amount": cart.total_amount,
                "spending_cap": intent.spending_cap,
                "headroom": headroom,
                "currency": cart.currency,
                "verdict": verdict,
            },
            message=verdict,
        )

    # ----------------------------------------------------------------
    # Utility / admin
    # ----------------------------------------------------------------

    def get_transaction_log(self) -> List[Dict[str, Any]]:
        """Return the PaymentProcessor's full transaction audit log."""
        return self.processor.get_transaction_log()

    def get_mandate_summary(self) -> str:
        """Return a human-readable summary of all processed mandates."""
        return self.ap2.summary()

    def get_transaction_summary(self) -> str:
        """Return a human-readable summary of all transactions processed."""
        return self.processor.transaction_summary()

    def __repr__(self) -> str:
        return (
            f"PaymentAgent("
            f"id={self.agent_id!r}, "
            f"mandates={len(self.ap2._mandates)}, "
            f"carts={len(self.ap2._carts)}, "
            f"transactions={len(self.processor.get_transaction_log())})"
        )
