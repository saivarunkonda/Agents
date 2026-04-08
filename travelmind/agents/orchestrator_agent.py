"""
agents/orchestrator_agent.py
=============================
OrchestratorAgent — Master Trip-Booking Coordinator
-----------------------------------------------------
The OrchestratorAgent is the central brain of the TravelMind system.  It
receives a high-level user trip request ("Book me a trip to Paris, 3 nights,
budget $2000") and orchestrates every subsequent step autonomously:

  AP2 First       — Creates an IntentMandate BEFORE any agent is contacted,
                    establishing a cryptographically-anchored spending cap and
                    destination constraint that governs all downstream decisions.

  A2A Discovery   — Uses the shared A2ARegistry to find the FlightAgent,
                    HotelAgent, CarRentalAgent, and PaymentAgent by capability
                    string, without any hard-coded references.

  A2A Tasks       — Dispatches find_flights / find_hotels / find_cars tasks to
                    the specialist agents via A2AClient (which handles the full
                    discovery → auth → task → response lifecycle and emits
                    detailed [A2A] log lines at every step).

  Budget Fitting  — After receiving search results, selects the best combination
                    of flight + hotel (+ optional car) that fits within the
                    AP2 spending cap.  Falls back to cheaper options if the
                    first choice would exceed the budget.

  AP2 Cart        — Packages the selected items into a CartMandate, then calls
                    verify_mandate_chain() to confirm the total is within cap
                    and the destination matches before presenting to the user.

  User Approval   — In interactive mode: displays the cart and simulates user
                    confirmation.  In delegated mode: applies approval
                    automatically based on the pre-signed IntentMandate.

  A2A Payment     — Sends the approved CartMandate to the PaymentAgent via A2A.
                    The PaymentAgent runs its own independent AP2 verification
                    and PaymentProcessor charge, then returns a receipt.

  Trip Summary    — Assembles a comprehensive trip summary including booking
                    references, costs, receipt, and mandate IDs, and returns
                    it to the caller (main.py).

Public interface
----------------
  plan_trip(user_id, destination, nights, budget, travel_date,
            include_car=False, delegated=False, origin="NYC")
      -> dict  (full trip result — see _build_trip_summary() for schema)

All log lines are prefixed with either [ORCHESTRATOR], [A2A], or [AP2] so the
protocol boundaries are clearly visible in the console output.

No external dependencies — stdlib only.
"""

from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Path bootstrapping — ensure the project root is on sys.path so that
# sibling packages (a2a, ap2, agents) resolve correctly regardless of how
# the interpreter was launched.
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent  # project3_travelmind/
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from a2a.a2a_protocol import A2AClient, get_registry
from a2a.agent_card import AgentCard
from ap2.ap2_protocol import AP2Protocol
from ap2.mandate import CartMandate, IntentMandate

# ---------------------------------------------------------------------------
# OrchestratorAgent
# ---------------------------------------------------------------------------


