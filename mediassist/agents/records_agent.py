"""
Records Agent — A2A Sub-Agent for Patient Medical Records
==========================================================
PROTOCOL ROLES:
  • A2A Server   — This agent registers an AgentCard and exposes a
                   process_task() endpoint that the OrchestratorAgent calls.
  • MCP Client   — Internally, this agent uses an MCPClient to reach the
                   MCP Server for patient demographics and appointment data.

RESPONSIBILITIES:
-----------------
The RecordsAgent is the read-only medical records specialist in MediAssist.
It retrieves, aggregates, and summarises patient health information from the
MCP Server — acting as the "chart pull" function in a clinical workflow.

Supported A2A intents (declared in its AgentCard capabilities):
  • "get_patient_records"   — Retrieve full demographics + conditions.
  • "get_medical_history"   — Retrieve appointments + conditions summary.

FLOW FOR "get_patient_records":
  1. OrchestratorAgent discovers this agent via A2ARegistry.
  2. Orchestrator sends an A2ATask with intent="get_patient_records" and
     payload={"patient_id": "P001"}
  3. process_task() receives the task.
  4. Agent calls MCP tool "get_patient_record" for demographics.
  5. Agent calls MCP tool "get_appointments" for scheduled visits.
  6. Agent assembles a structured summary and returns an A2AResponse.

FLOW FOR "get_medical_history":
  1. Same discovery and dispatch as above, different intent string.
  2. process_task() routes to _handle_get_medical_history().
  3. Both MCP tools are called; results are combined into a history narrative.
  4. Returns A2AResponse with the combined health summary.

WHY TWO INTENTS FOR SIMILAR DATA?
----------------------------------
"get_patient_records" returns raw structured data (for machine consumption),
while "get_medical_history" returns a narrative summary (suitable for display
to a clinician or for inclusion in an LLM prompt as context). This separation
mirrors real EHR APIs that offer both structured (FHIR) and narrative (HL7 CDA)
formats for the same underlying data.
"""

from datetime import datetime
from typing import Any, Dict, List

from a2a.a2a_protocol import A2AResponse, A2ATask
from a2a.agent_card import AgentCard
from mcp.mcp_client import MCPClient
from mcp.mcp_server import MCPServer

# ---------------------------------------------------------------------------
# ANSI colour codes for console output
# ---------------------------------------------------------------------------
_PURPLE = "\033[94m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_RED = "\033[91m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RESET = "\033[0m"


def _log(agent_name: str, text: str) -> None:
    """Print a formatted agent log line with a timestamp."""
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"{_PURPLE}{_BOLD}[{agent_name}]{_RESET} [{ts}] {text}")


# ---------------------------------------------------------------------------
# RecordsAgent
# ---------------------------------------------------------------------------


