"""
agents/car_rental_agent.py
==========================
CarRentalAgent — A2A Server Agent for Car Rental Search and Booking
--------------------------------------------------------------------
The CarRentalAgent is a specialist A2A server that handles all car-rental
related tasks within the TravelMind system.  It is discovered by the
OrchestratorAgent at runtime via the shared A2ARegistry using the capability
strings "find_cars" and "rent_car".

Car rental is treated as an *optional* add-on to the core trip.  The
OrchestratorAgent queries this agent only when the user's constraints
include car rental (e.g. constraints["include_car_rental"] == True) or
when budget headroom remains after flights and hotels are selected.

Capabilities
------------
  find_cars  : Search the mock car-rental database for available vehicles
               in a destination city matching a daily budget and car type
               preference.  Returns options sorted by price ascending.

  rent_car   : Confirm a rental reservation for a specific car by its ID
               and rental duration in days.  Marks the car as unavailable
               in the working copy and returns a rental confirmation record.

A2A Lifecycle
-------------
  On instantiation the agent:
    1. Loads the mock car data from data/cars.json.
    2. Creates and publishes its AgentCard to the shared A2ARegistry so the
       OrchestratorAgent can discover it without a hard-coded reference.

  On each process_task(task) call:
    1. Validates the auth_token via the a2a.auth module.
    2. Dispatches to the appropriate handler based on task.intent.
    3. Returns a structured A2AResponse (success or error).

Data file
---------
  data/cars.json — list of car objects with fields:
    id, city, type, company, price_per_day, available

No external dependencies — stdlib only (json, pathlib, copy, hashlib, time).
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
_CARS_FILE = _DATA_DIR / "cars.json"


# ---------------------------------------------------------------------------
# CarRentalAgent
# ---------------------------------------------------------------------------


class CarRentalAgent:
    """
    A2A server agent responsible for searching and reserving rental cars.

    The agent loads car data once at startup (from data/cars.json) and
    keeps an in-memory working copy so that confirmed rentals (availability
    updates) are reflected across subsequent calls within the same session.

    Cars are ranked by daily price ascending — the cheapest matching option
    is always presented first.  An optional ``car_type`` filter lets callers
    narrow results to "Economy", "Compact", "SUV", etc.

    Parameters
    ----------
    agent_id : str
        Unique identifier for this agent instance.
        Defaults to "car-rental-agent-01".
    verbose : bool
        When True (default) the agent prints [CAR RENTAL AGENT] diagnostic
        lines in addition to the standard [A2A] log lines emitted by the
        client.
    auto_register : bool
        When True (default) the agent registers itself in the shared global
        A2ARegistry on instantiation.
    """

    # ----------------------------------------------------------------
    # Class-level constants
    # ----------------------------------------------------------------

    AGENT_ID: str = "car-rental-agent-01"
    AGENT_NAME: str = "CarRentalAgent"
    AGENT_DESCRIPTION: str = (
        "Specialist agent for searching available rental cars and confirming "
        "car rental reservations.  Accepts find_cars and rent_car tasks via A2A."
    )
    CAPABILITIES: List[str] = ["find_cars", "rent_car"]
    SUPPORTED_TASKS: List[str] = ["find_cars", "rent_car"]
    ENDPOINT: str = "https://agents.travelmind.internal/car-rental/v1"

    # Maximum number of results returned for a find_cars query
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

        # Load car rental data from JSON
        self._cars: List[Dict[str, Any]] = self._load_cars()
        # In-memory rental ledger: { car_id -> rental_ref }
        self._rentals: Dict[str, str] = {}

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

    def _load_cars(self) -> List[Dict[str, Any]]:
        """Load and return car rental records from the JSON data file."""
        if not _CARS_FILE.exists():
            self._log(
                f"[CAR RENTAL AGENT] WARNING: Data file not found at {_CARS_FILE}. "
                f"Using empty dataset."
            )
            return []
        with _CARS_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        self._log(
            f"[CAR RENTAL AGENT] Loaded {len(data)} car rental records from "
            f"{_CARS_FILE.name}"
        )
        return data

    def _register(self) -> None:
        """Register this agent in the shared A2A discovery registry."""
        registry = get_registry()
        registry.register(self.agent_card)
        self._log(
            f"[CAR RENTAL AGENT] Registered {self.AGENT_NAME!r} in A2ARegistry "
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

    def _new_rental_ref(self, car_id: str) -> str:
        """Generate a unique rental confirmation reference for a car booking."""
        raw = f"{car_id}-{time.time()}"
        digest = hashlib.md5(raw.encode()).hexdigest()[:8].upper()
        return f"CR-{digest}"

    @staticmethod
    def _daily_value_score(car: Dict[str, Any]) -> float:
        """
        Compute a simple value score for a car: 1 / price_per_day.
        Higher score = cheaper per day.  Used as a secondary sort key.
        Guards against division by zero.
        """
        price = float(car.get("price_per_day", 0))
        return round(1.0 / price, 6) if price > 0 else 0.0

    # ----------------------------------------------------------------
    # Task dispatcher  (A2A entry point)
    # ----------------------------------------------------------------

    def process_task(self, task: A2ATask) -> A2AResponse:
        """
        Main A2A entry point — receives a task dispatched by A2AClient and
        routes it to the appropriate handler method.

        Supported intents
        -----------------
        "find_cars"  -> _handle_find_cars(task)
        "rent_car"   -> _handle_rent_car(task)

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
            f"\n[CAR RENTAL AGENT] ── Received task {task.task_id!r} "
            f"from {task.sender_id!r} | intent={task.intent!r}"
        )

        # Auth check
        if not self._validate_auth(task):
            self._log(f"[CAR RENTAL AGENT] Auth FAILED for task {task.task_id!r}")
            return A2AResponse.error(
                task_id=task.task_id,
                agent_id=self.agent_id,
                message=(
                    f"Authentication failed: invalid or expired token "
                    f"from sender {task.sender_id!r}."
                ),
            )

        # Intent dispatch
        if task.intent == "find_cars":
            return self._handle_find_cars(task)
        elif task.intent == "rent_car":
            return self._handle_rent_car(task)
        else:
            return A2AResponse.error(
                task_id=task.task_id,
                agent_id=self.agent_id,
                message=(
                    f"CarRentalAgent does not handle intent {task.intent!r}. "
                    f"Supported intents: {self.SUPPORTED_TASKS}"
                ),
            )

    # ----------------------------------------------------------------
    # Intent handlers
    # ----------------------------------------------------------------

    def _handle_find_cars(self, task: A2ATask) -> A2AResponse:
        """
        Search for available rental cars matching the criteria in task.payload.

        Expected payload keys
        ---------------------
        city             : str    Destination city (e.g. "Paris", "London").
                                  Matched case-insensitively against the car's
                                  "city" field.
        days             : int    Number of rental days (used to compute total
                                  rental cost for budget comparison).
                                  Optional; defaults to 1.
        budget_per_day   : float  Hard maximum daily rental price in USD.
                                  Optional; if omitted, no ceiling is applied.
        budget_total     : float  Hard maximum TOTAL rental cost
                                  (price_per_day * days).
                                  Optional; used only when days is also supplied.
        car_type         : str    Preferred car category (e.g. "Economy",
                                  "Compact", "SUV").  Case-insensitive partial
                                  match.  Optional; if omitted, all types returned.

        Returns
        -------
        A2AResponse with result dict:
          {
            "cars": [
              {
                "id":            str,
                "city":          str,
                "type":          str,
                "company":       str,
                "price_per_day": float,
                "total_cost":    float,   # price_per_day * days
                "days":          int,
                "available":     bool,
                "value_score":   float    # 1 / price_per_day
              },
              ...  (up to MAX_RESULTS entries, sorted price_per_day ascending)
            ],
            "query": { ... }   # echo of search parameters for traceability
          }
        """
        payload = task.payload

        city: str = str(payload.get("city", "")).strip()
        days: int = max(1, int(payload.get("days", 1)))
        budget_per_day: Optional[float] = (
            float(payload["budget_per_day"])
            if payload.get("budget_per_day") is not None
            else None
        )
        budget_total: Optional[float] = (
            float(payload["budget_total"])
            if payload.get("budget_total") is not None
            else None
        )
        car_type: Optional[str] = (
            str(payload["car_type"]).strip() if payload.get("car_type") else None
        )

        self._log(f"[CAR RENTAL AGENT] Searching rental cars:")
        self._log(f"[CAR RENTAL AGENT]   City           : {city!r}")
        self._log(f"[CAR RENTAL AGENT]   Days           : {days}")
        self._log(
            f"[CAR RENTAL AGENT]   Budget/Day     : "
            f"{'$' + f'{budget_per_day:.2f}' if budget_per_day is not None else 'any'}"
        )
        self._log(
            f"[CAR RENTAL AGENT]   Budget Total   : "
            f"{'$' + f'{budget_total:.2f}' if budget_total is not None else 'any'}"
        )
        self._log(
            f"[CAR RENTAL AGENT]   Car Type       : {car_type if car_type else 'any'}"
        )

        if not city:
            return A2AResponse.error(
                task_id=task.task_id,
                agent_id=self.agent_id,
                message="find_cars requires a 'city' in the payload.",
            )

        # Work on a deep copy so original data is never mutated by filtering
        candidates = deepcopy(self._cars)

        # ---- Filter: city ----
        city_lower = city.lower()
        candidates = [c for c in candidates if city_lower in c.get("city", "").lower()]

        # ---- Filter: availability ----
        candidates = [c for c in candidates if c.get("available", False)]

        # ---- Filter: car type (partial match) ----
        if car_type:
            type_lower = car_type.lower()
            candidates = [
                c for c in candidates if type_lower in c.get("type", "").lower()
            ]

        # ---- Filter: budget per day ----
        if budget_per_day is not None:
            candidates = [
                c
                for c in candidates
                if float(c.get("price_per_day", 0)) <= budget_per_day
            ]

        # ---- Filter: total budget ----
        if budget_total is not None:
            candidates = [
                c
                for c in candidates
                if float(c.get("price_per_day", 0)) * days <= budget_total
            ]

        # ---- Annotate with computed fields ----
        for car in candidates:
            car["value_score"] = self._daily_value_score(car)
            car["total_cost"] = round(float(car.get("price_per_day", 0)) * days, 2)
            car["days"] = days

        # ---- Sort by price_per_day ascending (cheapest first) ----
        candidates.sort(key=lambda c: float(c.get("price_per_day", 0)))

        # ---- Limit results ----
        top_results = candidates[: self.MAX_RESULTS]

        self._log(
            f"[CAR RENTAL AGENT] Found {len(top_results)} matching car(s) "
            f"(from {len(self._cars)} total, after filtering)"
        )
        for i, car in enumerate(top_results, 1):
            self._log(
                f"[CAR RENTAL AGENT]   [{i}] {car['company']:<14s} "
                f"{car['type']:<10s} "
                f"${car['price_per_day']:.2f}/day  "
                f"total=${car['total_cost']:.2f} for {days}d  "
                f"city={car['city']}"
            )

        if not top_results:
            return A2AResponse.error(
                task_id=task.task_id,
                agent_id=self.agent_id,
                message=(
                    f"No rental cars found in {city!r} matching the given criteria "
                    f"(budget_per_day={budget_per_day}, car_type={car_type!r}). "
                    f"Try relaxing filters or choosing a different city."
                ),
                result={"cars": [], "query": payload},
            )

        cheapest = top_results[0]
        return A2AResponse.success(
            task_id=task.task_id,
            agent_id=self.agent_id,
            result={
                "cars": top_results,
                "query": {
                    "city": city,
                    "days": days,
                    "budget_per_day": budget_per_day,
                    "budget_total": budget_total,
                    "car_type": car_type,
                },
            },
            message=(
                f"Found {len(top_results)} rental car(s) in {city!r}. "
                f"Cheapest: {cheapest['company']} {cheapest['type']} at "
                f"${cheapest['price_per_day']:.2f}/day "
                f"(total ${cheapest['total_cost']:.2f} for {days} day(s))."
            ),
        )

    def _handle_rent_car(self, task: A2ATask) -> A2AResponse:
        """
        Confirm a car rental reservation by car ID.

        Expected payload keys
        ---------------------
        car_id      : str   The "id" field of the car to rent (e.g. "CR001").
        days        : int   Number of rental days.
                            Optional; defaults to 1.
        driver      : str   Name / identifier of the primary driver.
                            Optional; defaults to task.sender_id.
        pickup_date : str   Desired pickup date in YYYY-MM-DD format.
                            Optional; echoed in the confirmation record.
        dropoff_date: str   Desired drop-off date.
                            Optional; echoed in the confirmation record.

        Side-effects
        ------------
        Marks the car as unavailable (available=False) in the in-memory
        working copy to prevent double-booking within the same session.
        Stores the rental reference in self._rentals.

        Returns
        -------
        A2AResponse with result dict:
          {
            "rental_ref":    str,    # e.g. "CR-A4F2C1B3"
            "car":           dict,   # the rented car record (with total_cost)
            "days":          int,
            "total_cost":    float,
            "driver":        str,
            "pickup_date":   str,
            "dropoff_date":  str,
            "status":        "confirmed"
          }
        """
        payload = task.payload
        car_id: str = str(payload.get("car_id", "")).strip()
        days: int = max(1, int(payload.get("days", 1)))
        driver: str = str(payload.get("driver", task.sender_id)).strip()
        pickup_date: str = str(payload.get("pickup_date", "TBD"))
        dropoff_date: str = str(payload.get("dropoff_date", "TBD"))

        self._log(
            f"[CAR RENTAL AGENT] Renting car {car_id!r} for {days} day(s), "
            f"driver={driver!r}"
        )

        if not car_id:
            return A2AResponse.error(
                task_id=task.task_id,
                agent_id=self.agent_id,
                message="rent_car requires a 'car_id' in the payload.",
            )

        # Locate the car in the working copy
        matched = next(
            (c for c in self._cars if c.get("id") == car_id),
            None,
        )

        if matched is None:
            return A2AResponse.error(
                task_id=task.task_id,
                agent_id=self.agent_id,
                message=f"Car {car_id!r} not found in rental inventory.",
            )

        if not matched.get("available", False):
            return A2AResponse.error(
                task_id=task.task_id,
                agent_id=self.agent_id,
                message=(
                    f"Car {car_id!r} ({matched.get('company')} {matched.get('type')}) "
                    f"is currently unavailable — it may already be reserved."
                ),
            )

        # Confirm rental: mark as unavailable to prevent double-booking
        matched["available"] = False

        total_cost = round(float(matched.get("price_per_day", 0)) * days, 2)
        rental_ref = self._new_rental_ref(car_id)
        self._rentals[car_id] = rental_ref

        # Build a clean snapshot of the rented car record
        rented_car = deepcopy(matched)
        rented_car["total_cost"] = total_cost
        rented_car["days"] = days
        rented_car["value_score"] = self._daily_value_score(matched)

        self._log(f"[CAR RENTAL AGENT] Rental CONFIRMED ✓")
        self._log(f"[CAR RENTAL AGENT]   Rental Ref   : {rental_ref}")
        self._log(
            f"[CAR RENTAL AGENT]   Car          : "
            f"{matched['company']} {matched['type']}"
        )
        self._log(f"[CAR RENTAL AGENT]   City         : {matched['city']}")
        self._log(f"[CAR RENTAL AGENT]   Days         : {days}")
        self._log(
            f"[CAR RENTAL AGENT]   Rate         : ${matched['price_per_day']:.2f}/day"
        )
        self._log(f"[CAR RENTAL AGENT]   Total Cost   : ${total_cost:.2f}")
        self._log(f"[CAR RENTAL AGENT]   Pick-up      : {pickup_date}")
        self._log(f"[CAR RENTAL AGENT]   Drop-off     : {dropoff_date}")

        return A2AResponse.success(
            task_id=task.task_id,
            agent_id=self.agent_id,
            result={
                "rental_ref": rental_ref,
                "car": rented_car,
                "days": days,
                "total_cost": total_cost,
                "driver": driver,
                "pickup_date": pickup_date,
                "dropoff_date": dropoff_date,
                "status": "confirmed",
            },
            message=(
                f"Car {car_id!r} rented successfully! "
                f"Ref: {rental_ref}. "
                f"{matched['company']} {matched['type']} in {matched['city']} "
                f"for {days} day(s) — total ${total_cost:.2f}."
            ),
        )

    # ----------------------------------------------------------------
    # Utility / admin
    # ----------------------------------------------------------------

    def get_car(self, car_id: str) -> Optional[Dict[str, Any]]:
        """Return a copy of the car record for the given ID, or None."""
        matched = next((c for c in self._cars if c.get("id") == car_id), None)
        return deepcopy(matched) if matched else None

    def get_rental(self, car_id: str) -> Optional[str]:
        """Return the rental reference for a confirmed car reservation, or None."""
        return self._rentals.get(car_id)

    def list_cars(self, city: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Return a deep copy of all loaded car records.
        If city is provided, filter to only that city (case-insensitive).
        """
        cars = deepcopy(self._cars)
        if city:
            city_lower = city.lower()
            cars = [c for c in cars if city_lower in c.get("city", "").lower()]
        return cars

    def available_cars(self, city: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Return only available car records, optionally filtered by city.
        Sorted by price_per_day ascending.
        """
        cars = [c for c in self.list_cars(city) if c.get("available", False)]
        cars.sort(key=lambda c: float(c.get("price_per_day", 0)))
        return cars

    def __repr__(self) -> str:
        return (
            f"CarRentalAgent(id={self.agent_id!r}, "
            f"cars={len(self._cars)}, "
            f"rentals={len(self._rentals)})"
        )