class OrchestratorAgent:
    """
    Master coordinator agent for the TravelMind booking system.

    The OrchestratorAgent itself is NOT registered in the A2ARegistry — it is
    the *caller*, not a server.  It owns an A2AClient instance that it uses
    to discover and communicate with the specialist server agents.

    It also owns an AP2Protocol instance that it uses to create and manage
    IntentMandates and CartMandates on behalf of the user.

    Parameters
    ----------
    agent_id : str
        Identifier for this orchestrator instance.  Used as the sender_id in
        all A2A tasks it dispatches.  Defaults to "orchestrator-agent-01".
    verbose : bool
        When True (default) all [ORCHESTRATOR], [A2A], and [AP2] log lines
        are printed to stdout.
    """

    AGENT_ID: str = "orchestrator-agent-01"

    def __init__(
        self,
        agent_id: str = AGENT_ID,
        verbose: bool = True,
    ) -> None:
        self.agent_id = agent_id
        self.verbose = verbose

        # A2A client for dispatching tasks to specialist agents
        self.a2a_client = A2AClient(
            caller_id=self.agent_id,
            registry=get_registry(),
            verbose=verbose,
        )

        # AP2 protocol instance for mandate creation and management
        self.ap2 = AP2Protocol(verbose=verbose)

        self._log(
            f"[ORCHESTRATOR] OrchestratorAgent initialised (id={self.agent_id!r})"
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _log(self, message: str) -> None:
        if self.verbose:
            print(message)

    def _section(self, title: str, width: int = 62) -> None:
        """Print a visually distinct section header."""
        if self.verbose:
            bar = "-" * width
            print(f"\n[ORCHESTRATOR] {bar}")
            print(f"[ORCHESTRATOR]  {title}")
            print(f"[ORCHESTRATOR] {bar}")

    # ------------------------------------------------------------------
    # Main public method
    # ------------------------------------------------------------------

    def plan_trip(
        self,
        user_id: str,
        destination: str,
        nights: int,
        budget: float,
        travel_date: str,
        include_car: bool = False,
        delegated: bool = False,
        origin: str = "NYC",
        return_date: Optional[str] = None,
        constraints: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Execute the full autonomous trip-booking workflow.

        This is the main entry point called by main.py for each scenario.
        It runs the complete 10-step AP2 + A2A flow and returns a rich
        result dictionary regardless of success or failure.

        Parameters
        ----------
        user_id      : str    Identifier of the human making the booking.
        destination  : str    Destination city (e.g. "Paris", "London").
        nights       : int    Number of nights to stay.
        budget       : float  Maximum total spend in USD.
        travel_date  : str    Outbound travel date in YYYY-MM-DD format.
        include_car  : bool   When True, also search for and add a rental car.
                              Defaults to False.
        delegated    : bool   When True, apply cart approval automatically
                              (pre-authorised / delegated purchase flow).
                              When False (default), simulate interactive
                              user review and approval.
        origin       : str    Departure city/code.  Defaults to "NYC".
        return_date  : str    Optional return travel date for the summary.
        constraints  : dict   Optional additional AP2 constraints, e.g.
                              {"min_hotel_stars": 3, "prefer_direct_flights": True}.

        Returns
        -------
        dict  — Full trip result with keys:
          success          : bool
          user_id          : str
          destination      : str
          nights           : int
          budget           : float
          intent_mandate   : dict | None
          cart_mandate     : dict | None
          flight           : dict | None
          hotel            : dict | None
          car              : dict | None
          total_cost       : float
          payment_receipt  : dict | None
          transaction_id   : str | None
          error            : str | None
          steps_completed  : list[str]
        """
        self._section(
            f"TRIP PLAN: {destination.upper()} | {nights} nights | "
            f"Budget ${budget:.2f} | User: {user_id!r}"
        )

        steps_completed: List[str] = []
        constraints = constraints or {}

        # Merge in the include_car hint so the AP2 mandate captures it
        if include_car:
            constraints.setdefault("include_car_rental", True)

        # ----------------------------------------------------------------
        # Step 1 — AP2: Create Intent Mandate
        # ----------------------------------------------------------------
        self._section("Step 1/10 — [AP2] Create Intent Mandate")

        return_dt = return_date or self._compute_return_date(travel_date, nights)
        travel_dates = {"outbound": travel_date, "return": return_dt}

        intent_description = (
            f"Book round trip to {destination} from {origin}, {nights} nights, "
            f"under ${budget:.0f}. "
            f"Departure: {travel_date}."
        )
        if include_car:
            intent_description += " Include car rental."

        try:
            intent_mandate: IntentMandate = self.ap2.create_intent_mandate(
                user_id=user_id,
                intent_description=intent_description,
                spending_cap=budget,
                destination=destination,
                travel_dates=travel_dates,
                constraints=constraints,
                currency="USD",
                metadata={
                    "origin": origin,
                    "nights": nights,
                    "include_car": include_car,
                    "delegated": delegated,
                },
            )
            steps_completed.append("AP2: Intent Mandate created")
            self._log(
                f"[ORCHESTRATOR] Intent Mandate created: "
                f"{intent_mandate.mandate_id} (cap=${budget:.2f})"
            )
        except Exception as exc:
            return self._error_result(
                user_id,
                destination,
                nights,
                budget,
                steps_completed,
                f"Failed to create IntentMandate: {exc}",
            )

        # ----------------------------------------------------------------
        # Step 2 — A2A Discovery: Find all required agents
        # ----------------------------------------------------------------
        self._section("Step 2/10 — [A2A] Agent Discovery")

        registry = get_registry()
        self._log(f"[ORCHESTRATOR] Querying A2A registry for required capabilities...")
        self._log(f"[ORCHESTRATOR] {registry.summary()}")

        required_capabilities = ["find_flights", "find_hotels", "process_payment"]
        if include_car:
            required_capabilities.append("find_cars")

        missing_caps = []
        for cap in required_capabilities:
            if not registry.discover_one(cap):
                missing_caps.append(cap)

        if missing_caps:
            return self._error_result(
                user_id,
                destination,
                nights,
                budget,
                steps_completed,
                f"A2A Registry is missing agents for capabilities: {missing_caps}. "
                f"Ensure all agents are instantiated before calling plan_trip().",
                intent_mandate=intent_mandate,
            )

        self._log(
            f"[ORCHESTRATOR] All {len(required_capabilities)} required capabilities "
            f"discovered in registry ✓"
        )
        steps_completed.append("A2A: Agent discovery complete")

        # ----------------------------------------------------------------
        # Step 3 — A2A Task: Find flights
        # ----------------------------------------------------------------
        self._section(
            f"Step 3/10 — [A2A] Find Flights → {destination} on {travel_date}"
        )

        # Reserve budget headroom for hotel: flights should be ≤ 50% of total
        # Use the full budget as ceiling here; we'll enforce total in Step 6.
        flight_response = self.a2a_client.send_task(
            capability="find_flights",
            intent="find_flights",
            payload={
                "origin": origin,
                "destination": destination,
                "date": travel_date,
                "budget": budget,  # let the agent filter; we select below
            },
        )

        if not flight_response.is_success():
            return self._error_result(
                user_id,
                destination,
                nights,
                budget,
                steps_completed,
                f"FlightAgent returned an error: {flight_response.message}",
                intent_mandate=intent_mandate,
            )

        available_flights: List[Dict[str, Any]] = flight_response.result.get(
            "flights", []
        )
        if not available_flights:
            return self._error_result(
                user_id,
                destination,
                nights,
                budget,
                steps_completed,
                f"No flights available to {destination!r} on {travel_date}.",
                intent_mandate=intent_mandate,
            )

        steps_completed.append(
            f"A2A: FlightAgent returned {len(available_flights)} option(s)"
        )
        self._log(
            f"[ORCHESTRATOR] FlightAgent returned {len(available_flights)} "
            f"flight option(s)"
        )

        # ----------------------------------------------------------------
        # Step 4 — A2A Task: Find hotels
        # ----------------------------------------------------------------
        self._section(
            f"Step 4/10 — [A2A] Find Hotels in {destination} for {nights} nights"
        )

        hotel_response = self.a2a_client.send_task(
            capability="find_hotels",
            intent="find_hotels",
            payload={
                "city": destination,
                "nights": nights,
                "budget_total": budget,  # orchestrator enforces total budget
                "min_stars": int(constraints.get("min_hotel_stars", 1)),
            },
        )

        if not hotel_response.is_success():
            return self._error_result(
                user_id,
                destination,
                nights,
                budget,
                steps_completed,
                f"HotelAgent returned an error: {hotel_response.message}",
                intent_mandate=intent_mandate,
            )

        available_hotels: List[Dict[str, Any]] = hotel_response.result.get("hotels", [])
        if not available_hotels:
            return self._error_result(
                user_id,
                destination,
                nights,
                budget,
                steps_completed,
                f"No hotels available in {destination!r} for {nights} nights.",
                intent_mandate=intent_mandate,
            )

        steps_completed.append(
            f"A2A: HotelAgent returned {len(available_hotels)} option(s)"
        )
        self._log(
            f"[ORCHESTRATOR] HotelAgent returned {len(available_hotels)} "
            f"hotel option(s)"
        )

        # ----------------------------------------------------------------
        # Step 5 — A2A Task: Find cars (optional)
        # ----------------------------------------------------------------
        available_cars: List[Dict[str, Any]] = []

        if include_car:
            self._section(
                f"Step 5/10 — [A2A] Find Cars in {destination} for {nights} days"
            )
            car_response = self.a2a_client.send_task(
                capability="find_cars",
                intent="find_cars",
                payload={
                    "city": destination,
                    "days": nights,
                    "budget_total": budget,
                },
            )
            if car_response.is_success():
                available_cars = car_response.result.get("cars", [])
                self._log(
                    f"[ORCHESTRATOR] CarRentalAgent returned "
                    f"{len(available_cars)} car option(s)"
                )
                steps_completed.append(
                    f"A2A: CarRentalAgent returned {len(available_cars)} option(s)"
                )
            else:
                self._log(
                    f"[ORCHESTRATOR] CarRentalAgent error (non-fatal): "
                    f"{car_response.message}"
                )
                self._log(f"[ORCHESTRATOR] Continuing without car rental.")
        else:
            self._log(f"[ORCHESTRATOR] Step 5/10 — Car rental not requested. Skipping.")

        # ----------------------------------------------------------------
        # Step 6 — Select best options within budget
        # ----------------------------------------------------------------
        self._section("Step 6/10 — Selecting Best Trip Components Within Budget")

        selected_flight, selected_hotel, selected_car, selection_error = (
            self._select_within_budget(
                flights=available_flights,
                hotels=available_hotels,
                cars=available_cars,
                budget=budget,
                nights=nights,
                include_car=include_car,
                destination=destination,
            )
        )

        if selection_error:
            return self._error_result(
                user_id,
                destination,
                nights,
                budget,
                steps_completed,
                selection_error,
                intent_mandate=intent_mandate,
            )

        steps_completed.append("Budget selection: best combination chosen")

        # Log the chosen combination
        total_cost = float(selected_flight.get("price", 0)) + float(
            selected_hotel.get("total_cost", 0)
        )
        if selected_car:
            total_cost += float(selected_car.get("total_cost", 0))

        self._log(f"[ORCHESTRATOR] Selected combination:")
        self._log(
            f"[ORCHESTRATOR]   ✈ Flight : "
            f"{selected_flight.get('airline')} — "
            f"${selected_flight.get('price', 0):.2f}"
        )
        self._log(
            f"[ORCHESTRATOR]   🏨 Hotel  : "
            f"{selected_hotel.get('name')} ({selected_hotel.get('stars')}★) — "
            f"${selected_hotel.get('total_cost', 0):.2f} ({nights} nights)"
        )
        if selected_car:
            self._log(
                f"[ORCHESTRATOR]   🚗 Car    : "
                f"{selected_car.get('company')} {selected_car.get('type')} — "
                f"${selected_car.get('total_cost', 0):.2f} ({nights} days)"
            )
        self._log(
            f"[ORCHESTRATOR]   💰 TOTAL  : ${total_cost:.2f} "
            f"(budget: ${budget:.2f}, "
            f"headroom: ${budget - total_cost:.2f})"
        )

        # ----------------------------------------------------------------
        # Step 7 — AP2: Create Cart Mandate
        # ----------------------------------------------------------------
        self._section("Step 7/10 — [AP2] Create Cart Mandate")

        cart_items = self._build_cart_items(
            flight=selected_flight,
            hotel=selected_hotel,
            car=selected_car,
            nights=nights,
            destination=destination,
        )

        try:
            cart_mandate: CartMandate = self.ap2.create_cart_mandate(
                intent_mandate=intent_mandate,
                selected_items=cart_items,
            )
            steps_completed.append("AP2: Cart Mandate created")
            self._log(
                f"[ORCHESTRATOR] Cart Mandate created: "
                f"{cart_mandate.cart_id} "
                f"(total=${cart_mandate.total_amount:.2f})"
            )
        except Exception as exc:
            return self._error_result(
                user_id,
                destination,
                nights,
                budget,
                steps_completed,
                f"Failed to create CartMandate: {exc}",
                intent_mandate=intent_mandate,
            )

        # ----------------------------------------------------------------
        # Step 7b — AP2: Verify mandate chain (pre-flight check)
        # ----------------------------------------------------------------
        self._section("Step 7b/10 — [AP2] Verify Mandate Chain (pre-flight check)")

        chain_ok = self.ap2.verify_mandate_chain(cart_mandate, intent_mandate)
        if not chain_ok:
            return self._error_result(
                user_id,
                destination,
                nights,
                budget,
                steps_completed,
                f"AP2 mandate chain verification FAILED: cart total "
                f"${cart_mandate.total_amount:.2f} does not satisfy "
                f"IntentMandate constraints (cap=${budget:.2f}).",
                intent_mandate=intent_mandate,
                cart_mandate=cart_mandate,
            )

        steps_completed.append("AP2: Mandate chain verified ✓")

        # ----------------------------------------------------------------
        # Step 8 — User approval
        # ----------------------------------------------------------------
        self._section("Step 8/10 — User Approval")

        if delegated:
            self._log(
                f"[ORCHESTRATOR] Delegated purchase mode: applying automatic "
                f"approval via pre-signed IntentMandate "
                f"{intent_mandate.mandate_id!r}"
            )
            approval_method = "delegated"
        else:
            self._log(
                f"[ORCHESTRATOR] Interactive mode: presenting cart to user "
                f"for review..."
            )
            self._display_cart_for_user(
                cart_mandate=cart_mandate,
                intent_mandate=intent_mandate,
                flight=selected_flight,
                hotel=selected_hotel,
                car=selected_car,
                nights=nights,
            )
            approval_method = "interactive"

        try:
            cart_mandate = self.ap2.user_approve_cart(
                cart_mandate,
                approval_method=approval_method,
            )
            steps_completed.append(
                f"User approval: {approval_method} approval applied ✓"
            )
        except Exception as exc:
            return self._error_result(
                user_id,
                destination,
                nights,
                budget,
                steps_completed,
                f"User approval step failed: {exc}",
                intent_mandate=intent_mandate,
                cart_mandate=cart_mandate,
            )

        # ----------------------------------------------------------------
        # Step 9 — A2A Task: Send to PaymentAgent
        # ----------------------------------------------------------------
        self._section("Step 9/10 — [A2A] Send to PaymentAgent for Processing")

        # Serialise mandates for the A2A wire format
        cart_dict = cart_mandate.to_dict()
        cart_dict["_full_signature"] = cart_mandate.signature  # include full sig
        cart_dict["user_approved"] = cart_mandate.user_approved
        cart_dict["status"] = cart_mandate.status
        cart_dict["approval_method"] = cart_mandate.approval_method

        intent_dict = intent_mandate.to_dict()
        intent_dict["_full_signature"] = intent_mandate.signature  # include full sig
        intent_dict["status"] = intent_mandate.status

        payment_response = self.a2a_client.send_task(
            capability="process_payment",
            intent="process_payment",
            payload={
                "cart_mandate": cart_dict,
                "intent_mandate": intent_dict,
                "approval_method": approval_method,
                "force_approve": True,  # cart was already approved in Step 8
            },
        )

        if not payment_response.is_success():
            return self._error_result(
                user_id,
                destination,
                nights,
                budget,
                steps_completed,
                f"PaymentAgent returned an error: {payment_response.message}",
                intent_mandate=intent_mandate,
                cart_mandate=cart_mandate,
            )

        steps_completed.append("A2A: PaymentAgent processed payment ✓")
        payment_result = payment_response.result or {}
        receipt = payment_result.get("receipt", {})
        transaction_id = payment_result.get("transaction_id", "N/A")

        self._log(
            f"[ORCHESTRATOR] Payment successful ✓  "
            f"TXN: {transaction_id}  "
            f"Amount: ${payment_result.get('amount', 0):.2f}"
        )

        # Mark IntentMandate as fulfilled in our own AP2 instance too
        self.ap2.mark_intent_fulfilled(intent_mandate.mandate_id)
        self.ap2.mark_cart_charged(cart_mandate.cart_id)

        # ----------------------------------------------------------------
        # Step 10 — Return full trip summary
        # ----------------------------------------------------------------
        self._section("Step 10/10 — Trip Summary")

        summary = self._build_trip_summary(
            success=True,
            user_id=user_id,
            destination=destination,
            nights=nights,
            budget=budget,
            origin=origin,
            travel_date=travel_date,
            return_date=return_dt,
            intent_mandate=intent_mandate,
            cart_mandate=cart_mandate,
            flight=selected_flight,
            hotel=selected_hotel,
            car=selected_car,
            payment_result=payment_result,
            steps_completed=steps_completed,
        )

        steps_completed.append("Trip summary assembled ✓")

        self._print_trip_summary(summary)

        return summary

    # ------------------------------------------------------------------
    # Budget selection logic
    # ------------------------------------------------------------------

    def _select_within_budget(
        self,
        flights: List[Dict[str, Any]],
        hotels: List[Dict[str, Any]],
        cars: List[Dict[str, Any]],
        budget: float,
        nights: int,
        include_car: bool,
        destination: str,
    ) -> Tuple[
        Optional[Dict[str, Any]],
        Optional[Dict[str, Any]],
        Optional[Dict[str, Any]],
        Optional[str],
    ]:
        """
        Select the best flight + hotel (+ optional car) combination that fits
        within the AP2 spending cap (budget).

        Strategy
        --------
        1. Sort flights cheapest-first and hotels best-value-first (pre-sorted
           by the specialist agents).
        2. Try each flight × hotel combination in order (cheapest flight with
           best-value hotel first).
        3. Add the cheapest available car if include_car is True and there is
           remaining headroom.
        4. Return the first combination whose total <= budget.
        5. If no combination fits, return an explanatory error string.

        Returns
        -------
        Tuple of (selected_flight, selected_hotel, selected_car, error_message).
        On success: (flight_dict, hotel_dict, car_dict_or_None, None).
        On failure: (None, None, None, error_string).
        """
        self._log(
            f"[ORCHESTRATOR] Fitting best combination within budget ${budget:.2f}..."
        )

        # Flights already sorted by price ascending by FlightAgent
        # Hotels already sorted by value_score descending by HotelAgent
        for flight in flights:
            flight_price = float(flight.get("price", 0))

            for hotel in hotels:
                hotel_total = float(hotel.get("total_cost", 0))

                subtotal = flight_price + hotel_total
                remaining = budget - subtotal

                if remaining < 0:
                    self._log(
                        f"[ORCHESTRATOR]   Skip: {flight.get('airline')} "
                        f"(${flight_price:.2f}) + "
                        f"{hotel.get('name')} (${hotel_total:.2f}) "
                        f"= ${subtotal:.2f} exceeds budget"
                    )
                    continue

                # Try to fit a car if requested
                selected_car: Optional[Dict[str, Any]] = None
                if include_car and cars:
                    cheapest_car = cars[0]  # already sorted by price asc
                    car_total = float(cheapest_car.get("total_cost", 0))
                    if car_total <= remaining:
                        selected_car = deepcopy(cheapest_car)
                        subtotal += car_total
                        self._log(
                            f"[ORCHESTRATOR]   Car fits: "
                            f"{cheapest_car.get('company')} "
                            f"{cheapest_car.get('type')} "
                            f"(${car_total:.2f}) — "
                            f"new total ${subtotal:.2f}"
                        )
                    else:
                        self._log(
                            f"[ORCHESTRATOR]   Car too expensive "
                            f"(${car_total:.2f}) — skipping car, "
                            f"proceeding without rental"
                        )

                self._log(
                    f"[ORCHESTRATOR]   SELECTED: "
                    f"{flight.get('airline')} (${flight_price:.2f}) + "
                    f"{hotel.get('name')} (${hotel_total:.2f})"
                    + (
                        f" + {selected_car.get('company')} "
                        f"(${float(selected_car.get('total_cost', 0)):.2f})"
                        if selected_car
                        else ""
                    )
                    + f" = ${subtotal:.2f} ✓"
                )

                return deepcopy(flight), deepcopy(hotel), selected_car, None

        # No combination fits
        cheapest_flight = float(flights[0].get("price", 0)) if flights else 0
        cheapest_hotel = float(hotels[0].get("total_cost", 0)) if hotels else 0
        min_possible = cheapest_flight + cheapest_hotel

        error = (
            f"No flight + hotel combination fits within the ${budget:.2f} budget. "
            f"The cheapest available combination for {destination!r} is "
            f"${min_possible:.2f} "
            f"(flight ${cheapest_flight:.2f} + hotel ${cheapest_hotel:.2f}). "
            f"Consider increasing your budget or reducing the number of nights."
        )
        return None, None, None, error

    # ------------------------------------------------------------------
    # Cart item builder
    # ------------------------------------------------------------------

    def _build_cart_items(
        self,
        flight: Dict[str, Any],
        hotel: Dict[str, Any],
        car: Optional[Dict[str, Any]],
        nights: int,
        destination: str,
    ) -> List[Dict[str, Any]]:
        """
        Convert the selected travel components into the AP2 CartMandate
        item format.

        Each item must have at minimum:
          type, id, description, price, merchant_id, destination/city/to
        """
        items: List[Dict[str, Any]] = []

        # Flight item
        flight_price = float(flight.get("price", 0))
        items.append(
            {
                "type": "flight",
                "id": flight.get("id", "FL-UNKNOWN"),
                "description": (
                    f"{flight.get('airline')} {flight.get('from', 'NYC')} → "
                    f"{flight.get('to', destination)} on "
                    f"{flight.get('date', 'TBD')} ({flight.get('duration', '?')})"
                ),
                "price": flight_price,
                "currency": "USD",
                "merchant_id": f"airline-{flight.get('airline', 'unknown').lower().replace(' ', '-')}",
                "airline": flight.get("airline", ""),
                "from": flight.get("from", "NYC"),
                "to": flight.get("to", destination),
                "date": flight.get("date", "TBD"),
                "duration": flight.get("duration", "?"),
                # Destination hint for AP2 verify_mandate_chain()
                "destination": destination,
            }
        )

        # Hotel item
        hotel_price_per_night = float(hotel.get("price_per_night", 0))
        hotel_total = float(hotel.get("total_cost", hotel_price_per_night * nights))
        items.append(
            {
                "type": "hotel",
                "id": hotel.get("id", "HT-UNKNOWN"),
                "description": (
                    f"{hotel.get('name')} ({hotel.get('stars', '?')} stars) "
                    f"in {hotel.get('city', destination)} - "
                    f"{nights} nights @ ${hotel_price_per_night:.2f}/night"
                ),
                "price": hotel_total,
                "currency": "USD",
                "merchant_id": f"hotel-{hotel.get('id', 'unknown').lower()}",
                "hotel_name": hotel.get("name", ""),
                "city": hotel.get("city", destination),
                "stars": hotel.get("stars", 0),
                "hotel_stars": hotel.get("stars", 0),
                "price_per_night": hotel_price_per_night,
                "nights": nights,
                "amenities": hotel.get("amenities", []),
                # Destination hint for AP2 verify_mandate_chain()
                "destination": destination,
            }
        )

        # Car rental item (optional)
        if car:
            car_price_per_day = float(car.get("price_per_day", 0))
            car_total = float(car.get("total_cost", car_price_per_day * nights))
            items.append(
                {
                    "type": "car",
                    "id": car.get("id", "CR-UNKNOWN"),
                    "description": (
                        f"{car.get('company')} {car.get('type')} "
                        f"in {car.get('city', destination)} - "
                        f"{nights} days @ ${car_price_per_day:.2f}/day"
                    ),
                    "price": car_total,
                    "currency": "USD",
                    "merchant_id": f"rental-{car.get('company', 'unknown').lower().replace(' ', '-')}",
                    "company": car.get("company", ""),
                    "car_type": car.get("type", ""),
                    "city": car.get("city", destination),
                    "price_per_day": car_price_per_day,
                    "days": nights,
                    "destination": destination,
                }
            )

        return items

    # ------------------------------------------------------------------
    # User-facing cart display
    # ------------------------------------------------------------------

    def _display_cart_for_user(
        self,
        cart_mandate: CartMandate,
        intent_mandate: IntentMandate,
        flight: Dict[str, Any],
        hotel: Dict[str, Any],
        car: Optional[Dict[str, Any]],
        nights: int,
    ) -> None:
        """
        Print a user-friendly cart summary to simulate the interactive
        approval step.  In a real application this would render a UI.
        """
        if not self.verbose:
            return

        width = 62
        bar = "=" * width
        thin = "-" * width

        print(f"\n[ORCHESTRATOR] +{bar}+")
        print(f"[ORCHESTRATOR] |{'  TRIP CART -- REVIEW & APPROVE':^{width}}|")
        print(f"[ORCHESTRATOR] +{bar}+")
        print(
            f"[ORCHESTRATOR] |  Intent : "
            f"{intent_mandate.intent_description[:50]:<50s}  |"
        )
        print(
            f"[ORCHESTRATOR] |  Budget : "
            f"${intent_mandate.spending_cap:.2f} (cap)                              |"
        )
        print(f"[ORCHESTRATOR] +{bar}+")
        print(f"[ORCHESTRATOR] |  {'Item':<12s} {'Description':<32s} {'Price':>8s}  |")
        print(f"[ORCHESTRATOR] |  {thin[:57]}  |")

        for item in cart_mandate.items:
            item_type = item.get("type", "item").upper()
            desc = str(item.get("description", item.get("id", "?")))
            desc_short = desc[:32]
            price = float(item.get("price", 0))
            print(
                f"[ORCHESTRATOR] |  {item_type:<12s} {desc_short:<32s} "
                f"${price:>7.2f}  |"
            )

        print(f"[ORCHESTRATOR] |  {thin[:57]}  |")
        print(
            f"[ORCHESTRATOR] |  {'TOTAL':<12s} {'':32s} "
            f"${cart_mandate.total_amount:>7.2f}  |"
        )
        headroom = intent_mandate.spending_cap - cart_mandate.total_amount
        print(
            f"[ORCHESTRATOR] |  {'Headroom':<12s} {'(remaining from budget)':<32s} "
            f"${headroom:>7.2f}  |"
        )
        print(f"[ORCHESTRATOR] +{bar}+")
        print(f"[ORCHESTRATOR] |  Cart ID : {cart_mandate.cart_id:<51s}|")
        print(f"[ORCHESTRATOR] |  Intent  : {intent_mandate.mandate_id:<51s}|")
        print(f"[ORCHESTRATOR] +{bar}+")
        print(f"[ORCHESTRATOR] -> User reviews cart and confirms purchase.")

    # ------------------------------------------------------------------
    # Trip summary builder
    # ------------------------------------------------------------------

    def _build_trip_summary(
        self,
        success: bool,
        user_id: str,
        destination: str,
        nights: int,
        budget: float,
        origin: str,
        travel_date: str,
        return_date: str,
        intent_mandate: Optional[IntentMandate],
        cart_mandate: Optional[CartMandate],
        flight: Optional[Dict[str, Any]],
        hotel: Optional[Dict[str, Any]],
        car: Optional[Dict[str, Any]],
        payment_result: Optional[Dict[str, Any]],
        steps_completed: List[str],
        error: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Assemble and return the canonical trip result dictionary.
        """
        total_cost = 0.0
        if flight:
            total_cost += float(flight.get("price", 0))
        if hotel:
            total_cost += float(hotel.get("total_cost", 0))
        if car:
            total_cost += float(car.get("total_cost", 0))

        return {
            "success": success,
            "user_id": user_id,
            "destination": destination,
            "origin": origin,
            "nights": nights,
            "budget": budget,
            "travel_date": travel_date,
            "return_date": return_date,
            "intent_mandate_id": (
                intent_mandate.mandate_id if intent_mandate else None
            ),
            "cart_mandate_id": (cart_mandate.cart_id if cart_mandate else None),
            "intent_mandate": intent_mandate.to_dict() if intent_mandate else None,
            "cart_mandate": cart_mandate.to_dict() if cart_mandate else None,
            "flight": deepcopy(flight) if flight else None,
            "hotel": deepcopy(hotel) if hotel else None,
            "car": deepcopy(car) if car else None,
            "total_cost": round(total_cost, 2),
            "budget_headroom": round(budget - total_cost, 2),
            "payment_receipt": (
                payment_result.get("receipt") if payment_result else None
            ),
            "transaction_id": (
                payment_result.get("transaction_id") if payment_result else None
            ),
            "receipt_id": (
                payment_result.get("receipt_id") if payment_result else None
            ),
            "items_charged": (
                payment_result.get("items_charged", []) if payment_result else []
            ),
            "error": error,
            "steps_completed": steps_completed,
        }

    def _print_trip_summary(self, summary: Dict[str, Any]) -> None:
        """Print a concise, human-readable trip summary to stdout."""
        if not self.verbose:
            return

        width = 62
        bar = "=" * width

        status = "BOOKED [OK]" if summary["success"] else "FAILED [!!]"

        print(f"\n[ORCHESTRATOR] +{bar}+")
        print(f"[ORCHESTRATOR] |{'  TRIP BOOKING SUMMARY -- ' + status:^{width}}|")
        print(f"[ORCHESTRATOR] +{bar}+")
        print(f"[ORCHESTRATOR] |  User          : {summary['user_id']:<44s}|")
        print(f"[ORCHESTRATOR] |  Destination   : {summary['destination']:<44s}|")
        print(f"[ORCHESTRATOR] |  Travel Date   : {summary['travel_date']:<44s}|")
        print(f"[ORCHESTRATOR] |  Return Date   : {summary['return_date']:<44s}|")
        print(f"[ORCHESTRATOR] |  Nights        : {str(summary['nights']):<44s}|")

        if summary["flight"]:
            fl = summary["flight"]
            print(
                f"[ORCHESTRATOR] |  [Flight]      : "
                f"{fl.get('airline', '?')} "
                f"{fl.get('from', '?')}->{fl.get('to', '?')} "
                f"${fl.get('price', 0):.2f}        |"
            )

        if summary["hotel"]:
            ht = summary["hotel"]
            print(
                f"[ORCHESTRATOR] |  [Hotel]       : "
                f"{ht.get('name', '?')} ({ht.get('stars', '?')} stars) "
                f"${ht.get('total_cost', 0):.2f}       |"
            )

        if summary["car"]:
            cr = summary["car"]
            print(
                f"[ORCHESTRATOR] |  [Car]         : "
                f"{cr.get('company', '?')} {cr.get('type', '?')} "
                f"${cr.get('total_cost', 0):.2f}         |"
            )

        print(
            f"[ORCHESTRATOR] |  [Total Cost]  : "
            f"${summary['total_cost']:.2f} "
            f"(budget ${summary['budget']:.2f}, "
            f"headroom ${summary['budget_headroom']:.2f})  |"
        )

        if summary.get("transaction_id"):
            print(
                f"[ORCHESTRATOR] |  [TXN ID]      : {summary['transaction_id']:<44s}|"
            )
        if summary.get("receipt_id"):
            print(f"[ORCHESTRATOR] |  [Receipt]     : {summary['receipt_id']:<44s}|")
        if summary.get("intent_mandate_id"):
            print(
                f"[ORCHESTRATOR] |  [IntentMand]  : "
                f"{summary['intent_mandate_id']:<44s}|"
            )
        if summary.get("cart_mandate_id"):
            print(
                f"[ORCHESTRATOR] |  [CartMand]    : {summary['cart_mandate_id']:<44s}|"
            )

        if summary.get("error"):
            err_short = summary["error"][:55]
            print(f"[ORCHESTRATOR] |  [Error]       : {err_short:<44s}|")

        steps = summary.get("steps_completed", [])
        print(f"[ORCHESTRATOR] |  Steps Done    : {str(len(steps)):<44s}|")
        print(f"[ORCHESTRATOR] +{bar}+\n")

    # ------------------------------------------------------------------
    # Error result builder
    # ------------------------------------------------------------------

    def _error_result(
        self,
        user_id: str,
        destination: str,
        nights: int,
        budget: float,
        steps_completed: List[str],
        error_message: str,
        intent_mandate: Optional[IntentMandate] = None,
        cart_mandate: Optional[CartMandate] = None,
        origin: str = "NYC",
        travel_date: str = "TBD",
        return_date: str = "TBD",
    ) -> Dict[str, Any]:
        """
        Build and log a standardised failure result dictionary.
        """
        self._log(f"\n[ORCHESTRATOR] ✗ TRIP PLANNING FAILED")
        self._log(f"[ORCHESTRATOR]   Reason : {error_message}")

        steps_completed.append(f"FAILED: {error_message[:60]}")

        return self._build_trip_summary(
            success=False,
            user_id=user_id,
            destination=destination,
            nights=nights,
            budget=budget,
            origin=origin,
            travel_date=travel_date,
            return_date=return_date,
            intent_mandate=intent_mandate,
            cart_mandate=cart_mandate,
            flight=None,
            hotel=None,
            car=None,
            payment_result=None,
            steps_completed=steps_completed,
            error=error_message,
        )

    # ------------------------------------------------------------------
    # Static utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_return_date(travel_date: str, nights: int) -> str:
        """
        Compute the return date by adding `nights` days to `travel_date`.

        Falls back gracefully if parsing fails (returns "TBD").
        """
        try:
            from datetime import date, timedelta

            parts = travel_date.split("-")
            d = date(int(parts[0]), int(parts[1]), int(parts[2]))
            return str(d + timedelta(days=nights))
        except Exception:
            return "TBD"

    def __repr__(self) -> str:
        return (
            f"OrchestratorAgent("
            f"id={self.agent_id!r}, "
            f"ap2_mandates={len(self.ap2._mandates)})"
        )
