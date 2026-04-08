"""
Scheduling Agent — A2A Sub-Agent for Appointment Management
============================================================
PROTOCOL ROLES:
  • A2A Server   — This agent registers an AgentCard and exposes a
                   process_task() endpoint that the OrchestratorAgent calls.
  • MCP Client   — Internally, this agent uses an MCPClient to reach the
                   MCP Server for calendar/slot tools and appointment booking.

RESPONSIBILITIES:
-----------------
The SchedulingAgent handles all appointment-related tasks in the MediAssist
system. It is the *only* agent that is allowed to write new appointments to
the data store — all other agents are read-only.

Supported A2A intents (declared in its AgentCard capabilities):
  • "schedule_appointment"  — Book a new appointment for a patient.
  • "check_availability"    — List open time slots for a doctor on a date.

FLOW FOR "schedule_appointment":
  1. OrchestratorAgent discovers this agent via A2ARegistry.
  2. Orchestrator sends an A2ATask with intent="schedule_appointment" and
     payload={"patient_id": ..., "date": ..., "time": ..., "doctor": ...}
  3. process_task() receives the task.
  4. Agent calls MCP tool "list_available_slots" to verify the slot is free.
  5. Agent calls MCP tool "book_appointment" to persist the booking.
  6. Agent returns an A2AResponse with the confirmed appointment details.

FLOW FOR "check_availability":
  1. Orchestrator sends A2ATask with intent="check_availability" and
     payload={"doctor": ..., "date": ...}
  2. process_task() calls MCP tool "list_available_slots".
  3. Returns A2AResponse listing all open slots.
"""

from datetime import datetime
from typing import Any, Dict

from a2a.a2a_protocol import A2AResponse, A2ATask
from a2a.agent_card import AgentCard
from mcp.mcp_client import MCPClient
from mcp.mcp_server import MCPServer

# ---------------------------------------------------------------------------
# ANSI colour codes for console output
# ---------------------------------------------------------------------------
_TEAL = "\033[36m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_RED = "\033[91m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RESET = "\033[0m"


def _log(agent_name: str, text: str) -> None:
    """Print a formatted agent log line."""
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"{_TEAL}{_BOLD}[{agent_name}]{_RESET} [{ts}] {text}")


# ---------------------------------------------------------------------------
# SchedulingAgent
# ---------------------------------------------------------------------------


