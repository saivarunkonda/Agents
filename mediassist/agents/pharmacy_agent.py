"""
Pharmacy Agent — A2A Sub-Agent for Prescription Management
===========================================================
PROTOCOL ROLES:
  • A2A Server   — This agent registers an AgentCard and exposes a
                   process_task() endpoint that the OrchestratorAgent calls.
  • MCP Client   — Internally, this agent uses an MCPClient to reach the
                   MCP Server for prescription data.

RESPONSIBILITIES:
-----------------
The PharmacyAgent is the prescription and medication specialist in MediAssist.
It retrieves prescription records, evaluates refill eligibility, and simulates
refill requests — acting as the "pharmacy counter" function in a clinical workflow.

Supported A2A intents (declared in its AgentCard capabilities):
  • "check_prescriptions"  — Retrieve all active prescriptions and their status.
  • "request_refill"       — Attempt to request a refill for a specific medication.

FLOW FOR "check_prescriptions":
  1. OrchestratorAgent discovers this agent via A2ARegistry.
  2. Orchestrator sends an A2ATask with intent="check_prescriptions" and
     payload={"patient_id": "P001"}
  3. process_task() receives the task.
  4. Agent calls MCP tool "get_prescriptions" for all active medications.
  5. Agent analyses each prescription's refill count and last-fill date.
  6. Agent returns an A2AResponse with a full status report.

FLOW FOR "request_refill":
  1. Orchestrator sends A2ATask with intent="request_refill" and
     payload={"patient_id": "P001", "medication": "Metformin 500mg"}
  2. process_task() routes to _handle_request_refill().
  3. Agent calls MCP tool "get_prescriptions" to check refills_remaining.
  4. If refills_remaining > 0: simulates an approved refill and returns success.
  5. If refills_remaining == 0: returns a structured "requires_authorization"
     response advising the patient to contact their prescribing doctor.

WHY SEPARATE check_prescriptions AND request_refill?
------------------------------------------------------
"check_prescriptions" is a read-only status query (safe to call at any time),
while "request_refill" is a write-intent that has real-world side effects
(decrements refill count, triggers pharmacy fulfilment, notifies insurance).
Separating them mirrors real pharmacy system APIs (e.g. Surescripts, NCPDP SCRIPT)
that treat medication status queries and refill transactions as distinct operations
with different auth scopes and audit requirements.
"""

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from a2a.a2a_protocol import A2AResponse, A2ATask
from a2a.agent_card import AgentCard
from mcp.mcp_client import MCPClient
from mcp.mcp_server import MCPServer