class RecordsAgent:
    """
    A2A Sub-Agent — Patient Medical Records Specialist.

    PROTOCOL ROLE: A2A Server (sub-agent / skill agent)
    ----------------------------------------------------
    In A2A terminology, this agent acts as a *server* — it exposes a
    process_task() method that the Orchestrator calls (via A2AClient).
    It publishes an AgentCard that advertises:
      - What it can do    (capabilities: get_patient_records, get_medical_history)
      - Where to reach it (endpoint — simulated URL)
      - How to auth       (auth_scheme: Bearer)

    The OrchestratorAgent registers this card in the A2ARegistry at startup.
    When a "full_checkup" request arrives, the orchestrator discovers and calls
    this agent alongside the other two specialists, then merges all three
    A2AResponses into a unified patient summary.

    PROTOCOL ROLE: MCP Client (data access)
    ----------------------------------------
    Internally, this agent uses an MCPClient to invoke two read-only tools on
    the shared MCP Server:
      • get_patient_record(patient_id)  — fetches demographics and conditions
      • get_appointments(patient_id)    — fetches all scheduled appointments

    The agent never reads JSON files directly. All data access is mediated by
    the MCP layer, preserving the clean separation of concerns:

        OrchestratorAgent
            ──[A2A]──▶ RecordsAgent.process_task()
                            ──[MCP]──▶ MCPServer.get_patient_record()
                            ──[MCP]──▶ MCPServer.get_appointments()
                                            ──▶ patients.json
                                            ──▶ appointments.json

    Attributes:
        agent_card  : This agent's A2A identity and capability declaration.
        _mcp_client : The MCP client used to call data retrieval tools.
        _name       : Short display name used in log output.
    """

    # ------------------------------------------------------------------
    # Agent identity constants
    # ------------------------------------------------------------------
    AGENT_ID = "records-agent-001"
    AGENT_NAME = "RecordsAgent"
    ENDPOINT = "http://localhost:8002/a2a"  # simulated — not a real server

    def __init__(self, mcp_server: MCPServer) -> None:
        """
        Initialise the RecordsAgent.

        Creates the agent's AgentCard (A2A identity) and connects an
        MCPClient to the provided MCP Server (data access layer).

        Args:
            mcp_server: The shared MCPServer instance that all agents use.
                        Using a single shared server instance mirrors how
                        multiple microservices connect to a shared database
                        or API gateway in a real healthcare architecture.
        """
        self._name = self.AGENT_NAME

        # ── A2A: AgentCard ────────────────────────────────────────────
        # This card is registered in the A2ARegistry by the Orchestrator.
        # The capabilities list is indexed by the registry for discovery:
        # when the orchestrator asks for "get_patient_records", the registry
        # finds THIS card and returns its endpoint for task delivery.
        self.agent_card = AgentCard(
            agent_id=self.AGENT_ID,
            name=self.AGENT_NAME,
            description=(
                "Specialist agent for retrieving and summarising patient "
                "medical records. Provides structured record data and "
                "narrative medical history summaries."
            ),
            capabilities=["get_patient_records", "get_medical_history"],
            endpoint=self.ENDPOINT,
            version="1.0.0",
            auth_scheme="Bearer",
            metadata={
                "data_access": "read-only",
                "phi_handling": "HIPAA-compliant (simulated)",
                "supported_formats": "structured, narrative",
            },
        )

        # ── MCP: Client connection ────────────────────────────────────
        # The MCPClient wraps all calls to the MCP Server, adding [MCP]
        # log lines so the full data-access chain is visible in output.
        # client_id="RecordsAgent" tags every [MCP] log line so you can
        # distinguish this agent's tool calls from those of other agents.
        self._mcp_client = MCPClient(
            server=mcp_server,
            client_id=self.AGENT_NAME,
        )

        _log(
            self._name,
            f"{_GREEN}Initialised.{_RESET} AgentCard ready. MCP connected.",
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
        deployment this logic would sit behind an HTTP POST /a2a handler.
        The A2AClient on the orchestrator side sends a serialised A2ATask;
        this method processes it and returns a serialised A2AResponse.

        Dispatch logic:
          • intent == "get_patient_records"  → _handle_get_patient_records()
          • intent == "get_medical_history"  → _handle_get_medical_history()
          • anything else                    → failed A2AResponse

        Error handling:
          All exceptions are caught and surfaced as failed A2AResponse objects
          rather than raised. This keeps the OrchestratorAgent's aggregation
          loop simple — it can always safely inspect response.status without
          needing try/except wrappers around every A2A call.

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

        try:
            if task.intent == "get_patient_records":
                return self._handle_get_patient_records(task)
            elif task.intent == "get_medical_history":
                return self._handle_get_medical_history(task)
            else:
                msg = (
                    f"Unknown intent '{task.intent}'. "
                    f"Supported: get_patient_records, get_medical_history"
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
            # Surface unexpected errors as structured failed responses so
            # the orchestrator can handle them gracefully and continue
            # aggregating results from the other sub-agents.
            msg = f"RecordsAgent encountered an unexpected error: {exc}"
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

    def _handle_get_patient_records(self, task: A2ATask) -> A2AResponse:
        """
        Handle the "get_patient_records" intent.

        Returns raw structured data suitable for machine consumption or
        display in a structured UI (e.g., a patient dashboard).

        STEPS:
          1. Validate that patient_id is present in the payload.
          2. [MCP] Call get_patient_record to fetch demographics + conditions.
          3. [MCP] Call get_appointments to fetch scheduled visits.
          4. Combine both into a single structured result dict.
          5. Return A2AResponse with the combined data.

        Expected payload keys:
            patient_id : str — e.g. "P001"

        Result dict keys:
            patient_id        : str
            demographics      : dict  (name, dob, doctor, conditions)
            appointments      : list  (all scheduled appointments)
            conditions        : list  (active medical conditions)
            appointment_count : int
            summary           : str   (one-line plain-English summary)

        Args:
            task: The A2ATask containing patient_id in its payload.

        Returns:
            A2AResponse with structured patient record in result dict,
            or a failed response if patient_id is missing or not found.
        """
        payload = task.payload

        # ── Validate required fields ──────────────────────────────────
        if "patient_id" not in payload:
            msg = "Missing required payload field: 'patient_id'"
            _log(self._name, f"{_RED}✗ VALIDATION ERROR:{_RESET} {msg}")
            return A2AResponse(
                task_id=task.task_id,
                agent_id=self.AGENT_ID,
                status="failed",
                result={"missing_fields": ["patient_id"]},
                message=msg,
            )

        patient_id = payload["patient_id"]
        _log(self._name, f"Fetching records for patient {patient_id!r} ...")

        # ── Step 1: Fetch patient demographics via MCP ────────────────
        # WHY: Demographics (name, DOB, assigned doctor, known conditions)
        # are the foundation of every clinical workflow. We fetch them
        # first so we can build a complete picture of the patient.
        _log(self._name, f"{_YELLOW}→ [MCP] Fetching patient demographics ...{_RESET}")
        patient_result = self._mcp_client.call_tool(
            "get_patient_record",
            patient_id=patient_id,
        )

        if patient_result["status"] != "ok":
            msg = f"Could not retrieve patient record: {patient_result['message']}"
            _log(self._name, f"{_RED}✗ MCP ERROR:{_RESET} {msg}")
            return A2AResponse(
                task_id=task.task_id,
                agent_id=self.AGENT_ID,
                status="failed",
                result=patient_result,
                message=msg,
            )

        patient_data = patient_result["data"]
        patient_name = patient_data.get("name", "Unknown")
        conditions: List[str] = patient_data.get("conditions", [])
        _log(
            self._name,
            f"{_GREEN}✓ Demographics retrieved:{_RESET} {patient_name}  "
            f"conditions={conditions}",
        )

        # ── Step 2: Fetch appointments via MCP ────────────────────────
        # WHY: Appointments give us the patient's care continuity context —
        # when they last visited, upcoming appointments, and whether they
        # are actively engaged with their care plan.
        _log(self._name, f"{_YELLOW}→ [MCP] Fetching appointments ...{_RESET}")
        appt_result = self._mcp_client.call_tool(
            "get_appointments",
            patient_id=patient_id,
        )

        if appt_result["status"] != "ok":
            msg = f"Could not retrieve appointments: {appt_result['message']}"
            _log(self._name, f"{_RED}✗ MCP ERROR:{_RESET} {msg}")
            return A2AResponse(
                task_id=task.task_id,
                agent_id=self.AGENT_ID,
                status="failed",
                result=appt_result,
                message=msg,
            )

        appointments: List[Dict[str, Any]] = appt_result["data"]
        appt_count = len(appointments)
        _log(
            self._name,
            f"{_GREEN}✓ Appointments retrieved:{_RESET} {appt_count} on record",
        )

        # ── Assemble combined structured result ───────────────────────
        conditions_str = ", ".join(conditions) if conditions else "none on record"
        summary = (
            f"Patient {patient_name} (ID: {patient_id}) — "
            f"Assigned to {patient_data.get('doctor', 'unassigned')} — "
            f"Active conditions: {conditions_str} — "
            f"{appt_count} appointment(s) on record."
        )

        _log(self._name, f"{_GREEN}✓ RECORDS ASSEMBLED:{_RESET} {summary}")

        return A2AResponse(
            task_id=task.task_id,
            agent_id=self.AGENT_ID,
            status="completed",
            result={
                "patient_id": patient_id,
                "demographics": patient_data,
                "appointments": appointments,
                "conditions": conditions,
                "appointment_count": appt_count,
                "summary": summary,
            },
            message=summary,
        )

    def _handle_get_medical_history(self, task: A2ATask) -> A2AResponse:
        """
        Handle the "get_medical_history" intent.

        Returns a narrative-style medical history suitable for display
        to a clinician, or for injection into an LLM's context window
        as structured background knowledge about the patient.

        Compared to "get_patient_records", this intent:
          - Produces a richer, multi-section narrative string
          - Categorises appointments by status (confirmed vs pending)
          - Highlights upcoming visits and their proximity
          - Flags conditions that may require ongoing monitoring

        STEPS:
          1. Validate that patient_id is present in the payload.
          2. [MCP] Call get_patient_record for demographics + conditions.
          3. [MCP] Call get_appointments for full appointment history.
          4. Build a narrative history string with multiple sections.
          5. Return A2AResponse with the narrative and underlying raw data.

        Expected payload keys:
            patient_id : str — e.g. "P001"

        Result dict keys:
            patient_id        : str
            patient_name      : str
            narrative         : str   (multi-line formatted medical history)
            raw_demographics  : dict
            raw_appointments  : list
            conditions        : list
            upcoming_appts    : list  (status == "confirmed" or "pending")
            history_generated_at : str  (ISO timestamp)

        Args:
            task: The A2ATask containing patient_id in its payload.

        Returns:
            A2AResponse with the narrative history in result["narrative"],
            or a failed response if patient_id is missing or not found.
        """
        payload = task.payload

        # ── Validate required fields ──────────────────────────────────
        if "patient_id" not in payload:
            msg = "Missing required payload field: 'patient_id'"
            _log(self._name, f"{_RED}✗ VALIDATION ERROR:{_RESET} {msg}")
            return A2AResponse(
                task_id=task.task_id,
                agent_id=self.AGENT_ID,
                status="failed",
                result={"missing_fields": ["patient_id"]},
                message=msg,
            )

        patient_id = payload["patient_id"]
        _log(self._name, f"Compiling medical history for patient {patient_id!r} ...")

        # ── Fetch demographics via MCP ────────────────────────────────
        _log(self._name, f"{_YELLOW}→ [MCP] Fetching patient demographics ...{_RESET}")
        patient_result = self._mcp_client.call_tool(
            "get_patient_record",
            patient_id=patient_id,
        )

        if patient_result["status"] != "ok":
            msg = f"Could not retrieve patient record: {patient_result['message']}"
            _log(self._name, f"{_RED}✗ MCP ERROR:{_RESET} {msg}")
            return A2AResponse(
                task_id=task.task_id,
                agent_id=self.AGENT_ID,
                status="failed",
                result=patient_result,
                message=msg,
            )

        patient_data = patient_result["data"]

        # ── Fetch appointments via MCP ────────────────────────────────
        _log(self._name, f"{_YELLOW}→ [MCP] Fetching appointment history ...{_RESET}")
        appt_result = self._mcp_client.call_tool(
            "get_appointments",
            patient_id=patient_id,
        )

        if appt_result["status"] != "ok":
            msg = f"Could not retrieve appointments: {appt_result['message']}"
            _log(self._name, f"{_RED}✗ MCP ERROR:{_RESET} {msg}")
            return A2AResponse(
                task_id=task.task_id,
                agent_id=self.AGENT_ID,
                status="failed",
                result=appt_result,
                message=msg,
            )

        appointments: List[Dict[str, Any]] = appt_result["data"]

        # ── Build the narrative medical history ───────────────────────
        narrative = self._build_narrative(patient_data, appointments)

        # Separate upcoming (active) appointments from historical ones
        active_statuses = {"confirmed", "pending"}
        upcoming = [
            a for a in appointments if a.get("status", "").lower() in active_statuses
        ]

        patient_name = patient_data.get("name", "Unknown")
        generated_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        _log(
            self._name,
            f"{_GREEN}✓ HISTORY COMPILED:{_RESET} "
            f"{patient_name} — {len(appointments)} appt(s), "
            f"{len(upcoming)} upcoming",
        )

        return A2AResponse(
            task_id=task.task_id,
            agent_id=self.AGENT_ID,
            status="completed",
            result={
                "patient_id": patient_id,
                "patient_name": patient_name,
                "narrative": narrative,
                "raw_demographics": patient_data,
                "raw_appointments": appointments,
                "conditions": patient_data.get("conditions", []),
                "upcoming_appts": upcoming,
                "history_generated_at": generated_at,
            },
            message=(
                f"Medical history compiled for {patient_name} "
                f"({len(appointments)} appointment(s), "
                f"{len(patient_data.get('conditions', []))} active condition(s))."
            ),
        )

    # ------------------------------------------------------------------
    # Narrative builder
    # ------------------------------------------------------------------

    def _build_narrative(
        self,
        patient: Dict[str, Any],
        appointments: List[Dict[str, Any]],
    ) -> str:
        """
        Construct a formatted multi-section narrative from raw MCP data.

        WHY A NARRATIVE?
        ----------------
        In healthcare IT, two representations of patient data are common:
          1. Structured data  (FHIR resources, HL7 segments) — for machines
          2. Narrative text   (CDA documents, clinical notes) — for humans
             and LLM context windows

        This method produces format (2): a plain-text narrative that a
        clinician can read at a glance, or that an LLM orchestrator can
        inject into a prompt as patient context before answering questions.

        Format:
            ╔══ MEDICAL HISTORY REPORT ══╗
            Patient:    Alice Johnson (P001)
            DOB:        1985-03-15
            ...
            ─── Active Medical Conditions ───
            • hypertension
            • diabetes
            ─── Appointment History ───
            • [A001] 2025-08-10 at 09:00 — Dr. Smith (confirmed)
            ...

        Args:
            patient     : Raw patient dict from get_patient_record MCP tool.
            appointments: Raw appointment list from get_appointments MCP tool.

        Returns:
            A formatted string containing the complete medical history narrative.
        """
        lines: List[str] = []

        # ── Header ────────────────────────────────────────────────────
        lines.append("╔══════════════════════════════════════════════╗")
        lines.append("║         MEDICAL HISTORY REPORT               ║")
        lines.append("╚══════════════════════════════════════════════╝")
        lines.append("")

        # ── Patient demographics ──────────────────────────────────────
        lines.append("─── Patient Demographics ─────────────────────")
        lines.append(f"  Name       : {patient.get('name', 'N/A')}")
        lines.append(f"  Patient ID : {patient.get('id', 'N/A')}")
        lines.append(f"  Date of Birth: {patient.get('dob', 'N/A')}")
        lines.append(f"  Primary Doctor: {patient.get('doctor', 'N/A')}")
        lines.append("")

        # ── Active conditions ─────────────────────────────────────────
        conditions: List[str] = patient.get("conditions", [])
        lines.append("─── Active Medical Conditions ────────────────")
        if conditions:
            for condition in conditions:
                lines.append(f"  • {condition.title()}")
        else:
            lines.append("  • No active conditions on record")
        lines.append("")

        # ── Appointment history ───────────────────────────────────────
        lines.append("─── Appointment History ──────────────────────")
        if appointments:
            # Sort appointments by date descending (most recent first)
            sorted_appts = sorted(
                appointments,
                key=lambda a: (a.get("date", ""), a.get("time", "")),
                reverse=True,
            )
            for appt in sorted_appts:
                appt_id = appt.get("id", "N/A")
                date = appt.get("date", "N/A")
                time = appt.get("time", "N/A")
                doctor = appt.get("doctor", "N/A")
                status = appt.get("status", "unknown").upper()
                lines.append(f"  [{appt_id}] {date} at {time} — {doctor}  [{status}]")
        else:
            lines.append("  • No appointments on record")
        lines.append("")

        # ── Clinical notes (auto-generated observations) ──────────────
        lines.append("─── Clinical Notes (Auto-Generated) ─────────")
        notes = self._generate_clinical_notes(conditions, appointments)
        for note in notes:
            lines.append(f"  ⚕ {note}")
        if not notes:
            lines.append("  ⚕ No automated notes generated.")
        lines.append("")

        lines.append("─────────────────────────────────────────────")
        generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"  Generated by RecordsAgent at {generated_at}")
        lines.append("─────────────────────────────────────────────")

        return "\n".join(lines)

    def _generate_clinical_notes(
        self,
        conditions: List[str],
        appointments: List[Dict[str, Any]],
    ) -> List[str]:
        """
        Generate rule-based automated clinical observations.

        In a real system this logic would be replaced by a clinical decision
        support (CDS) engine or an LLM with medical knowledge. Here we use
        simple rules to demonstrate the concept of automated health insights
        being surfaced alongside raw records.

        Rules applied:
          - Hypertension + diabetes → flag for regular monitoring reminder
          - 0 upcoming appointments → flag for follow-up scheduling
          - Expired appointment status → note historical visit

        Args:
            conditions  : List of active condition strings for this patient.
            appointments: Full list of appointment dicts for this patient.

        Returns:
            A list of plain-English clinical observation strings.
        """
        notes: List[str] = []
        cond_lower = [c.lower() for c in conditions]

        # ── Condition-based observations ──────────────────────────────
        if "hypertension" in cond_lower:
            notes.append(
                "Patient has hypertension — ensure blood pressure is "
                "monitored at every visit."
            )
        if "diabetes" in cond_lower:
            notes.append(
                "Patient has diabetes — regular HbA1c checks and dietary "
                "counselling recommended."
            )
        if "hypertension" in cond_lower and "diabetes" in cond_lower:
            notes.append(
                "Comorbid hypertension + diabetes: heightened cardiovascular "
                "risk — consider cardiology referral if not already in place."
            )
        if "asthma" in cond_lower:
            notes.append(
                "Patient has asthma — confirm rescue inhaler is current and "
                "review action plan at next visit."
            )
        if "migraine" in cond_lower:
            notes.append(
                "Patient has migraines — review trigger diary and current "
                "prophylactic therapy at next appointment."
            )

        # ── Appointment-based observations ────────────────────────────
        upcoming = [
            a
            for a in appointments
            if a.get("status", "").lower() in {"confirmed", "pending"}
        ]
        if not upcoming:
            notes.append(
                "No upcoming appointments scheduled — consider proactive "
                "outreach to book a follow-up visit."
            )
        elif len(upcoming) == 1:
            next_appt = upcoming[0]
            notes.append(
                f"Next visit: {next_appt.get('date')} at {next_appt.get('time')} "
                f"with {next_appt.get('doctor')} [{next_appt.get('status')}]."
            )

        return notes

    # ------------------------------------------------------------------
    # Dunder helpers
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return f"RecordsAgent(agent_id={self.AGENT_ID!r}, endpoint={self.ENDPOINT!r})"