class SchedulingAgent:
    """
    A2A Sub-Agent — Appointment Scheduling Specialist.

    PROTOCOL ROLE: A2A Server (sub-agent / skill agent)
    ----------------------------------------------------
    In A2A terminology, this agent acts as a *server* — it exposes a
    process_task() method that the Orchestrator calls (via A2AClient).
    It has an AgentCard that advertises:
      - What it can do   (capabilities)
      - Where to reach it (endpoint — simulated URL)
      - How to auth      (auth_scheme)

    The Orchestrator discovers this agent dynamically at runtime via the
    A2ARegistry — it does NOT hard-code a direct import or call. This means
    the SchedulingAgent could be swapped, upgraded, or replaced without
    touching the OrchestratorAgent code at all.

    PROTOCOL ROLE: MCP Client (data access)
    ----------------------------------------
    Internally, this agent uses an MCPClient to access the two scheduling-
    related tools on the MCP Server:
      • list_available_slots(doctor, date)   — calendar read
      • book_appointment(patient_id, date, time, doctor) — calendar write

    It never touches the JSON data files directly; all data access goes
    through the MCP layer. This enforces clean separation:
      OrchestratorAgent
          ──[A2A]──▶ SchedulingAgent.process_task()
                          ──[MCP]──▶ MCPServer tools
                                          ──▶ appointments.json / patients.json

    Attributes:
        agent_card  : This agent's A2A identity and capability declaration.
        _mcp_client : The MCP client used to call scheduling tools.
        _name       : Short display name used in log output.
    """

    # ------------------------------------------------------------------
    # A2A AgentCard — published at construction time so the
    # OrchestratorAgent can register it in the A2ARegistry immediately.
    # ------------------------------------------------------------------
    AGENT_ID = "scheduling-agent-001"
    AGENT_NAME = "SchedulingAgent"
    ENDPOINT = "http://localhost:8001/a2a"  # simulated — not a real server

    def __init__(self, mcp_server: MCPServer) -> None:
        """
        Initialise the SchedulingAgent.

        Creates the agent's AgentCard (A2A identity) and connects an
        MCPClient to the provided MCP Server (data access layer).

        Args:
            mcp_server: The shared MCPServer instance. All sub-agents share
                        the same server to access the same data store —
                        mirroring how multiple microservices might share a
                        central database or API gateway.
        """
        self._name = self.AGENT_NAME

        # ── A2A: AgentCard ────────────────────────────────────────────
        # This card is what the OrchestratorAgent registers in the
        # A2ARegistry so other agents can discover us. The capabilities
        # list is the discovery key — when the orchestrator asks for
        # "schedule_appointment", the registry finds THIS card.
        self.agent_card = AgentCard(
            agent_id=self.AGENT_ID,
            name=self.AGENT_NAME,
            description=(
                "Specialist agent for patient appointment scheduling. "
                "Can check doctor availability and book confirmed appointments."
            ),
            capabilities=["schedule_appointment", "check_availability"],
            endpoint=self.ENDPOINT,
            version="1.0.0",
            auth_scheme="Bearer",
            metadata={
                "max_booking_window_days": 90,
                "supported_doctors": "all",
                "booking_confirmation": "immediate",
            },
        )

        # ── MCP: Client connection ────────────────────────────────────
        # Each agent owns its own MCPClient instance. In a real deployment
        # each agent process would open its own transport connection to the
        # MCP Server. client_id appears in all [MCP] log lines so you can
        # see which agent initiated each tool call.
        self._mcp_client = MCPClient(
            server=mcp_server,
            client_id=self.AGENT_NAME,
        )

        _log(
            self._name, f"{_GREEN}Initialised.{_RESET} AgentCard ready. MCP connected."
        )

    # ------------------------------------------------------------------
    # A2A task handler — the "endpoint" called by A2AClient
    # ------------------------------------------------------------------

    def process_task(self, task: A2ATask) -> A2AResponse:
        """
        Handle an incoming A2ATask and return an A2AResponse.

        PROTOCOL STEP: A2A Task Processing
        ------------------------------------
        This method IS the A2A "endpoint" for this agent. In a real A2A
        deployment, this logic would sit behind an HTTP POST /a2a handler.
        The A2AClient on the orchestrator side sends a serialised A2ATask;
        this method deserialises it, does the work, and returns a serialised
        A2AResponse.

        The method dispatches based on task.intent — each intent maps to a
        private handler method that uses the MCP client for data access.

        Supported intents:
          • "schedule_appointment" — book a new appointment
          • "check_availability"   — list open slots for a doctor+date

        Args:
            task: The A2ATask received from the OrchestratorAgent (via
                  A2AClient.send_task). Contains intent + payload dict.

        Returns:
            A2AResponse with status="completed" on success or
            status="failed" on any error, always with a descriptive message.
        """
        _log(
            self._name,
            f"{_YELLOW}▶ TASK RECEIVED{_RESET}  "
            f"task_id={task.task_id[:8]}...  "
            f"intent={task.intent!r}  "
            f"payload_keys={list(task.payload.keys())}",
        )

        # Route to the appropriate intent handler
        try:
            if task.intent == "schedule_appointment":
                return self._handle_schedule_appointment(task)
            elif task.intent == "check_availability":
                return self._handle_check_availability(task)
            else:
                # Unknown intent — return a structured error response
                # (never raise in a task handler; wrap all errors in responses)
                msg = (
                    f"Unknown intent '{task.intent}'. "
                    f"Supported intents: schedule_appointment, check_availability"
                )
                _log(self._name, f"{_RED}✗ UNKNOWN INTENT:{_RESET} {msg}")
                return A2AResponse(
                    task_id=task.task_id,
                    agent_id=self.AGENT_ID,
                    status="failed",
                    result={},
                    message=msg,
                )
        except Exception as exc:  # noqa: BLE001
            # Catch-all: never let an unhandled exception crash the
            # orchestrator. Surface it as a failed A2AResponse instead.
            msg = f"SchedulingAgent encountered an unexpected error: {exc}"
            _log(self._name, f"{_RED}✗ ERROR:{_RESET} {msg}")
            return A2AResponse(
                task_id=task.task_id,
                agent_id=self.AGENT_ID,
                status="failed",
                result={"error": str(exc)},
                message=msg,
            )

    # ------------------------------------------------------------------
    # Intent handlers
    # ------------------------------------------------------------------

    def _handle_schedule_appointment(self, task: A2ATask) -> A2AResponse:
        """
        Handle the "schedule_appointment" intent.

        STEPS:
          1. Extract and validate required payload fields.
          2. [MCP] Call list_available_slots to verify the requested time
             is actually free (guard against double-booking).
          3. [MCP] Call book_appointment to persist the new appointment.
          4. Return A2AResponse with full booking confirmation.

        Expected payload keys:
            patient_id : str  — e.g. "P001"
            date       : str  — YYYY-MM-DD
            time       : str  — HH:MM (24-hour)
            doctor     : str  — e.g. "Dr. Lee"

        Args:
            task: The A2ATask containing booking parameters in payload.

        Returns:
            A2AResponse with the confirmed appointment in result["appointment"],
            or a failed response if the slot is unavailable or payload is invalid.
        """
        payload = task.payload

        # ── Validate required fields ──────────────────────────────────
        required = ["patient_id", "date", "time", "doctor"]
        missing = [f for f in required if f not in payload]
        if missing:
            msg = f"Missing required payload fields: {missing}"
            _log(self._name, f"{_RED}✗ VALIDATION ERROR:{_RESET} {msg}")
            return A2AResponse(
                task_id=task.task_id,
                agent_id=self.AGENT_ID,
                status="failed",
                result={"missing_fields": missing},
                message=msg,
            )

        patient_id = payload["patient_id"]
        date = payload["date"]
        time_slot = payload["time"]
        doctor = payload["doctor"]

        _log(
            self._name,
            f"Processing appointment request: "
            f"patient={patient_id!r}, doctor={doctor!r}, "
            f"date={date!r}, time={time_slot!r}",
        )

        # ── Step 1: Check slot availability via MCP ───────────────────
        # WHY: Before booking we verify the requested slot is actually free.
        # This prevents double-bookings and gives a helpful error if the
        # time is already taken.
        _log(self._name, f"{_YELLOW}→ [MCP] Checking slot availability ...{_RESET}")
        slots_result = self._mcp_client.call_tool(
            "list_available_slots",
            doctor=doctor,
            date=date,
        )

        if slots_result["status"] != "ok":
            msg = f"Could not retrieve availability: {slots_result['message']}"
            _log(self._name, f"{_RED}✗ MCP ERROR:{_RESET} {msg}")
            return A2AResponse(
                task_id=task.task_id,
                agent_id=self.AGENT_ID,
                status="failed",
                result=slots_result,
                message=msg,
            )

        available_slots = slots_result["data"]["available_slots"]
        _log(
            self._name,
            f"Available slots for {doctor} on {date}: {available_slots}",
        )

        # Check if the requested time is among the available slots
        if time_slot not in available_slots:
            msg = (
                f"Requested time slot {time_slot!r} is not available for "
                f"{doctor} on {date}. "
                f"Available slots: {available_slots}"
            )
            _log(self._name, f"{_RED}✗ SLOT UNAVAILABLE:{_RESET} {msg}")
            return A2AResponse(
                task_id=task.task_id,
                agent_id=self.AGENT_ID,
                status="failed",
                result={
                    "requested_slot": time_slot,
                    "available_slots": available_slots,
                },
                message=msg,
            )

        _log(
            self._name,
            f"{_GREEN}✓ Slot {time_slot!r} is available.{_RESET} Proceeding to book ...",
        )

        # ── Step 2: Book the appointment via MCP ──────────────────────
        # WHY: Only after confirming availability do we write to the store.
        # The MCP Server's book_appointment tool generates a unique ID,
        # timestamps the booking, and persists it to appointments.json.
        _log(self._name, f"{_YELLOW}→ [MCP] Booking appointment ...{_RESET}")
        booking_result = self._mcp_client.call_tool(
            "book_appointment",
            patient_id=patient_id,
            date=date,
            time=time_slot,
            doctor=doctor,
        )

        if booking_result["status"] != "ok":
            msg = f"Booking failed: {booking_result['message']}"
            _log(self._name, f"{_RED}✗ BOOKING FAILED:{_RESET} {msg}")
            return A2AResponse(
                task_id=task.task_id,
                agent_id=self.AGENT_ID,
                status="failed",
                result=booking_result,
                message=msg,
            )

        # ── Booking confirmed ─────────────────────────────────────────
        appointment = booking_result["data"]
        appt_id = appointment.get("id", "N/A")
        msg = (
            f"Appointment {appt_id} confirmed for patient {patient_id} "
            f"with {doctor} on {date} at {time_slot}."
        )
        _log(self._name, f"{_GREEN}✓ BOOKING CONFIRMED:{_RESET} {msg}")

        return A2AResponse(
            task_id=task.task_id,
            agent_id=self.AGENT_ID,
            status="completed",
            result={
                "appointment": appointment,
                "available_slots_remaining": [
                    s for s in available_slots if s != time_slot
                ],
            },
            message=msg,
        )

    def _handle_check_availability(self, task: A2ATask) -> A2AResponse:
        """
        Handle the "check_availability" intent.

        STEPS:
          1. Extract and validate required payload fields (doctor, date).
          2. [MCP] Call list_available_slots to get open time slots.
          3. Return A2AResponse with the list of available slots.

        Expected payload keys:
            doctor : str — e.g. "Dr. Smith"
            date   : str — YYYY-MM-DD

        Args:
            task: The A2ATask containing doctor and date in payload.

        Returns:
            A2AResponse with available slots in result["available_slots"],
            or a failed response if the payload is invalid or MCP errors.
        """
        payload = task.payload

        # ── Validate required fields ──────────────────────────────────
        required = ["doctor", "date"]
        missing = [f for f in required if f not in payload]
        if missing:
            msg = f"Missing required payload fields: {missing}"
            _log(self._name, f"{_RED}✗ VALIDATION ERROR:{_RESET} {msg}")
            return A2AResponse(
                task_id=task.task_id,
                agent_id=self.AGENT_ID,
                status="failed",
                result={"missing_fields": missing},
                message=msg,
            )

        doctor = payload["doctor"]
        date = payload["date"]

        _log(
            self._name,
            f"Checking availability for {doctor!r} on {date!r} ...",
        )

        # ── Query MCP for available slots ─────────────────────────────
        _log(self._name, f"{_YELLOW}→ [MCP] Fetching available slots ...{_RESET}")
        slots_result = self._mcp_client.call_tool(
            "list_available_slots",
            doctor=doctor,
            date=date,
        )

        if slots_result["status"] != "ok":
            msg = f"Could not retrieve availability: {slots_result['message']}"
            _log(self._name, f"{_RED}✗ MCP ERROR:{_RESET} {msg}")
            return A2AResponse(
                task_id=task.task_id,
                agent_id=self.AGENT_ID,
                status="failed",
                result=slots_result,
                message=msg,
            )

        available = slots_result["data"]["available_slots"]
        count = len(available)
        msg = f"Found {count} available slot(s) for {doctor} on {date}: {available}"
        _log(self._name, f"{_GREEN}✓ AVAILABILITY RETRIEVED:{_RESET} {count} slot(s)")

        return A2AResponse(
            task_id=task.task_id,
            agent_id=self.AGENT_ID,
            status="completed",
            result={
                "doctor": doctor,
                "date": date,
                "available_slots": available,
                "total_available": count,
            },
            message=msg,
        )

    # ------------------------------------------------------------------
    # Dunder helpers
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"SchedulingAgent(agent_id={self.AGENT_ID!r}, endpoint={self.ENDPOINT!r})"
        )