# ---------------------------------------------------------------------------
# ANSI colour codes for console output
# ---------------------------------------------------------------------------
_ORANGE = "\033[38;5;208m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_RED = "\033[91m"
_BLUE = "\033[94m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RESET = "\033[0m"


def _log(agent_name: str, text: str) -> None:
    """Print a formatted agent log line with a timestamp."""
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"{_ORANGE}{_BOLD}[{agent_name}]{_RESET} [{ts}] {text}")


# ---------------------------------------------------------------------------
# Refill status constants
# ---------------------------------------------------------------------------

# Threshold: prescriptions with this many or fewer refills are considered "low"
_LOW_REFILL_THRESHOLD = 1

# Number of days after the last fill before a refill is typically eligible
# (simplified rule — real pharmacy systems use days-supply calculations)
_MIN_DAYS_BETWEEN_FILLS = 25


# ---------------------------------------------------------------------------
# PharmacyAgent
# ---------------------------------------------------------------------------


class PharmacyAgent:
    """
    A2A Sub-Agent — Prescription & Pharmacy Specialist.

    PROTOCOL ROLE: A2A Server (sub-agent / skill agent)
    ----------------------------------------------------
    In A2A terminology, this agent acts as a *server* — it exposes a
    process_task() method that the OrchestratorAgent calls (via A2AClient).
    It publishes an AgentCard that advertises:
      - What it can do    (capabilities: check_prescriptions, request_refill)
      - Where to reach it (endpoint — simulated URL)
      - How to auth       (auth_scheme: Bearer)

    The OrchestratorAgent registers this card in the A2ARegistry at startup.
    During a "full_checkup" request, the orchestrator discovers and calls this
    agent alongside RecordsAgent and SchedulingAgent, then merges all three
    A2AResponses into a unified patient summary for the user.

    PROTOCOL ROLE: MCP Client (data access)
    ----------------------------------------
    Internally, this agent uses an MCPClient to invoke the prescriptions tool
    on the shared MCP Server:
      • get_prescriptions(patient_id)  — fetches all active prescriptions

    The agent never reads prescriptions.json directly. All data access is
    mediated by the MCP layer, preserving the clean separation of concerns:

        OrchestratorAgent
            ──[A2A]──▶ PharmacyAgent.process_task()
                            ──[MCP]──▶ MCPServer.get_prescriptions()
                                            ──▶ prescriptions.json

    CLINICAL CONTEXT:
    -----------------
    In a real healthcare system this agent would interface with:
      - The state's Prescription Drug Monitoring Program (PDMP) database
      - A pharmacy benefits manager (PBM) for insurance adjudication
      - The NCPDP SCRIPT standard for e-prescribing transactions
      - The prescribing doctor's EHR for prior authorisation workflows

    Here we simulate all of that with prescriptions.json and rule-based logic.

    Attributes:
        agent_card  : This agent's A2A identity and capability declaration.
        _mcp_client : The MCP client used to call prescription tools.
        _name       : Short display name used in log output.
    """

    # ------------------------------------------------------------------
    # Agent identity constants
    # ------------------------------------------------------------------
    AGENT_ID = "pharmacy-agent-001"
    AGENT_NAME = "PharmacyAgent"
    ENDPOINT = "http://localhost:8003/a2a"  # simulated — not a real server

    def __init__(self, mcp_server: MCPServer) -> None:
        """
        Initialise the PharmacyAgent.

        Creates the agent's AgentCard (A2A identity) and connects an
        MCPClient to the provided MCP Server (data access layer).

        Args:
            mcp_server: The shared MCPServer instance that all agents use.
                        Using a single shared server instance mirrors how
                        multiple microservices connect to a shared pharmacy
                        database or PBM API gateway in a real architecture.
        """
        self._name = self.AGENT_NAME

        # ── A2A: AgentCard ────────────────────────────────────────────
        # This card is registered in the A2ARegistry by the Orchestrator.
        # The capabilities list is indexed for discovery: when the
        # orchestrator asks for "check_prescriptions", the registry finds
        # THIS card and returns its endpoint for task delivery.
        self.agent_card = AgentCard(
            agent_id=self.AGENT_ID,
            name=self.AGENT_NAME,
            description=(
                "Specialist agent for patient prescription management. "
                "Checks active medication status, refill counts, and "
                "processes refill requests with eligibility validation."
            ),
            capabilities=["check_prescriptions", "request_refill"],
            endpoint=self.ENDPOINT,
            version="1.0.0",
            auth_scheme="Bearer",
            metadata={
                "data_access": "read + simulated-write",
                "refill_processing": "rule-based (no real pharmacy)",
                "pdmp_integration": "simulated",
                "prior_auth_support": False,
            },
        )

        # ── MCP: Client connection ────────────────────────────────────
        # The MCPClient wraps all calls to the MCP Server, adding [MCP]
        # log lines so the full data-access chain is visible in output.
        # client_id="PharmacyAgent" tags every [MCP] log line so you can
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
          • intent == "check_prescriptions"  → _handle_check_prescriptions()
          • intent == "request_refill"       → _handle_request_refill()
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
            if task.intent == "check_prescriptions":
                return self._handle_check_prescriptions(task)
            elif task.intent == "request_refill":
                return self._handle_request_refill(task)
            else:
                msg = (
                    f"Unknown intent '{task.intent}'. "
                    f"Supported: check_prescriptions, request_refill"
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
            msg = f"PharmacyAgent encountered an unexpected error: {exc}"
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

    def _handle_check_prescriptions(self, task: A2ATask) -> A2AResponse:
        """
        Handle the "check_prescriptions" intent.

        Returns a complete prescription status report for the patient,
        including per-medication refill analysis and actionable alerts
        (e.g., "low refills", "refill due soon", "needs doctor auth").

        STEPS:
          1. Validate that patient_id is present in the payload.
          2. [MCP] Call get_prescriptions to fetch all active medications.
          3. Analyse each prescription for refill eligibility and status.
          4. Aggregate into a summary report with alerts.
          5. Return A2AResponse with full prescription data.

        Expected payload keys:
            patient_id : str — e.g. "P001"

        Result dict keys:
            patient_id          : str
            prescriptions       : list  (raw prescription dicts from MCP)
            prescription_count  : int
            analysed            : list  (enriched dicts with status fields)
            alerts              : list  (actionable warning strings)
            refill_needed_count : int   (prescriptions with 0 refills left)
            low_refill_count    : int   (prescriptions at or below threshold)
            summary             : str   (one-line plain-English summary)

        Args:
            task: The A2ATask containing patient_id in its payload.

        Returns:
            A2AResponse with prescription report in result dict,
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
        _log(self._name, f"Checking prescriptions for patient {patient_id!r} ...")

        # ── Fetch prescriptions via MCP ───────────────────────────────
        # WHY: All prescription data goes through the MCP layer, keeping
        # the PharmacyAgent decoupled from the storage format. If the
        # backend switches from JSON files to a real PBM API, only the
        # MCP Server changes — this agent stays the same.
        _log(self._name, f"{_YELLOW}→ [MCP] Fetching prescription records ...{_RESET}")
        rx_result = self._mcp_client.call_tool(
            "get_prescriptions",
            patient_id=patient_id,
        )

        if rx_result["status"] != "ok":
            msg = f"Could not retrieve prescriptions: {rx_result['message']}"
            _log(self._name, f"{_RED}✗ MCP ERROR:{_RESET} {msg}")
            return A2AResponse(
                task_id=task.task_id,
                agent_id=self.AGENT_ID,
                status="failed",
                result=rx_result,
                message=msg,
            )

        prescriptions: List[Dict[str, Any]] = rx_result["data"]
        rx_count = len(prescriptions)

        if rx_count == 0:
            msg = f"No active prescriptions found for patient {patient_id}."
            _log(self._name, f"{_YELLOW}ℹ NO PRESCRIPTIONS:{_RESET} {msg}")
            return A2AResponse(
                task_id=task.task_id,
                agent_id=self.AGENT_ID,
                status="completed",
                result={
                    "patient_id": patient_id,
                    "prescriptions": [],
                    "prescription_count": 0,
                    "analysed": [],
                    "alerts": [],
                    "refill_needed_count": 0,
                    "low_refill_count": 0,
                    "summary": msg,
                },
                message=msg,
            )

        _log(
            self._name,
            f"{_GREEN}✓ Retrieved {rx_count} prescription(s).{_RESET} "
            f"Analysing refill status ...",
        )

        # ── Analyse each prescription ─────────────────────────────────
        analysed: List[Dict[str, Any]] = []
        alerts: List[str] = []
        refill_needed_count = 0
        low_refill_count = 0

        for rx in prescriptions:
            analysis = self._analyse_prescription(rx)
            analysed.append(analysis)

            medication = rx.get("medication", "Unknown")
            refills = rx.get("refills_remaining", 0)
            status_tag = analysis["refill_status"]

            # Log each medication's analysis result
            status_colour = (
                _GREEN
                if status_tag == "ok"
                else _YELLOW
                if status_tag == "low"
                else _RED
            )
            _log(
                self._name,
                f"  {medication:<30} refills={refills}  "
                f"status={status_colour}{status_tag.upper()}{_RESET}",
            )

            # Accumulate alerts
            if analysis["alerts"]:
                alerts.extend(analysis["alerts"])

            if refills == 0:
                refill_needed_count += 1
            elif refills <= _LOW_REFILL_THRESHOLD:
                low_refill_count += 1

        # ── Build summary string ──────────────────────────────────────
        summary_parts = [f"Patient {patient_id}: {rx_count} active prescription(s)."]
        if refill_needed_count:
            summary_parts.append(
                f"{refill_needed_count} require doctor authorisation for refill."
            )
        if low_refill_count:
            summary_parts.append(f"{low_refill_count} have low refills remaining.")
        if not refill_needed_count and not low_refill_count:
            summary_parts.append("All prescriptions have adequate refills.")
        summary = " ".join(summary_parts)

        _log(self._name, f"{_GREEN}✓ PRESCRIPTION REPORT READY:{_RESET} {summary}")

        return A2AResponse(
            task_id=task.task_id,
            agent_id=self.AGENT_ID,
            status="completed",
            result={
                "patient_id": patient_id,
                "prescriptions": prescriptions,
                "prescription_count": rx_count,
                "analysed": analysed,
                "alerts": alerts,
                "refill_needed_count": refill_needed_count,
                "low_refill_count": low_refill_count,
                "summary": summary,
            },
            message=summary,
        )

    def _handle_request_refill(self, task: A2ATask) -> A2AResponse:
        """
        Handle the "request_refill" intent.

        Validates refill eligibility for a specific medication and, if eligible,
        simulates an approved refill transaction. Returns a structured response
        describing the outcome — whether approved, denied, or requiring doctor
        authorisation.

        CLINICAL RULES APPLIED:
          1. Refills remaining > 0  → APPROVED  (auto-refill, no auth needed)
          2. Refills remaining == 0 → REQUIRES_AUTHORISATION
             (patient must contact prescribing doctor for a new Rx or auth code)
          3. Medication not found   → NOT_FOUND (patient/medication mismatch)

        NOTE: In this simulation we do NOT actually decrement refills_remaining
        in the JSON file. A production pharmacy system would:
          - Submit an NCPDP SCRIPT transaction to the pharmacy
          - Receive a claim adjudication response from the PBM
          - Decrement the refill count in the prescriber's EHR
          - Trigger fulfilment (dispensing + shipping/pickup notification)

        Expected payload keys:
            patient_id : str — e.g. "P001"
            medication : str — must match medication name in prescriptions.json
                               (e.g. "Metformin 500mg")

        Result dict keys:
            patient_id       : str
            medication       : str
            outcome          : str   ("approved" | "requires_authorisation" | "not_found")
            refills_before   : int   (refills count before this request)
            refills_after    : int   (refills count after — same in simulation)
            prescription     : dict  (the matching prescription record, or None)
            next_steps       : list  (actionable instructions for the patient)

        Args:
            task: The A2ATask containing patient_id and medication in payload.

        Returns:
            A2AResponse with refill outcome in result dict,
            or a failed response if required fields are missing.
        """
        payload = task.payload

        # ── Validate required fields ──────────────────────────────────
        required = ["patient_id", "medication"]
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
        medication = payload["medication"]

        _log(
            self._name,
            f"Processing refill request: "
            f"patient={patient_id!r}, medication={medication!r}",
        )

        # ── Fetch all prescriptions for this patient via MCP ──────────
        _log(
            self._name,
            f"{_YELLOW}→ [MCP] Fetching prescriptions for eligibility check ...{_RESET}",
        )
        rx_result = self._mcp_client.call_tool(
            "get_prescriptions",
            patient_id=patient_id,
        )

        if rx_result["status"] != "ok":
            msg = f"Could not retrieve prescriptions: {rx_result['message']}"
            _log(self._name, f"{_RED}✗ MCP ERROR:{_RESET} {msg}")
            return A2AResponse(
                task_id=task.task_id,
                agent_id=self.AGENT_ID,
                status="failed",
                result=rx_result,
                message=msg,
            )

        prescriptions: List[Dict[str, Any]] = rx_result["data"]

        # ── Locate the matching prescription ─────────────────────────
        # Match is case-insensitive and trims surrounding whitespace for
        # robustness (real pharmacy APIs often have normalised drug names).
        target_rx: Optional[Dict[str, Any]] = None
        for rx in prescriptions:
            if rx.get("medication", "").strip().lower() == medication.strip().lower():
                target_rx = rx
                break

        # ── Handle "not found" case ───────────────────────────────────
        if target_rx is None:
            all_meds = [rx.get("medication", "?") for rx in prescriptions]
            msg = (
                f"Medication '{medication}' not found in patient {patient_id}'s "
                f"prescriptions. On record: {all_meds}"
            )
            _log(self._name, f"{_RED}✗ MEDICATION NOT FOUND:{_RESET} {msg}")
            return A2AResponse(
                task_id=task.task_id,
                agent_id=self.AGENT_ID,
                status="completed",  # task ran successfully; outcome is "not_found"
                result={
                    "patient_id": patient_id,
                    "medication": medication,
                    "outcome": "not_found",
                    "refills_before": None,
                    "refills_after": None,
                    "prescription": None,
                    "next_steps": [
                        "Verify the medication name matches exactly as prescribed.",
                        f"Available medications: {all_meds}",
                        "Contact your pharmacy or prescribing doctor if you believe "
                        "this is an error.",
                    ],
                },
                message=msg,
            )

        refills_remaining: int = target_rx.get("refills_remaining", 0)
        last_filled: str = target_rx.get("last_filled", "unknown")

        _log(
            self._name,
            f"Found {medication!r}: refills_remaining={refills_remaining}, "
            f"last_filled={last_filled!r}",
        )

        # ── Evaluate eligibility and determine outcome ─────────────────
        if refills_remaining > 0:
            # ── APPROVED: refills available ───────────────────────────
            # Simulate approval. In production this would submit an
            # NCPDP SCRIPT RxFill transaction to the dispensing pharmacy.
            today_str = datetime.now().strftime("%Y-%m-%d")
            msg = (
                f"Refill APPROVED for '{medication}' (patient {patient_id}). "
                f"Refills remaining after this request: {refills_remaining - 1}. "
                f"Estimated ready date: {today_str} (same-day processing)."
            )
            _log(self._name, f"{_GREEN}✓ REFILL APPROVED:{_RESET} {msg}")

            next_steps = [
                f"Your refill for {medication} has been approved.",
                "Your pharmacy will process it within 2–4 hours.",
                "You will receive a notification when it is ready for pickup or dispatch.",
            ]
            if refills_remaining - 1 == 0:
                next_steps.append(
                    "⚠ This is your LAST authorised refill. Please schedule an "
                    "appointment with your doctor to renew your prescription."
                )
            elif refills_remaining - 1 <= _LOW_REFILL_THRESHOLD:
                next_steps.append(
                    f"⚠ Only {refills_remaining - 1} refill(s) will remain after this. "
                    "Consider scheduling a review with your doctor soon."
                )

            return A2AResponse(
                task_id=task.task_id,
                agent_id=self.AGENT_ID,
                status="completed",
                result={
                    "patient_id": patient_id,
                    "medication": medication,
                    "outcome": "approved",
                    "refills_before": refills_remaining,
                    "refills_after": refills_remaining - 1,  # simulated decrement
                    "prescription": target_rx,
                    "processed_date": today_str,
                    "next_steps": next_steps,
                },
                message=msg,
            )

        else:
            # ── REQUIRES AUTHORISATION: no refills remaining ──────────
            # This is NOT an error — it is a valid clinical outcome.
            # The patient must obtain a new prescription or prior-auth
            # code from their prescribing doctor before the pharmacy can
            # dispense. Returning status="completed" with outcome=
            # "requires_authorisation" lets the orchestrator handle it
            # gracefully (e.g., automatically booking a doctor appointment).
            msg = (
                f"Refill DENIED for '{medication}' (patient {patient_id}): "
                f"no refills remaining. Doctor authorisation required."
            )
            _log(self._name, f"{_YELLOW}⚠ REQUIRES AUTHORISATION:{_RESET} {msg}")

            return A2AResponse(
                task_id=task.task_id,
                agent_id=self.AGENT_ID,
                status="completed",  # task completed; outcome just requires action
                result={
                    "patient_id": patient_id,
                    "medication": medication,
                    "outcome": "requires_authorisation",
                    "refills_before": 0,
                    "refills_after": 0,
                    "prescription": target_rx,
                    "next_steps": [
                        f"Your prescription for {medication} has no refills remaining.",
                        "Please contact your prescribing doctor to request a new "
                        "prescription or a refill authorisation code.",
                        "You can book an appointment through the MediAssist scheduling "
                        "system.",
                        "In urgent cases, your pharmacy may be able to dispense an "
                        "emergency supply — please ask your pharmacist.",
                    ],
                },
                message=msg,
            )

    # ------------------------------------------------------------------
    # Analysis helpers
    # ------------------------------------------------------------------

    def _analyse_prescription(self, rx: Dict[str, Any]) -> Dict[str, Any]:
        """
        Enrich a raw prescription dict with computed status fields and alerts.

        This method adds derived fields that aren't stored in the database
        but are important for patient-facing display and clinical decision
        support. It mirrors what a pharmacy management system (PMS) would
        compute before showing a prescription on its UI.

        Refill status classification:
          "ok"       — refills_remaining > LOW_REFILL_THRESHOLD
          "low"      — 0 < refills_remaining <= LOW_REFILL_THRESHOLD
          "empty"    — refills_remaining == 0  (needs doctor auth)

        Eligibility classification (based on days since last fill):
          "eligible"     — enough days have passed since last fill
          "too_soon"     — last fill was too recent (possible early refill)
          "unknown"      — last_filled date could not be parsed

        Args:
            rx: A raw prescription dict from the MCP Server's get_prescriptions
                tool, with keys: patient_id, medication, refills_remaining,
                last_filled.

        Returns:
            A new dict containing all original fields plus:
              - refill_status  : "ok" | "low" | "empty"
              - eligibility    : "eligible" | "too_soon" | "unknown"
              - days_since_fill: int or None
              - alerts         : list of warning strings
        """
        medication: str = rx.get("medication", "Unknown")
        refills_remaining: int = rx.get("refills_remaining", 0)
        last_filled_str: str = rx.get("last_filled", "")

        # ── Refill status ─────────────────────────────────────────────
        if refills_remaining == 0:
            refill_status = "empty"
        elif refills_remaining <= _LOW_REFILL_THRESHOLD:
            refill_status = "low"
        else:
            refill_status = "ok"

        # ── Eligibility (days since last fill) ────────────────────────
        days_since_fill: Optional[int] = None
        eligibility = "unknown"

        if last_filled_str:
            try:
                last_filled_date = datetime.strptime(last_filled_str, "%Y-%m-%d").date()
                today = datetime.now().date()
                days_since_fill = (today - last_filled_date).days
                eligibility = (
                    "eligible"
                    if days_since_fill >= _MIN_DAYS_BETWEEN_FILLS
                    else "too_soon"
                )
            except ValueError:
                # Date in unexpected format — mark as unknown, don't crash
                eligibility = "unknown"

        # ── Build alerts ──────────────────────────────────────────────
        alerts: List[str] = []

        if refill_status == "empty":
            alerts.append(
                f"{medication}: No refills remaining — doctor authorisation required."
            )
        elif refill_status == "low":
            alerts.append(
                f"{medication}: Only {refills_remaining} refill(s) left — "
                f"consider scheduling a prescription review soon."
            )

        if eligibility == "too_soon" and days_since_fill is not None:
            alerts.append(
                f"{medication}: Last filled {days_since_fill} day(s) ago — "
                f"refill typically eligible after {_MIN_DAYS_BETWEEN_FILLS} days."
            )

        return {
            **rx,  # include all original fields
            "refill_status": refill_status,
            "eligibility": eligibility,
            "days_since_fill": days_since_fill,
            "alerts": alerts,
        }

    def _format_prescription_report(
        self,
        prescriptions: List[Dict[str, Any]],
        analysed: List[Dict[str, Any]],
        alerts: List[str],
        patient_id: str,
    ) -> str:
        """
        Build a formatted multi-line prescription status report string.

        Produces a human-readable report (and LLM-friendly context block)
        summarising all of the patient's active prescriptions with their
        refill status and any actionable alerts.

        Format:
            ╔══ PRESCRIPTION STATUS REPORT ══╗
            Patient ID: P001
            ─── Active Prescriptions ───
              [1] Metformin 500mg
                  Refills remaining : 2    Status : OK
                  Last filled       : 2025-07-01  (28 days ago)
                  Eligibility       : Eligible for refill
              ...
            ─── Alerts ───
              ⚠ Lisinopril 10mg: No refills remaining ...

        Args:
            prescriptions : Raw prescription list from MCP.
            analysed      : Enriched list from _analyse_prescription().
            alerts        : Aggregated alert strings from all prescriptions.
            patient_id    : The patient's ID for the report header.

        Returns:
            A formatted string containing the full prescription status report.
        """
        lines: List[str] = []
        lines.append("╔══════════════════════════════════════════════╗")
        lines.append("║       PRESCRIPTION STATUS REPORT             ║")
        lines.append("╚══════════════════════════════════════════════╝")
        lines.append(f"  Patient ID : {patient_id}")
        lines.append(f"  Generated  : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("")

        lines.append("─── Active Prescriptions ─────────────────────")
        for i, (rx, analysis) in enumerate(zip(prescriptions, analysed), start=1):
            medication = rx.get("medication", "Unknown")
            refills = rx.get("refills_remaining", 0)
            last_filled = rx.get("last_filled", "N/A")
            status = analysis.get("refill_status", "unknown").upper()
            eligibility = analysis.get("eligibility", "unknown").title()
            days = analysis.get("days_since_fill")
            days_str = f"({days} days ago)" if days is not None else ""

            lines.append(f"  [{i}] {medication}")
            lines.append(f"      Refills remaining : {refills:<4}  Status : {status}")
            lines.append(f"      Last filled       : {last_filled}  {days_str}")
            lines.append(f"      Eligibility       : {eligibility}")
            lines.append("")

        if alerts:
            lines.append("─── Alerts ───────────────────────────────────")
            for alert in alerts:
                lines.append(f"  ⚠ {alert}")
            lines.append("")
        else:
            lines.append("─── Alerts ───────────────────────────────────")
            lines.append("  ✓ No alerts — all prescriptions are in good standing.")
            lines.append("")

        lines.append("─────────────────────────────────────────────")
        lines.append("  Generated by PharmacyAgent | MediAssist v1.0")
        lines.append("─────────────────────────────────────────────")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Dunder helpers
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return f"PharmacyAgent(agent_id={self.AGENT_ID!r}, endpoint={self.ENDPOINT!r})"
