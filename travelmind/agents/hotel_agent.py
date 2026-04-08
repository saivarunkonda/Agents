"""
agents/hotel_agent.py
=====================
HotelAgent — A2A Server Agent for Hotel Search and Booking
-----------------------------------------------------------
The HotelAgent is a specialist A2A server that handles all hotel-related
tasks within the TravelMind system.  It is discovered by the OrchestratorAgent
at runtime via the shared A2ARegistry using the capability strings
"find_hotels" and "book_hotel".

Capabilities
------------
  find_hotels  : Search the mock hotel database for available hotels
                 matching a city, nightly budget, star rating, and
                 availability.  Returns options sorted by "value score"
                 (stars divided by price_per_night — higher is better value).

  book_hotel   : Confirm a reservation for a specific hotel by its ID and
                 number of nights.  Marks the hotel as unavailable in the
                 working copy and returns a booking reference.

A2A Lifecycle
-------------
  On instantiation the agent:
    1. Loads the mock hotel data from data/hotels.json.
    2. Creates and publishes its AgentCard to the shared A2ARegistry so the
       OrchestratorAgent can discover it without a hard-coded reference.

  On each process_task(task) call:
    1. Validates the auth_token via the a2a.auth module.
    2. Dispatches to the appropriate handler based on task.intent.
    3. Returns a structured A2AResponse (success or error).

Data file
---------
  data/hotels.json — list of hotel objects with fields:
    id, city, name, stars, price_per_night, available, amenities

No external dependencies — stdlib only (json, pathlib, copy, hashlib).
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

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent  # agents/
_DATA_DIR = _HERE.parent / "data"  # data/
_HOTELS_FILE = _DATA_DIR / "hotels.json"


# ---------------------------------------------------------------------------
# HotelAgent
# ---------------------------------------------------------------------------


class HotelAgent:
    """
    A2A server agent responsible for searching and booking hotels.

    The agent loads hotel data once at startup (from data/hotels.json) and
    keeps an in-memory working copy so that confirmed bookings (availability
    updates) are reflected across subsequent calls within the same session.

    Hotels are ranked by a *value score* computed as::

        value_score = stars / price_per_night

    A higher value score means more stars per dollar — a useful proxy for
    "best value" that does not simply always return the cheapest option.

    Parameters
    ----------
    agent_id : str
        Unique identifier for this agent instance.
        Defaults to "hotel-agent-01".
    verbose : bool
        When True (default) the agent prints [HOTEL AGENT] diagnostic lines
        in addition to the standard [A2A] log lines emitted by the client.
    auto_register : bool
        When True (default) the agent registers itself in the shared global
        A2ARegistry on instantiation.
    """

    # ----------------------------------------------------------------
    # Class-level constants
    # ----------------------------------------------------------------

    AGENT_ID: str = "hotel-agent-01"
    AGENT_NAME: str = "HotelAgent"
    AGENT_DESCRIPTION: str = (
        "Specialist agent for searching available hotels and confirming "
        "hotel bookings.  Accepts find_hotels and book_hotel tasks via A2A."
    )
    CAPABILITIES: List[str] = ["find_hotels", "book_hotel"]
    SUPPORTED_TASKS: List[str] = ["find_hotels", "book_hotel"]
    ENDPOINT: str = "https://agents.travelmind.internal/hotel/v1"

    # Maximum number of results returned for a find_hotels query
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

        # Load hotel data from JSON
        self._hotels: List[Dict[str, Any]] = self._load_hotels()
        # In-memory booking ledger: { hotel_id -> booking_ref }
        self._bookings: Dict[str, str] = {}

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

    def _load_hotels(self) -> List[Dict[str, Any]]:
        """Load and return hotel records from the JSON data file."""
        if not _HOTELS_FILE.exists():
            self._log(
                f"[HOTEL AGENT] WARNING: Data file not found at {_HOTELS_FILE}. "
                f"Using empty dataset."
            )
            return []
        with _HOTELS_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        self._log(
            f"[HOTEL AGENT] Loaded {len(data)} hotel records from {_HOTELS_FILE.name}"
        )
        return data

    def _register(self) -> None:
        """Register this agent in the shared A2A discovery registry."""
        registry = get_registry()
        registry.register(self.agent_card)
        self._log(
            f"[HOTEL AGENT] Registered {self.AGENT_NAME!r} in A2ARegistry "
            f"with capabilities: {self.CAPABILITIES}"
        )

    def _validate_auth(self, task: A2ATask) -> bool:
        """
        Verify the auth token carried by the incoming task.

        Returns True if the token is valid (or auth is disabled), False otherwise.
        The token is verified against the sender's own agent_id because the
        A2AClient scopes tokens to the caller's identity, not the receiver's.
        """
        if not self.agent_card.auth_required:
            return True
        return verify_token(task.auth_token, task.sender_id)

    @staticmethod
    def _value_score(hotel: Dict[str, Any]) -> float:
        """
        Compute the value score for a hotel.

        value_score = stars / price_per_night

        Higher is better.  A 4-star hotel at $80/night scores
        4/80 = 0.05, beating a 3-star at $90/night (0.033).
        Guards against division by zero by returning 0.0 for zero-price hotels.
        """
        stars = float(hotel.get("stars", 0))
        price = float(hotel.get("price_per_night", 0))
        return round(stars / price, 6) if price > 0 else 0.0

    def _new_booking_ref(self, hotel_id: str) -> str:
        """Generate a deterministic booking reference for a hotel stay."""
        raw = f"{hotel_id}-{time.time()}"
        digest = hashlib.md5(raw.encode()).hexdigest()[:8].upper()
        return f"HB-{digest}"

    # ----------------------------------------------------------------
    # Task dispatcher  (A2A entry point)
    # ----------------------------------------------------------------

    def process_task(self, task: A2ATask) -> A2AResponse:
        """
        Main A2A entry point — receives a task dispatched by A2AClient and
        routes it to the appropriate handler method.

        Supported intents
        -----------------
        "find_hotels"  -> _handle_find_hotels(task)
        "book_hotel"   -> _handle_book_hotel(task)

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
            f"\n[HOTEL AGENT] ── Received task {task.task_id!r} "
            f"from {task.sender_id!r} | intent={task.intent!r}"
        )

        # Auth check
        if not self._validate_auth(task):
            self._log(f"[HOTEL AGENT] Auth FAILED for task {task.task_id!r}")
            return A2AResponse.error(
                task_id=task.task_id,
                agent_id=self.agent_id,
                message=(
                    f"Authentication failed: invalid or expired token "
                    f"from sender {task.sender_id!r}."
                ),
            )

        # Intent dispatch
        if task.intent == "find_hotels":
            return self._handle_find_hotels(task)
        elif task.intent == "book_hotel":
            return self._handle_book_hotel(task)
        else:
            return A2AResponse.error(
                task_id=task.task_id,
                agent_id=self.agent_id,
                message=(
                    f"HotelAgent does not handle intent {task.intent!r}. "
                    f"Supported intents: {self.SUPPORTED_TASKS}"
                ),
            )

    # ----------------------------------------------------------------
    # Intent handlers
    # ----------------------------------------------------------------

    def _handle_find_hotels(self, task: A2ATask) -> A2AResponse:
        """
        Search for available hotels matching the criteria in task.payload.

        Expected payload keys
        ---------------------
        city            : str   Destination city (e.g. "Paris", "London").
                                Matched case-insensitively against the hotel's
                                "city" field.
        nights          : int   Number of nights for the stay (used to compute
                                total hotel cost for budget comparison).
                                Optional; defaults to 1.
        budget_per_night: float Hard maximum nightly price in USD.
                                Optional; if omitted, no price ceiling applied.
        budget_total    : float Hard maximum TOTAL hotel cost (price_per_night
                                * nights).  Optional; used only when nights
                                is also supplied.
        min_stars       : int   Minimum star rating (1-5).
                                Optional; defaults to 1 (no minimum).

        Returns
        -------
        A2AResponse with result dict:
          {
            "hotels": [
              {
                "id":              str,
                "city":            str,
                "name":            str,
                "stars":           int,
                "price_per_night": float,
                "total_cost":      float,   # price_per_night * nights
                "available":       bool,
                "amenities":       list[str],
                "value_score":     float    # stars / price_per_night
              },
              ...  (up to MAX_RESULTS entries, sorted value_score descending)
            ],
            "query": { ... }   # echo of search parameters for traceability
          }
        """
        payload = task.payload

        city: str = str(payload.get("city", "")).strip()
        nights: int = max(1, int(payload.get("nights", 1)))
        budget_per_night: Optional[float] = (
            float(payload["budget_per_night"])
            if payload.get("budget_per_night") is not None
            else None
        )
        budget_total: Optional[float] = (
            float(payload["budget_total"])
            if payload.get("budget_total") is not None
            else None
        )
        min_stars: int = int(payload.get("min_stars", 1))

        self._log(f"[HOTEL AGENT] Searching hotels:")
        self._log(f"[HOTEL AGENT]   City             : {city!r}")
        self._log(f"[HOTEL AGENT]   Nights           : {nights}")
        self._log(
            f"[HOTEL AGENT]   Budget/Night     : "
            f"{'$' + f'{budget_per_night:.2f}' if budget_per_night is not None else 'any'}"
        )
        self._log(
            f"[HOTEL AGENT]   Budget Total     : "
            f"{'$' + f'{budget_total:.2f}' if budget_total is not None else 'any'}"
        )
        self._log(f"[HOTEL AGENT]   Min Stars        : {min_stars}")

        if not city:
            return A2AResponse.error(
                task_id=task.task_id,
                agent_id=self.agent_id,
                message="find_hotels requires a 'city' in the payload.",
            )

        # Work on a deep copy so original data is never mutated by filtering
        candidates = deepcopy(self._hotels)

        # ---- Filter: city ----
        city_lower = city.lower()
        candidates = [h for h in candidates if city_lower in h.get("city", "").lower()]

        # ---- Filter: availability ----
        candidates = [h for h in candidates if h.get("available", False)]

        # ---- Filter: minimum star rating ----
        candidates = [h for h in candidates if int(h.get("stars", 0)) >= min_stars]

        # ---- Filter: budget per night ----
        if budget_per_night is not None:
            candidates = [
                h
                for h in candidates
                if float(h.get("price_per_night", 0)) <= budget_per_night
            ]

        # ---- Filter: total budget ----
        if budget_total is not None:
            candidates = [
                h
                for h in candidates
                if float(h.get("price_per_night", 0)) * nights <= budget_total
            ]

        # ---- Annotate with computed fields ----
        for hotel in candidates:
            hotel["value_score"] = self._value_score(hotel)
            hotel["total_cost"] = round(
                float(hotel.get("price_per_night", 0)) * nights, 2
            )
            hotel["nights"] = nights

        # ---- Sort by value_score descending (best value first) ----
        candidates.sort(key=lambda h: h["value_score"], reverse=True)

        # ---- Limit results ----
        top_results = candidates[: self.MAX_RESULTS]

        self._log(
            f"[HOTEL AGENT] Found {len(top_results)} matching hotel(s) "
            f"(from {len(self._hotels)} total, after filtering)"
        )
        for i, ht in enumerate(top_results, 1):
            self._log(
                f"[HOTEL AGENT]   [{i}] {ht['name']:<30s} "
                f"{'★' * ht['stars']:<5s} "
                f"${ht['price_per_night']:.2f}/night  "
                f"total=${ht['total_cost']:.2f}  "
                f"score={ht['value_score']:.4f}"
            )

        if not top_results:
            return A2AResponse.error(
                task_id=task.task_id,
                agent_id=self.agent_id,
                message=(
                    f"No hotels found in {city!r} matching the given criteria "
                    f"(budget_per_night={budget_per_night}, min_stars={min_stars}). "
                    f"Try relaxing filters."
                ),
                result={"hotels": [], "query": payload},
            )

        best = top_results[0]
        return A2AResponse.success(
            task_id=task.task_id,
            agent_id=self.agent_id,
            result={
                "hotels": top_results,
                "query": {
                    "city": city,
                    "nights": nights,
                    "budget_per_night": budget_per_night,
                    "budget_total": budget_total,
                    "min_stars": min_stars,
                },
            },
            message=(
                f"Found {len(top_results)} hotel(s) in {city!r}. "
                f"Best value: {best['name']} ({best['stars']}★) at "
                f"${best['price_per_night']:.2f}/night "
                f"(total ${best['total_cost']:.2f} for {nights} night(s))."
            ),
        )

    def _handle_book_hotel(self, task: A2ATask) -> A2AResponse:
        """
        Confirm a hotel booking by hotel ID.

        Expected payload keys
        ---------------------
        hotel_id  : str   The "id" field of the hotel to book (e.g. "HT001").
        nights    : int   Number of nights for the stay.
                          Optional; defaults to 1.
        guest     : str   Name / identifier of the primary guest.
                          Optional; defaults to task.sender_id.

        Side-effects
        ------------
        Marks the hotel as unavailable (available=False) in the in-memory
        working copy to prevent double-booking within the same session.
        Stores the booking reference in self._bookings.

        Returns
        -------
        A2AResponse with result dict:
          {
            "booking_ref":  str,     # e.g. "HB-A4F2C1B3"
            "hotel":        dict,    # the booked hotel record (with total_cost)
            "nights":       int,
            "total_cost":   float,
            "guest":        str,
            "check_in":     str,     # echoed from payload or "TBD"
            "check_out":    str,     # echoed from payload or "TBD"
            "status":       "confirmed"
          }
        """
        payload = task.payload
        hotel_id: str = str(payload.get("hotel_id", "")).strip()
        nights: int = max(1, int(payload.get("nights", 1)))
        guest: str = str(payload.get("guest", task.sender_id)).strip()
        check_in: str = str(payload.get("check_in", "TBD"))
        check_out: str = str(payload.get("check_out", "TBD"))

        self._log(
            f"[HOTEL AGENT] Booking hotel {hotel_id!r} for {nights} night(s), "
            f"guest={guest!r}"
        )

        if not hotel_id:
            return A2AResponse.error(
                task_id=task.task_id,
                agent_id=self.agent_id,
                message="book_hotel requires a 'hotel_id' in the payload.",
            )

        # Locate the hotel in the working copy
        matched = next(
            (h for h in self._hotels if h.get("id") == hotel_id),
            None,
        )

        if matched is None:
            return A2AResponse.error(
                task_id=task.task_id,
                agent_id=self.agent_id,
                message=f"Hotel {hotel_id!r} not found in inventory.",
            )

        if not matched.get("available", False):
            return A2AResponse.error(
                task_id=task.task_id,
                agent_id=self.agent_id,
                message=(
                    f"Hotel {hotel_id!r} ({matched.get('name')}) is currently "
                    f"unavailable — it may already be booked."
                ),
            )

        # Confirm booking: mark as unavailable to prevent double-booking
        matched["available"] = False

        total_cost = round(float(matched.get("price_per_night", 0)) * nights, 2)
        booking_ref = self._new_booking_ref(hotel_id)
        self._bookings[hotel_id] = booking_ref

        # Build a clean snapshot of the booked hotel record
        booked_hotel = deepcopy(matched)
        booked_hotel["total_cost"] = total_cost
        booked_hotel["nights"] = nights
        booked_hotel["value_score"] = self._value_score(matched)

        self._log(f"[HOTEL AGENT] Booking CONFIRMED ✓")
        self._log(f"[HOTEL AGENT]   Booking Ref  : {booking_ref}")
        self._log(
            f"[HOTEL AGENT]   Hotel        : {matched['name']} ({matched['stars']}★)"
        )
        self._log(f"[HOTEL AGENT]   City         : {matched['city']}")
        self._log(f"[HOTEL AGENT]   Nights       : {nights}")
        self._log(
            f"[HOTEL AGENT]   Rate         : ${matched['price_per_night']:.2f}/night"
        )
        self._log(f"[HOTEL AGENT]   Total Cost   : ${total_cost:.2f}")
        self._log(f"[HOTEL AGENT]   Check-in     : {check_in}")
        self._log(f"[HOTEL AGENT]   Check-out    : {check_out}")
        self._log(f"[HOTEL AGENT]   Amenities    : {matched.get('amenities', [])}")

        return A2AResponse.success(
            task_id=task.task_id,
            agent_id=self.agent_id,
            result={
                "booking_ref": booking_ref,
                "hotel": booked_hotel,
                "nights": nights,
                "total_cost": total_cost,
                "guest": guest,
                "check_in": check_in,
                "check_out": check_out,
                "status": "confirmed",
            },
            message=(
                f"Hotel {hotel_id!r} booked successfully! "
                f"Ref: {booking_ref}. "
                f"{matched['name']} ({matched['stars']}★) in {matched['city']} "
                f"for {nights} night(s) — total ${total_cost:.2f}."
            ),
        )

    # ----------------------------------------------------------------
    # Utility / admin
    # ----------------------------------------------------------------

    def get_hotel(self, hotel_id: str) -> Optional[Dict[str, Any]]:
        """Return a copy of the hotel record for the given ID, or None."""
        matched = next((h for h in self._hotels if h.get("id") == hotel_id), None)
        return deepcopy(matched) if matched else None

    def get_booking(self, hotel_id: str) -> Optional[str]:
        """Return the booking reference for a confirmed hotel stay, or None."""
        return self._bookings.get(hotel_id)

    def list_hotels(self, city: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Return a deep copy of all loaded hotel records.
        If city is provided, filter to only that city (case-insensitive).
        """
        hotels = deepcopy(self._hotels)
        if city:
            city_lower = city.lower()
            hotels = [h for h in hotels if city_lower in h.get("city", "").lower()]
        return hotels

    def __repr__(self) -> str:
        return (
            f"HotelAgent(id={self.agent_id!r}, "
            f"hotels={len(self._hotels)}, "
            f"bookings={len(self._bookings)})"
        )
