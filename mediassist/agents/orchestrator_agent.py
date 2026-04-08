"""
Orchestrator Agent — The Central Brain of the MediAssist Multi-Agent System
============================================================================
PROTOCOL ROLES:
  • A2A Client      — Uses A2AClient to send tasks to sub-agents discovered
                      via the A2ARegistry.
  • A2A Coordinator — Owns the A2ARegistry; registers all sub-agents at
                      startup and performs capability-based discovery.
  • MCP Indirect    — Does NOT use MCP directly. All data access is delegated
                      to the specialised sub-agents that each own their own
                      MCPClient. This is deliberate: the Orchestrator's job
                      is coordination, not data access.

RESPONSIBILITIES:
-----------------
The OrchestratorAgent is the single entry point for all user-facing requests.
It acts as the "clinical coordinator" — it understands WHAT needs to be done
but delegates the HOW to the appropriate specialist agents via A2A.

Supported request types:
  • "full_checkup"       — Calls ALL three sub-agents in parallel (fan-out),
                           then combines their results into a unified patient
                           summary report.
  • "book_appointment"   — Discovers the SchedulingAgent via the A2ARegistry
                           and delegates the booking task to it.
  • "prescriptions"      — Discovers the PharmacyAgent via the A2ARegistry
                           and delegates the prescription status check.

A2A PROTOCOL FLOW:
------------------
  User/Application
      │
      │  orchestrator.handle_request(patient_id, request_type, **kwargs)
      ▼
  OrchestratorAgent
      │
      │  1. Query A2ARegistry.discover(capability)
      │     → Returns AgentCard for the matching sub-agent
      │
      │  2. Build A2ATask(intent=..., payload={patient_id, ...})
      │
      │  3. A2AClient.send_task(agent_card, task, agent.process_task)
      │     → [A2A] log lines emitted here
      │     → Sub-agent.process_task(task) is called
      │       → [MCP] log lines emitted here (inside sub-agent)
      │     → A2AResponse is returned
      │
      │  4. Combine / display results
      ▼
  Combined Result Dict

WHY THE ORCHESTRATOR DOESN'T USE MCP DIRECTLY:
-----------------------------------------------
This is a key architectural decision. In a well-designed multi-agent system:
  - Each sub-agent is an expert in its domain AND in the tools needed for it.
  - The orchestrator is an expert in ROUTING and COMBINING, not in any
    specific domain.
  - Keeping the orchestrator free of MCP calls means it can be swapped,
    upgraded, or replicated without any coupling to data schemas.
  - It also means the orchestrator can be replaced with an LLM-powered
    planner that decides which agents to call based on natural language —
    without changing any sub-agent code.

DESIGN PATTERN: Hierarchical Agent Delegation
---------------------------------------------
This pattern is common in production multi-agent frameworks (LangGraph,
AutoGen, CrewAI, Google ADK). The orchestrator is a "supervisor" or
"planner" agent that:
  1. Understands the high-level intent
  2. Decomposes it into sub-tasks
  3. Assigns each sub-task to the right specialist
  4. Aggregates and returns the combined result
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from a2a.a2a_protocol import A2AClient, A2ARegistry, A2AResponse, A2ATask
from a2a.agent_card import AgentCard
from mcp.mcp_server import MCPServer

from agents.pharmacy_agent import PharmacyAgent
from agents.records_agent import RecordsAgent
from agents.scheduling_agent import SchedulingAgent

# ---------------------------------------------------------------------------
# ANSI colour codes for rich console output
# ---------------------------------------------------------------------------
_WHITE = "\033[97m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_RED = "\033[91m"
_BLUE = "\033[94m"
_CYAN = "\033[96m"
_MAGENTA = "\033[95m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RESET = "\033[0m"

# Box-drawing borders used in section headers
_BORDER_DOUBLE = "═" * 62
_BORDER_SINGLE = "─" * 62
_BORDER_LIGHT = "·" * 62


def _log(text: str) -> None:
    """Print a formatted Orchestrator log line with timestamp."""
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"{_BLUE}{_BOLD}[OrchestratorAgent]{_RESET} [{ts}] {text}")


def _section(title: str, colour: str = _CYAN) -> None:
    """Print a prominent section header to visually separate protocol stages."""
    print(f"\n{colour}{_BOLD}╔{_BORDER_DOUBLE}╗")
    print(f"║  {title:<60}║")
    print(f"╚{_BORDER_DOUBLE}╝{_RESET}")


def _subsection(title: str) -> None:
    """Print a lighter sub-section divider."""
    print(f"\n{_DIM}{_BORDER_SINGLE}")
    print(f"  {title}")
    print(f"{_BORDER_SINGLE}{_RESET}")


def _result_block(label: str, content: str, colour: str = _GREEN) -> None:
    """Print a labelled result block in a consistent format."""
    print(f"\n{colour}{_BOLD}┌─ {label} {'─' * max(0, 58 - len(label))}┐{_RESET}")
    for line in content.strip().splitlines():
        print(f"  {line}")
    print(f"{colour}{_BOLD}└{'─' * 62}┘{_RESET}")


# ---------------------------------------------------------------------------
# OrchestratorAgent
# ---------------------------------------------------------------------------


class OrchestratorAgent:
    """
    Orchestrator Agent — the top-level coordinator of the MediAssist system.

    PROTOCOL ROLE: A2A Client + Registry Coordinator
    -------------------------------------------------
    This agent is the only agent that the application (main.py) interacts
    with directly. It:
      1. Owns an A2ARegistry and registers all sub-agent AgentCards.
      2. Owns an A2AClient for sending tasks to sub-agents.
      3. Exposes handle_request() as the unified public API.
      4. Performs intent-based routing using A2A discovery.
      5. Aggregates results from multiple agents for complex requests.

    DOES NOT:
      - Import or call data files directly.
      - Use MCP tools directly (those belong to sub-agents).
      - Hard-code which agent handles which capability (discovery does that).

    Attributes:
        agent_id        : Unique ID for this orchestrator instance.
        _registry       : The A2ARegistry holding all sub-agent AgentCards.
        _a2a_client     : The A2AClient used to send tasks to sub-agents.
        _mcp_server     : The shared MCPServer instance passed to sub-agents.
        _scheduling_agent: The SchedulingAgent instance.
        _records_agent  : The RecordsAgent instance.
        _pharmacy_agent : The PharmacyAgent instance.
        _request_log    : History of all requests handled in this session.
    """

    AGENT_ID = "orchestrator-agent-001"
    AGENT_NAME = "OrchestratorAgent"

    def __init__(self) -> None:
        """
        Initialise the OrchestratorAgent and bootstrap the entire system.

        Startup sequence:
          1. Create the shared MCPServer (the single data-access gateway).
          2. Instantiate all three sub-agents (each connects their MCPClient).
          3. Create the A2ARegistry and register every sub-agent's AgentCard.
          4. Create the A2AClient for outbound task dispatch.

        After __init__ completes, the system is fully operational and
        handle_request() can be called immediately.
        """
        _section("MediAssist — System Bootstrap", _MAGENTA)
        _log(f"{_GREEN}Starting OrchestratorAgent (id={self.AGENT_ID!r}) ...{_RESET}")

        # ── Step 1: Create the shared MCP Server ──────────────────────
        # The MCPServer is the single gateway to all medical data. Every
        # sub-agent gets a reference to the SAME server instance — mirroring
        # how multiple microservices might share a single database cluster
        # or API gateway in a real healthcare architecture.
        _log(
            f"{_YELLOW}▶ Step 1:{_RESET} Initialising MCP Server (medical data gateway) ..."
        )
        self._mcp_server = MCPServer(name="MediAssist MCP Server")
        _log(
            f"{_GREEN}✓ MCP Server ready:{_RESET} "
            f"{len(self._mcp_server.tools)} tools registered: "
            f"{list(self._mcp_server.tools.keys())}"
        )

        # ── Step 2: Instantiate sub-agents ────────────────────────────
        # Each agent's __init__ creates its AgentCard and connects its
        # own MCPClient to the shared MCPServer. The [MCP] connection
        # log lines you see here are from MCPClient.__init__().
        _log(f"{_YELLOW}▶ Step 2:{_RESET} Instantiating sub-agents ...")

        _subsection("Initialising SchedulingAgent")
        self._scheduling_agent = SchedulingAgent(mcp_server=self._mcp_server)

        _subsection("Initialising RecordsAgent")
        self._records_agent = RecordsAgent(mcp_server=self._mcp_server)

        _subsection("Initialising PharmacyAgent")
        self._pharmacy_agent = PharmacyAgent(mcp_server=self._mcp_server)

        # ── Step 3: Create A2A Registry and register all agents ───────
        # The A2ARegistry is the central directory of agent capabilities.
        # By registering here at startup (rather than lazily), we catch
        # configuration errors (e.g., duplicate agent IDs, missing caps)
        # before any real requests arrive.
        _log(
            f"{_YELLOW}▶ Step 3:{_RESET} Creating A2A Registry and registering agents ..."
        )
        self._registry = A2ARegistry()

        # Register each sub-agent's AgentCard.
        # In real A2A, each agent would self-register by publishing its
        # card at /.well-known/agent.json. Here the orchestrator registers
        # them centrally since it knows about all agents at boot time.
        self._registry.register(self._scheduling_agent.agent_card)
        self._registry.register(self._records_agent.agent_card)
        self._registry.register(self._pharmacy_agent.agent_card)

        # Print the full registry table so the startup sequence is clear
        self._registry.print_registry()

        # ── Step 4: Create A2A Client ─────────────────────────────────
        # The A2AClient is used by THIS orchestrator to SEND tasks to
        # sub-agents. Every outgoing task will carry sender_id=AGENT_ID.
        _log(f"{_YELLOW}▶ Step 4:{_RESET} Creating A2A Client ...")
        self._a2a_client = A2AClient(sender_id=self.AGENT_ID)

        # ── Request history (session audit trail) ─────────────────────
        self._request_log: List[Dict[str, Any]] = []

        _log(
            f"{_GREEN}✓ OrchestratorAgent READY.{_RESET} "
            f"{len(self._registry)} sub-agent(s) registered."
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def handle_request(
        self,
        patient_id: str,
        request_type: str,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Handle a user-facing request by routing it to the appropriate agent(s).

        This is the ONLY method the application layer needs to call. It
        abstracts away all the A2A protocol details (discovery, task creation,
        authentication, response handling) behind a simple Python method call.

        Supported request_type values:
          "full_checkup"
              Fan-out to ALL three sub-agents (RecordsAgent + PharmacyAgent +
              SchedulingAgent) and merge their results into a single unified
              patient summary. Shows the richest A2A interaction.

              Required kwargs: none
              Optional kwargs:
                doctor (str)  — doctor to check availability for
                date   (str)  — date to check availability (YYYY-MM-DD)

          "book_appointment"
              Route to SchedulingAgent (discovered dynamically via registry)
              and book a new appointment.

              Required kwargs:
                date   (str) — appointment date    (YYYY-MM-DD)
                time   (str) — appointment time    (HH:MM)
                doctor (str) — doctor to book with

          "prescriptions"
              Route to PharmacyAgent (discovered dynamically via registry)
              and return full prescription status report.

              Required kwargs: none

        ROUTING PRINCIPLE (why no if/elif for agent names):
        ---------------------------------------------------
        The routing uses A2ARegistry.discover(capability) rather than
        hard-coded agent names. This means:
          - A new agent can be added without changing this method.
          - Multiple agents with the same capability are handled gracefully
            (registry returns the best match; or discover_all for fan-out).
          - The capability string comes from the request_type itself, keeping
            the routing logic transparent and self-documenting.

        Args:
            patient_id   : The patient's ID (e.g. "P001").
            request_type : The type of request. See supported values above.
            **kwargs     : Additional parameters specific to the request type.
                           See per-type documentation above.

        Returns:
            A dict containing:
              "request_type"  : str   — echoes the input request_type
              "patient_id"    : str   — echoes the patient_id
              "status"        : str   — "success" | "partial_success" | "failed"
              "results"       : dict  — keyed results from each sub-agent called
              "summary"       : str   — combined plain-English summary
              "timestamp"     : str   — ISO timestamp of when the request was handled
              "errors"        : list  — any error messages (may be empty)

        Raises:
            ValueError: If request_type is not recognised.
        """
        ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        _section(
            f"REQUEST: {request_type.upper()}  |  Patient: {patient_id}",
            colour=_BLUE,
        )
        _log(
            f"Received request — type={request_type!r}, "
            f"patient_id={patient_id!r}, kwargs={kwargs}"
        )

        # Dispatch to the correct handler based on request type
        if request_type == "full_checkup":
            result = self._handle_full_checkup(patient_id, **kwargs)
        elif request_type == "book_appointment":
            result = self._handle_book_appointment(patient_id, **kwargs)
        elif request_type == "prescriptions":
            result = self._handle_prescriptions(patient_id)
        else:
            supported = ["full_checkup", "book_appointment", "prescriptions"]
            msg = f"Unknown request_type '{request_type}'. Supported types: {supported}"
            _log(f"{_RED}✗ UNKNOWN REQUEST TYPE:{_RESET} {msg}")
            raise ValueError(msg)

        # Stamp timestamp and request metadata onto the result
        result["timestamp"] = ts
        result["request_type"] = request_type
        result["patient_id"] = patient_id

        # Record in session audit log
        self._request_log.append(
            {
                "timestamp": ts,
                "patient_id": patient_id,
                "request_type": request_type,
                "status": result.get("status"),
                "kwargs": kwargs,
            }
        )

        _log(
            f"{_GREEN}✓ Request complete.{_RESET} "
            f"status={result.get('status')!r}  "
            f"errors={result.get('errors', [])}"
        )

        return result

    # ------------------------------------------------------------------
    # Request handlers
    # ------------------------------------------------------------------

    def _handle_full_checkup(
        self,
        patient_id: str,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Handle the "full_checkup" request type.

        PROTOCOL PATTERN: A2A Fan-Out + Result Aggregation
        ---------------------------------------------------
        This handler demonstrates the most complex A2A interaction pattern:
        "fan-out". The orchestrator sends tasks to ALL THREE sub-agents and
        then merges their responses into a single comprehensive result.

        Fan-out sequence:
          1. [A2A] → RecordsAgent    (get_medical_history)
          2. [A2A] → PharmacyAgent   (check_prescriptions)
          3. [A2A] → SchedulingAgent (check_availability) [optional]
          4. Aggregate all three A2AResponses → unified summary

        WHY CALL ALL THREE?
        -------------------
        In a real clinical workflow, a "full checkup" prep would:
          - Pull the patient's current chart (RecordsAgent)
          - Review all active medications and flag any refill issues (PharmacyAgent)
          - Check the doctor's upcoming schedule (SchedulingAgent)
        All three pieces of information are needed before the appointment.

        Result aggregation logic:
          - If ALL three succeed  → status = "success"
          - If SOME succeed       → status = "partial_success"
          - If ALL fail           → status = "failed"
          The unified summary concatenates all successful sub-results.

        Args:
            patient_id: The patient's ID to run the full checkup for.
            **kwargs  : Optional. May include:
                          doctor (str) — doctor name for slot availability check
                          date   (str) — date for slot availability check

        Returns:
            Aggregated result dict with sub-results keyed by agent name.
        """
        _log(
            f"{_CYAN}▶ FULL CHECKUP:{_RESET} "
            f"Fanning out to all 3 sub-agents for patient {patient_id!r} ..."
        )

        results: Dict[str, Any] = {}
        errors: List[str] = []
        summaries: List[str] = []

        # ── Sub-task 1: Medical Records ───────────────────────────────
        _subsection("Fan-out Step 1/3 — RecordsAgent: get_medical_history")
        records_response = self._dispatch_task(
            capability="get_medical_history",
            intent="get_medical_history",
            payload={"patient_id": patient_id},
            agent=self._records_agent,
        )
        results["medical_records"] = records_response
        if records_response.succeeded:
            summaries.append(records_response.message)
            _log(
                f"{_GREEN}✓ RecordsAgent completed:{_RESET} {records_response.message}"
            )
        else:
            errors.append(f"RecordsAgent: {records_response.message}")
            _log(f"{_RED}✗ RecordsAgent failed:{_RESET} {records_response.message}")

        # ── Sub-task 2: Prescriptions ─────────────────────────────────
        _subsection("Fan-out Step 2/3 — PharmacyAgent: check_prescriptions")
        pharmacy_response = self._dispatch_task(
            capability="check_prescriptions",
            intent="check_prescriptions",
            payload={"patient_id": patient_id},
            agent=self._pharmacy_agent,
        )
        results["prescriptions"] = pharmacy_response
        if pharmacy_response.succeeded:
            summaries.append(pharmacy_response.message)
            _log(
                f"{_GREEN}✓ PharmacyAgent completed:{_RESET} {pharmacy_response.message}"
            )
        else:
            errors.append(f"PharmacyAgent: {pharmacy_response.message}")
            _log(f"{_RED}✗ PharmacyAgent failed:{_RESET} {pharmacy_response.message}")

        # ── Sub-task 3: Appointment Availability (optional) ───────────
        # We check slot availability if a doctor is specified, otherwise
        # we just list the patient's existing appointments via records data.
        doctor = kwargs.get("doctor")
        date = kwargs.get("date")

        if doctor and date:
            _subsection(
                f"Fan-out Step 3/3 — SchedulingAgent: check_availability "
                f"({doctor} on {date})"
            )
            scheduling_response = self._dispatch_task(
                capability="check_availability",
                intent="check_availability",
                payload={"doctor": doctor, "date": date},
                agent=self._scheduling_agent,
            )
            results["availability"] = scheduling_response
            if scheduling_response.succeeded:
                summaries.append(scheduling_response.message)
                _log(
                    f"{_GREEN}✓ SchedulingAgent completed:{_RESET} "
                    f"{scheduling_response.message}"
                )
            else:
                errors.append(f"SchedulingAgent: {scheduling_response.message}")
                _log(
                    f"{_RED}✗ SchedulingAgent failed:{_RESET} "
                    f"{scheduling_response.message}"
                )
        else:
            _subsection("Fan-out Step 3/3 — SchedulingAgent: skipped (no doctor/date)")
            _log(
                f"{_DIM}Skipping availability check — "
                f"no doctor/date in request kwargs.{_RESET}"
            )
            results["availability"] = None

        # ── Determine overall status ──────────────────────────────────
        active_responses = [
            r
            for r in [
                records_response,
                pharmacy_response,
                results.get("availability"),
            ]
            if r is not None
        ]
        succeeded_count = sum(1 for r in active_responses if r.succeeded)
        total_count = len(active_responses)

        if succeeded_count == total_count:
            overall_status = "success"
        elif succeeded_count > 0:
            overall_status = "partial_success"
        else:
            overall_status = "failed"

        # ── Build unified summary ─────────────────────────────────────
        unified_summary = self._build_full_checkup_summary(
            patient_id=patient_id,
            records_response=records_response,
            pharmacy_response=pharmacy_response,
            scheduling_response=results.get("availability"),
        )

        _result_block("FULL CHECKUP RESULT", unified_summary, colour=_GREEN)

        return {
            "status": overall_status,
            "results": results,
            "summary": unified_summary,
            "sub_summaries": summaries,
            "errors": errors,
            "agents_called": 3 if (doctor and date) else 2,
            "agents_succeeded": succeeded_count,
        }

    def _handle_book_appointment(
        self,
        patient_id: str,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Handle the "book_appointment" request type.

        PROTOCOL PATTERN: A2A Discovery + Single Delegation
        ----------------------------------------------------
        This handler demonstrates standard A2A task delegation:
          1. Discover which agent handles "schedule_appointment"
             via the A2ARegistry (runtime discovery, not hard-coded).
          2. Build an A2ATask with the booking parameters.
          3. Send the task via A2AClient and receive the A2AResponse.
          4. Format and return the booking confirmation.

        IMPORTANT: The orchestrator does NOT know (or care) that the
        SchedulingAgent exists by name. It asks "who can schedule_appointment?"
        and the registry answers. This is the power of A2A discovery.

        Required kwargs:
            date   (str) — appointment date    (YYYY-MM-DD)
            time   (str) — appointment time    (HH:MM)
            doctor (str) — doctor name to book with

        Args:
            patient_id : The patient requesting the appointment.
            **kwargs   : Must include date, time, doctor.

        Returns:
            Result dict with the booking confirmation in results["booking"].
        """
        # ── Validate required kwargs ──────────────────────────────────
        required_kwargs = ["date", "time", "doctor"]
        missing = [k for k in required_kwargs if k not in kwargs]
        if missing:
            msg = (
                f"'book_appointment' request requires kwargs: {required_kwargs}. "
                f"Missing: {missing}"
            )
            _log(f"{_RED}✗ MISSING KWARGS:{_RESET} {msg}")
            return {
                "status": "failed",
                "results": {},
                "summary": msg,
                "errors": [msg],
            }

        date = kwargs["date"]
        time_slot = kwargs["time"]
        doctor = kwargs["doctor"]

        _log(
            f"{_CYAN}▶ BOOK APPOINTMENT:{_RESET} "
            f"patient={patient_id!r}, doctor={doctor!r}, "
            f"date={date!r}, time={time_slot!r}"
        )

        # ── A2A Discovery: find the scheduling agent ───────────────────
        _subsection("A2A Discovery — finding agent for 'schedule_appointment'")
        agent_card = self._registry.discover("schedule_appointment")

        if agent_card is None:
            msg = (
                "No agent registered with capability 'schedule_appointment'. "
                "The SchedulingAgent may not have been initialised correctly."
            )
            _log(f"{_RED}✗ DISCOVERY FAILED:{_RESET} {msg}")
            return {
                "status": "failed",
                "results": {},
                "summary": msg,
                "errors": [msg],
            }

        _log(
            f"{_GREEN}✓ Discovered:{_RESET} "
            f"{agent_card.name!r} at {agent_card.endpoint!r}"
        )

        # ── A2A Task dispatch ──────────────────────────────────────────
        _subsection("A2A Task Dispatch — SchedulingAgent: schedule_appointment")
        response = self._dispatch_task(
            capability="schedule_appointment",
            intent="schedule_appointment",
            payload={
                "patient_id": patient_id,
                "date": date,
                "time": time_slot,
                "doctor": doctor,
            },
            agent=self._scheduling_agent,
        )

        # ── Format result ─────────────────────────────────────────────
        if response.succeeded:
            appt = response.result.get("appointment", {})
            summary = (
                f"✓ Appointment CONFIRMED\n"
                f"  Appointment ID : {appt.get('id', 'N/A')}\n"
                f"  Patient ID     : {patient_id}\n"
                f"  Doctor         : {appt.get('doctor', doctor)}\n"
                f"  Date           : {appt.get('date', date)}\n"
                f"  Time           : {appt.get('time', time_slot)}\n"
                f"  Status         : {appt.get('status', 'confirmed').upper()}\n"
                f"  Booked At      : {appt.get('booked_at', 'now')}"
            )
            remaining = response.result.get("available_slots_remaining", [])
            if remaining:
                summary += f"\n  Other open slots: {remaining}"
            _result_block("BOOKING CONFIRMATION", summary, colour=_GREEN)
        else:
            summary = f"✗ Booking FAILED: {response.message}"
            _result_block("BOOKING FAILED", summary, colour=_RED)

        return {
            "status": "success" if response.succeeded else "failed",
            "results": {"booking": response},
            "summary": summary,
            "errors": [] if response.succeeded else [response.message],
        }

    def _handle_prescriptions(self, patient_id: str) -> Dict[str, Any]:
        """
        Handle the "prescriptions" request type.

        PROTOCOL PATTERN: A2A Discovery + Single Delegation (Read-only)
        ----------------------------------------------------------------
        Similar to _handle_book_appointment but for a read-only query:
          1. Discover which agent handles "check_prescriptions" via registry.
          2. Build an A2ATask with just the patient_id.
          3. Send the task via A2AClient and receive the A2AResponse.
          4. Format and return the prescription status report.

        The discovery step ensures the PharmacyAgent is found by capability,
        not by name — keeping the orchestrator loosely coupled to the
        specific agent implementation.

        Args:
            patient_id: The patient whose prescriptions to retrieve.

        Returns:
            Result dict with prescription report in results["prescriptions"].
        """
        _log(
            f"{_CYAN}▶ PRESCRIPTIONS:{_RESET} "
            f"Checking prescription status for patient {patient_id!r} ..."
        )

        # ── A2A Discovery: find the pharmacy agent ─────────────────────
        _subsection("A2A Discovery — finding agent for 'check_prescriptions'")
        agent_card = self._registry.discover("check_prescriptions")

        if agent_card is None:
            msg = (
                "No agent registered with capability 'check_prescriptions'. "
                "The PharmacyAgent may not have been initialised correctly."
            )
            _log(f"{_RED}✗ DISCOVERY FAILED:{_RESET} {msg}")
            return {
                "status": "failed",
                "results": {},
                "summary": msg,
                "errors": [msg],
            }

        _log(
            f"{_GREEN}✓ Discovered:{_RESET} "
            f"{agent_card.name!r} at {agent_card.endpoint!r}"
        )

        # ── A2A Task dispatch ──────────────────────────────────────────
        _subsection("A2A Task Dispatch — PharmacyAgent: check_prescriptions")
        response = self._dispatch_task(
            capability="check_prescriptions",
            intent="check_prescriptions",
            payload={"patient_id": patient_id},
            agent=self._pharmacy_agent,
        )

        # ── Format result ─────────────────────────────────────────────
        if response.succeeded:
            result_data = response.result
            rx_list = result_data.get("analysed", [])
            alerts = result_data.get("alerts", [])

            # Build a structured display of each prescription
            rx_lines: List[str] = []
            for rx in rx_list:
                med = rx.get("medication", "Unknown")
                refills = rx.get("refills_remaining", 0)
                status = rx.get("refill_status", "?").upper()
                last = rx.get("last_filled", "N/A")
                days = rx.get("days_since_fill")
                days_str = f"  ({days}d ago)" if days is not None else ""
                rx_lines.append(
                    f"  • {med:<32} refills={refills}  [{status}]  "
                    f"last filled: {last}{days_str}"
                )

            summary_lines = [response.message, ""]
            if rx_lines:
                summary_lines.append("Medications:")
                summary_lines.extend(rx_lines)
            if alerts:
                summary_lines.append("")
                summary_lines.append("Alerts:")
                for alert in alerts:
                    summary_lines.append(f"  ⚠ {alert}")

            summary = "\n".join(summary_lines)
            _result_block("PRESCRIPTION STATUS", summary, colour=_GREEN)
        else:
            summary = f"✗ Prescription check FAILED: {response.message}"
            _result_block("PRESCRIPTION CHECK FAILED", summary, colour=_RED)

        return {
            "status": "success" if response.succeeded else "failed",
            "results": {"prescriptions": response},
            "summary": summary,
            "errors": [] if response.succeeded else [response.message],
        }

    # ------------------------------------------------------------------
    # A2A dispatch helper
    # ------------------------------------------------------------------

    def _dispatch_task(
        self,
        capability: str,
        intent: str,
        payload: Dict[str, Any],
        agent: Any,
    ) -> A2AResponse:
        """
        Create and dispatch an A2ATask to a sub-agent via the A2AClient.

        PROTOCOL STEP: Task Creation + A2A Send
        ----------------------------------------
        This helper encapsulates the A2A task lifecycle:
          1. Retrieve the target agent's AgentCard from the registry.
          2. Construct an A2ATask with a generated task_id, the intent,
             and the payload.
          3. Call A2AClient.send_task() which:
             a. Validates the capability against the AgentCard.
             b. Simulates authentication (Bearer token).
             c. Calls agent.process_task(task) (simulates HTTP POST).
             d. Returns the A2AResponse.

        If the AgentCard is not found in the registry (shouldn't happen if
        __init__ completed successfully), a synthetic failed A2AResponse is
        returned so the caller can handle it gracefully.

        Args:
            capability : The capability string to look up in the registry.
                         This is used to find the AgentCard, not the agent
                         object (reinforcing the A2A discovery separation).
            intent     : The intent string to set on the A2ATask. Often the
                         same as capability but can differ (e.g., capability
                         might be "schedule_appointment" while intent might
                         be "check_availability" for the same agent).
            payload    : The data payload for the task.
            agent      : The actual agent object whose process_task() will be
                         called. Passed separately from the AgentCard because
                         in-process simulation needs both the metadata (card)
                         and the callable (agent).

        Returns:
            The A2AResponse from the sub-agent's process_task() method.
            Never raises — errors are wrapped in a failed A2AResponse.
        """
        # Look up the AgentCard from the registry (this is the A2A
        # discovery step — the orchestrator doesn't hold a direct
        # reference to the sub-agent's card; it goes through the registry)
        agent_card: Optional[AgentCard] = self._registry.discover(capability)

        if agent_card is None:
            error_msg = (
                f"Cannot dispatch task: no agent registered for "
                f"capability '{capability}'."
            )
            _log(f"{_RED}✗ DISPATCH ERROR:{_RESET} {error_msg}")
            # Return a synthetic failed response so callers don't need
            # to handle None values — they can always call .succeeded
            dummy_task_id = str(__import__("uuid").uuid4())
            return A2AResponse(
                task_id=dummy_task_id,
                agent_id="unknown",
                status="failed",
                result={"error": error_msg},
                message=error_msg,
            )

        # Build the A2ATask
        task = A2ATask(
            sender_id=self.AGENT_ID,
            receiver_id=agent_card.agent_id,
            intent=intent,
            payload=payload,
        )

        _log(
            f"Dispatching A2ATask  "
            f"task_id={task.task_id[:8]}...  "
            f"intent={intent!r}  "
            f"→  {agent_card.name!r}"
        )

        # Send via A2AClient (all [A2A] log lines are emitted inside here)
        try:
            response = self._a2a_client.send_task(
                agent_card=agent_card,
                task=task,
                agent_callable=agent.process_task,
            )
        except Exception as exc:  # noqa: BLE001
            error_msg = f"A2AClient.send_task raised an unexpected error: {exc}"
            _log(f"{_RED}✗ A2A CLIENT ERROR:{_RESET} {error_msg}")
            return A2AResponse(
                task_id=task.task_id,
                agent_id=agent_card.agent_id,
                status="failed",
                result={"error": str(exc)},
                message=error_msg,
            )

        return response

    # ------------------------------------------------------------------
    # Result aggregation helpers
    # ------------------------------------------------------------------

    def _build_full_checkup_summary(
        self,
        patient_id: str,
        records_response: A2AResponse,
        pharmacy_response: A2AResponse,
        scheduling_response: Optional[A2AResponse],
    ) -> str:
        """
        Build a unified, human-readable full-checkup summary report.

        This is the "aggregation" step of the fan-out pattern — taking
        the results from three separate A2A calls and merging them into
        a single cohesive document.

        In a real LLM-powered orchestrator, this step might use an LLM
        to synthesise the three sub-agent outputs into a narrative paragraph.
        Here we use structured formatting.

        The summary includes:
          Section 1 — Patient Demographics & Conditions (from RecordsAgent)
          Section 2 — Appointment History (from RecordsAgent)
          Section 3 — Prescription Status (from PharmacyAgent)
          Section 4 — Availability (from SchedulingAgent, if called)
          Section 5 — Clinical Alerts (aggregated from all agents)

        Args:
            patient_id           : Patient ID for the report header.
            records_response     : A2AResponse from RecordsAgent.
            pharmacy_response    : A2AResponse from PharmacyAgent.
            scheduling_response  : A2AResponse from SchedulingAgent (or None).

        Returns:
            A multi-line string containing the full integrated checkup report.
        """
        lines: List[str] = []
        generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # ── Report header ─────────────────────────────────────────────
        lines.append("╔══════════════════════════════════════════════════════════╗")
        lines.append("║            MediAssist — FULL CHECKUP REPORT              ║")
        lines.append("╚══════════════════════════════════════════════════════════╝")
        lines.append(f"  Patient ID : {patient_id}")
        lines.append(f"  Generated  : {generated_at}")
        lines.append(f"  Protocol   : A2A fan-out × 3 agents + MCP data retrieval")
        lines.append("")

        # ── Section 1 & 2: Records data ───────────────────────────────
        lines.append("══ 1. PATIENT RECORDS  [via RecordsAgent → A2A → MCP] ══════")
        if records_response.succeeded:
            rec = records_response.result
            demo = rec.get("demographics", {})
            conditions = rec.get("conditions", [])
            appointments = rec.get("appointments", [])

            lines.append(f"  Name        : {demo.get('name', 'N/A')}")
            lines.append(f"  Date of Birth: {demo.get('dob', 'N/A')}")
            lines.append(f"  Doctor      : {demo.get('doctor', 'N/A')}")
            lines.append(
                f"  Conditions  : "
                + (", ".join(c.title() for c in conditions) if conditions else "none")
            )
            lines.append(f"  Appointments: {len(appointments)} on record")
            if appointments:
                for appt in appointments:
                    lines.append(
                        f"    • [{appt.get('id')}] {appt.get('date')} "
                        f"at {appt.get('time')} — {appt.get('doctor')} "
                        f"[{appt.get('status', '?').upper()}]"
                    )
        else:
            lines.append(
                f"  {_RED}⚠ Records retrieval failed:{_RESET} "
                f"{records_response.message}"
            )
            lines.append(f"  ✗ {records_response.message}")
        lines.append("")

        # ── Section 3: Pharmacy data ───────────────────────────────────
        lines.append("══ 2. PRESCRIPTION STATUS  [via PharmacyAgent → A2A → MCP] ═")
        if pharmacy_response.succeeded:
            rx_data = pharmacy_response.result
            analysed = rx_data.get("analysed", [])
            alerts = rx_data.get("alerts", [])

            if analysed:
                for rx in analysed:
                    med = rx.get("medication", "Unknown")
                    refills = rx.get("refills_remaining", 0)
                    status_tag = rx.get("refill_status", "?").upper()
                    last = rx.get("last_filled", "N/A")
                    lines.append(
                        f"  • {med:<32} refills={refills}  [{status_tag}]  last: {last}"
                    )
                if alerts:
                    lines.append("  Alerts:")
                    for alert in alerts:
                        lines.append(f"    ⚠ {alert}")
            else:
                lines.append("  No active prescriptions found.")
        else:
            lines.append(f"  ✗ {pharmacy_response.message}")
        lines.append("")

        # ── Section 4: Scheduling data (if available) ─────────────────
        lines.append("══ 3. AVAILABILITY  [via SchedulingAgent → A2A → MCP] ══════")
        if scheduling_response is not None and scheduling_response.succeeded:
            sched_data = scheduling_response.result
            doctor = sched_data.get("doctor", "N/A")
            date_str = sched_data.get("date", "N/A")
            slots = sched_data.get("available_slots", [])
            lines.append(f"  Doctor : {doctor}  |  Date : {date_str}")
            lines.append(
                f"  Open slots: {', '.join(slots) if slots else '(fully booked)'}"
            )
        elif scheduling_response is not None and not scheduling_response.succeeded:
            lines.append(f"  ✗ {scheduling_response.message}")
        else:
            lines.append(
                "  (No availability check requested — doctor/date not provided)"
            )
        lines.append("")

        # ── Section 5: Aggregated clinical alerts ─────────────────────
        all_alerts: List[str] = []
        if pharmacy_response.succeeded:
            all_alerts.extend(pharmacy_response.result.get("alerts", []))
        # Add records-based clinical notes if available
        if records_response.succeeded:
            narrative = records_response.result.get("narrative", "")
            if "⚕" in narrative:
                # Extract clinical note lines from the narrative
                for line in narrative.splitlines():
                    if "⚕" in line:
                        note = line.strip().lstrip("⚕").strip()
                        all_alerts.append(note)

        if all_alerts:
            lines.append("══ 4. CLINICAL ALERTS ══════════════════════════════════════")
            for alert in all_alerts:
                lines.append(f"  ⚕ {alert}")
            lines.append("")

        # ── Footer ────────────────────────────────────────────────────
        lines.append("════════════════════════════════════════════════════════════")
        lines.append("  Generated by MediAssist OrchestratorAgent")
        lines.append(
            "  Protocols: A2A (Agent2Agent discovery + task delegation) "
            "+ MCP (tool calls)"
        )
        lines.append("════════════════════════════════════════════════════════════")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Introspection helpers
    # ------------------------------------------------------------------

    def get_session_summary(self) -> Dict[str, Any]:
        """
        Return a summary of all requests handled in this session.

        Useful for audit logging, session review, or feeding back to a
        monitoring dashboard. Shows total requests, breakdown by type,
        and overall success/failure counts.

        Returns:
            Dict with request statistics for the current session.
        """
        total = len(self._request_log)
        succeeded = sum(1 for r in self._request_log if r["status"] == "success")
        partial = sum(1 for r in self._request_log if r["status"] == "partial_success")
        failed = sum(1 for r in self._request_log if r["status"] == "failed")

        by_type: Dict[str, int] = {}
        for req in self._request_log:
            rt = req["request_type"]
            by_type[rt] = by_type.get(rt, 0) + 1

        return {
            "agent_id": self.AGENT_ID,
            "total_requests": total,
            "succeeded": succeeded,
            "partial_success": partial,
            "failed": failed,
            "requests_by_type": by_type,
            "registered_agents": len(self._registry),
            "registry_summary": [c.name for c in self._registry.list_agents()],
        }

    def print_session_summary(self) -> None:
        """Print the session summary in a formatted block to the console."""
        summary = self.get_session_summary()
        _section("SESSION SUMMARY", colour=_MAGENTA)
        print(
            f"  Total requests : {summary['total_requests']}\n"
            f"  Succeeded      : {summary['succeeded']}\n"
            f"  Partial        : {summary['partial_success']}\n"
            f"  Failed         : {summary['failed']}\n"
            f"  By type        : {summary['requests_by_type']}\n"
            f"  Agents active  : {summary['registry_summary']}"
        )

    def __repr__(self) -> str:
        return (
            f"OrchestratorAgent("
            f"agent_id={self.AGENT_ID!r}, "
            f"registered_agents={len(self._registry)})"
        )
