"""
agents/flight_agent.py
======================
FlightAgent — A2A Server Agent for Flight Search and Booking
-------------------------------------------------------------
The FlightAgent is a specialist A2A server that handles all flight-related
tasks within the TravelMind system.  It is discovered by the OrchestratorAgent
at runtime via the shared A2ARegistry using the capability strings
"find_flights" and "book_flight".

Capabilities
------------
  find_flights   : Search the mock flight database for available flights
                   matching a destination, date, and budget.  Returns the
                   top 3 cheapest options sorted by price ascending.

  book_flight    : Confirm a reservation for a specific flight by its ID.
                   Decrements the seat counter and returns a booking reference.

A2A Lifecycle
-------------
  On instantiation the agent:
    1. Loads the mock flight data from data/flights.json.
    2. Creates and publishes its AgentCard to the shared A2ARegistry so the
       OrchestratorAgent can discover it without a hard-coded reference.

  On each process_task(task) call:
    1. Validates the auth_token via the a2a.auth module.
    2. Dispatches to the appropriate handler based on task.intent.
    3. Returns a structured A2AResponse (success or error).

Data file
---------
  data/flights.json — list of flight objects with fields:
    id, from, to, date, price, airline, duration, seats_available

No external dependencies — stdlib only (json, os, pathlib).
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional

from a2a.a2a_protocol import A2AResponse, A2ATask, ResponseStatus, get_registry
from a2a.agent_card import AgentCard
from a2a.auth import verify_token

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent  # agents/
_DATA_DIR = _HERE.parent / "data"  # data/
_FLIGHTS_FILE = _DATA_DIR / "flights.json"


# ---------------------------------------------------------------------------
# FlightAgent
# ---------------------------------------------------------------------------


class FlightAgent:
    """
    A2A server agent responsible for searching and booking flights.

    The agent loads flight data once at startup (from data/flights.json) and
    keeps an in-memory working copy so that confirmed bookings (seat decrements)
    are reflected across subsequent calls within the same session.

    Parameters
    ----------
    agent_id : str
        Unique identifier for this agent instance.
        Defaults to "flight-agent-01".
    verbose : bool
        When True (default) the agent prints [FLIGHT AGENT] diagnostic lines
        in addition to the standard [A2A] log lines emitted by the client.
    auto_register : bool
        When True (default) the agent registers itself in the shared global
        A2ARegistry on instantiation.
    """

    # ----------------------------------------------------------------
    # Class-level AgentCard template
    # ----------------------------------------------------------------

    AGENT_ID: str = "flight-agent-01"
    AGENT_NAME: str = "FlightAgent"
    AGENT_DESCRIPTION: str = (
        "Specialist agent for searching available flights and confirming "
        "flight bookings.  Accepts find_flights and book_flight tasks via A2A."
    )
    CAPABILITIES: List[str] = ["find_flights", "book_flight"]
    SUPPORTED_TASKS: List[str] = ["find_flights", "book_flight"]
    ENDPOINT: str = "https://agents.travelmind.internal/flight/v1"

    # Maximum number of results returned for a find_flights query
    MAX_RESULTS: int = 3

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

        # Load flight data
        self._flights: List[Dict[str, Any]] = self._load_flights()
        # In-memory booking ledger: { flight_id -> booking_ref }
        self._bookings: Dict[str, str] = {}

        # Build AgentCard – embed 'instance' reference so A2AClient can
        # dispatch process_task() calls without importing this module directly.
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

    def _load_flights(self) -> List[Dict[str, Any]]:
        """Load and return flight records from the JSON data file."""
        if not _FLIGHTS_FILE.exists():
            self._log(
                f"[FLIGHT AGENT] WARNING: Data file not found at {_FLIGHTS_FILE}. "
                f"Using empty dataset."
            )
            return []
        with _FLIGHTS_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        self._log(
            f"[FLIGHT AGENT] Loaded {len(data)} flight records from "
            f"{_FLIGHTS_FILE.name}"
        )
        return data

    def _register(self) -> None:
        """Register this agent in the shared A2A discovery registry."""
        registry = get_registry()
        registry.register(self.agent_card)
        self._log(
            f"[FLIGHT AGENT] Registered {self.AGENT_NAME!r} in A2ARegistry "
            f"with capabilities: {self.CAPABILITIES}"
        )

    def _validate_auth(self, task: A2ATask) -> bool:
        """
        Verify the auth token carried by the incoming task.

        Returns True if the token is valid, False otherwise.
        Auth is checked against the sender's agent_id (the caller must have
        obtained a token scoped to their own agent_id).
        """
        if not self.agent_card.auth_required:
            return True
        return verify_token(task.auth_token, task.sender_id)

    def _new_booking_ref(self, flight_id: str) -> str:
        """Generate a deterministic booking reference for a flight."""
        import hashlib
        import time

        raw = f"{flight_id}-{time.time()}"
        digest = hashlib.md5(raw.encode()).hexdigest()[:8].upper()
        return f"BK-{digest}"

    # ----------------------------------------------------------------
    # Task dispatcher  (A2A entry point)
    # ----------------------------------------------------------------

    def process_task(self, task: A2ATask) -> A2AResponse:
        """
        Main A2A entry point — receives a task dispatched by A2AClient and
        routes it to the appropriate handler method.

        Supported intents
        -----------------
        "find_flights"   -> _handle_find_flights(task)
        "book_flight"    -> _handle_book_flight(task)

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
            f"\n[FLIGHT AGENT] ── Received task {task.task_id!r} "
            f"from {task.sender_id!r} | intent={task.intent!r}"
        )

        # Auth check
        if not self._validate_auth(task):
            self._log(f"[FLIGHT AGENT] Auth FAILED for task {task.task_id!r}")
            return A2AResponse.error(
                task_id=task.task_id,
                agent_id=self.agent_id,
                message=(
                    f"Authentication failed: invalid or expired token "
                    f"from sender {task.sender_id!r}."
                ),
            )

        # Intent dispatch
        if task.intent == "find_flights":
            return self._handle_find_flights(task)
        elif task.intent == "book_flight":
            return self._handle_book_flight(task)
        else:
            return A2AResponse.error(
                task_id=task.task_id,
                agent_id=self.agent_id,
                message=(
                    f"FlightAgent does not handle intent {task.intent!r}. "
                    f"Supported intents: {self.SUPPORTED_TASKS}"
                ),
            )

    # ----------------------------------------------------------------
    # Intent handlers
    # ----------------------------------------------------------------

    def _handle_find_flights(self, task: A2ATask) -> A2AResponse:
        """
        Search for available flights matching the criteria in task.payload.

        Expected payload keys
        ---------------------
        destination : str   Destination city/code (e.g. "Paris", "London").
                            Matched case-insensitively against the flight's
                            "to" field.
        date        : str   Outbound travel date in YYYY-MM-DD format.
                            Optional; if omitted, date filtering is skipped.
        budget      : float Hard maximum price per flight ticket in USD.
                            Optional; if omitted, no price ceiling is applied.
        origin      : str   Origin city/code (e.g. "NYC").
                            Optional; defaults to "NYC" when omitted.

        Returns
        -------
        A2AResponse with result dict:
          {
            "flights": [
              {
                "id":              str,
                "from":            str,
                "to":              str,
                "date":            str,
                "price":           float,
                "airline":         str,
                "duration":        str,
                "seats_available": int,
                "value_score":     float   # 1/price — higher is cheaper
              },
              ...  (up to MAX_RESULTS entries, sorted price ascending)
            ],
            "query": { ... }   # echo of search parameters for traceability
          }
        """
        payload = task.payload

        destination: str = str(payload.get("destination", "")).strip()
        date: Optional[str] = payload.get("date")
        budget: Optional[float] = payload.get("budget")
        origin: str = str(payload.get("origin", "NYC")).strip()

        self._log(f"[FLIGHT AGENT] Searching flights:")
        self._log(f"[FLIGHT AGENT]   Origin      : {origin!r}")
        self._log(f"[FLIGHT AGENT]   Destination : {destination!r}")
        self._log(f"[FLIGHT AGENT]   Date        : {date!r}")
        self._log(
            f"[FLIGHT AGENT]   Budget      : {'$' + str(budget) if budget else 'any'}"
        )

        if not destination:
            return A2AResponse.error(
                task_id=task.task_id,
                agent_id=self.agent_id,
                message="find_flights requires a 'destination' in the payload.",
            )

        # Work on a deep copy so original data is never mutated by filtering
        candidates = deepcopy(self._flights)

        # ---- Filter: destination ----
        dest_lower = destination.lower()
        candidates = [f for f in candidates if dest_lower in f.get("to", "").lower()]

        # ---- Filter: origin ----
        if origin:
            orig_lower = origin.lower()
            candidates = [
                f for f in candidates if orig_lower in f.get("from", "").lower()
            ]

        # ---- Filter: date ----
        if date:
            candidates = [f for f in candidates if f.get("date", "") == date]

        # ---- Filter: budget ----
        if budget is not None:
            candidates = [
                f for f in candidates if float(f.get("price", 0)) <= float(budget)
            ]

        # ---- Filter: seats available ----
        candidates = [f for f in candidates if int(f.get("seats_available", 0)) > 0]

        # ---- Sort by price ascending ----
        candidates.sort(key=lambda f: float(f.get("price", 0)))

        # ---- Limit results ----
        top_results = candidates[: self.MAX_RESULTS]

        # ---- Annotate with value_score ----
        for flight in top_results:
            price = float(flight.get("price", 1))
            flight["value_score"] = round(1.0 / price, 6) if price > 0 else 0.0

        self._log(
            f"[FLIGHT AGENT] Found {len(top_results)} matching flight(s) "
            f"(from {len(self._flights)} total, after filtering)"
        )
        for i, fl in enumerate(top_results, 1):
            self._log(
                f"[FLIGHT AGENT]   [{i}] {fl['airline']:<18s} "
                f"{fl['from']} → {fl['to']}  "
                f"${fl['price']:.2f}  {fl['duration']}  "
                f"seats={fl['seats_available']}"
            )

        if not top_results:
            return A2AResponse.error(
                task_id=task.task_id,
                agent_id=self.agent_id,
                message=(
                    f"No flights found for destination={destination!r}, "
                    f"date={date!r}, budget={budget}. "
                    f"Try relaxing filters (higher budget or different date)."
                ),
                result={"flights": [], "query": payload},
            )

        return A2AResponse.success(
            task_id=task.task_id,
            agent_id=self.agent_id,
            result={
                "flights": top_results,
                "query": {
                    "origin": origin,
                    "destination": destination,
                    "date": date,
                    "budget": budget,
                },
            },
            message=(
                f"Found {len(top_results)} flight(s) to {destination!r}. "
                f"Cheapest: {top_results[0]['airline']} at "
                f"${top_results[0]['price']:.2f}."
            ),
        )

    def _handle_book_flight(self, task: A2ATask) -> A2AResponse:
        """
        Confirm a flight booking by flight ID.

        Expected payload keys
        ---------------------
        flight_id   : str   The "id" field of the flight to book (e.g. "FL001").
        passenger   : str   Name / identifier of the passenger.
                            Optional; defaults to task.sender_id.

        Side-effects
        ------------
        Decrements seats_available by 1 in the in-memory working copy.
        Stores the booking reference in self._bookings.

        Returns
        -------
        A2AResponse with result dict:
          {
            "booking_ref": str,        # e.g. "BK-A4F2C1B3"
            "flight":      dict,       # the booked flight record
            "passenger":   str,
            "status":      "confirmed"
          }
        """
        payload = task.payload
        flight_id: str = str(payload.get("flight_id", "")).strip()
        passenger: str = str(payload.get("passenger", task.sender_id)).strip()

        self._log(
            f"[FLIGHT AGENT] Booking flight {flight_id!r} for passenger {passenger!r}"
        )

        if not flight_id:
            return A2AResponse.error(
                task_id=task.task_id,
                agent_id=self.agent_id,
                message="book_flight requires a 'flight_id' in the payload.",
            )

        # Look up the flight in the working copy
        matched = next(
            (f for f in self._flights if f.get("id") == flight_id),
            None,
        )

        if matched is None:
            return A2AResponse.error(
                task_id=task.task_id,
                agent_id=self.agent_id,
                message=f"Flight {flight_id!r} not found in inventory.",
            )

        if int(matched.get("seats_available", 0)) <= 0:
            return A2AResponse.error(
                task_id=task.task_id,
                agent_id=self.agent_id,
                message=(
                    f"Flight {flight_id!r} ({matched.get('airline')}) "
                    f"is fully booked — no seats available."
                ),
            )

        # Confirm booking: decrement seat count
        matched["seats_available"] = int(matched["seats_available"]) - 1

        booking_ref = self._new_booking_ref(flight_id)
        self._bookings[flight_id] = booking_ref

        self._log(f"[FLIGHT AGENT] Booking CONFIRMED ✓")
        self._log(f"[FLIGHT AGENT]   Booking Ref  : {booking_ref}")
        self._log(
            f"[FLIGHT AGENT]   Flight       : {matched['airline']} "
            f"{matched['from']} → {matched['to']} on {matched['date']}"
        )
        self._log(f"[FLIGHT AGENT]   Price        : ${matched['price']:.2f}")
        self._log(f"[FLIGHT AGENT]   Duration     : {matched['duration']}")
        self._log(f"[FLIGHT AGENT]   Seats Left   : {matched['seats_available']}")

        return A2AResponse.success(
            task_id=task.task_id,
            agent_id=self.agent_id,
            result={
                "booking_ref": booking_ref,
                "flight": deepcopy(matched),
                "passenger": passenger,
                "status": "confirmed",
            },
            message=(
                f"Flight {flight_id!r} booked successfully! "
                f"Ref: {booking_ref}. "
                f"{matched['airline']} {matched['from']}→{matched['to']} "
                f"on {matched['date']} — ${matched['price']:.2f}."
            ),
        )

    # ----------------------------------------------------------------
    # Utility / admin
    # ----------------------------------------------------------------

    def get_flight(self, flight_id: str) -> Optional[Dict[str, Any]]:
        """Return a copy of the flight record for the given ID, or None."""
        matched = next((f for f in self._flights if f.get("id") == flight_id), None)
        return deepcopy(matched) if matched else None

    def get_booking(self, flight_id: str) -> Optional[str]:
        """Return the booking reference for a confirmed flight, or None."""
        return self._bookings.get(flight_id)

    def list_flights(self) -> List[Dict[str, Any]]:
        """Return a deep copy of all loaded flight records (admin/debug)."""
        return deepcopy(self._flights)

    def __repr__(self) -> str:
        return (
            f"FlightAgent(id={self.agent_id!r}, "
            f"flights={len(self._flights)}, "
            f"bookings={len(self._bookings)})"
        )
