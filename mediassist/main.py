"""
MediAssist — Healthcare Agent System
main.py  |  Entry Point & Demo Runner
======================================
This script bootstraps the MediAssist multi-agent system and executes three
real-world demo scenarios that exercise both the MCP and A2A protocols end-to-end.

WHAT THIS FILE DEMONSTRATES:
------------------------------
  Scenario 1 — Full Patient Checkup (P001 / Alice Johnson)
      The richest interaction: the OrchestratorAgent fans out A2A tasks to
      ALL THREE sub-agents simultaneously, each of which fires MCP tool calls
      to retrieve patient records, prescriptions, and availability data. The
      orchestrator then aggregates all three responses into one unified report.

      Protocols on show:
        A2A  — 3× discovery queries, 3× task dispatches, 3× responses
        MCP  — get_patient_record, get_appointments, get_prescriptions,
               list_available_slots

  Scenario 2 — Book an Appointment (P002 / Bob Williams)
      Demonstrates the "write path": discovery of the SchedulingAgent,
      availability verification, and a new appointment being persisted
      via the MCP book_appointment tool.

      Protocols on show:
        A2A  — 1× discovery query, 1× task dispatch, 1× response
        MCP  — list_available_slots, book_appointment

  Scenario 3 — Prescription Status Check (P001 / Alice Johnson)
      A focused read-only query showing how the PharmacyAgent surfaces
      per-medication refill status, eligibility analysis, and actionable
      alerts for medications that need doctor authorisation.

      Protocols on show:
        A2A  — 1× discovery query, 1× task dispatch, 1× response
        MCP  — get_prescriptions

HOW TO RUN:
-----------
    # From the project root:
    python main.py

    # Or explicitly:
    python Agent/project1_mediassist/main.py

REQUIREMENTS:
    Python 3.7+  (uses dataclasses, f-strings, pathlib — all stdlib)
    No external packages required.

OUTPUT GUIDE:
    [MCP]              — Model Context Protocol: tool calls to the MCP Server
    [A2A]              — Agent-to-Agent: task dispatches between agents
    [OrchestratorAgent]— The main coordinator's log messages
    [SchedulingAgent]  — Appointment specialist's log messages
    [RecordsAgent]     — Medical records specialist's log messages
    [PharmacyAgent]    — Pharmacy specialist's log messages
"""

import io
import sys
import time
from pathlib import Path
from typing import Any, Dict

# ---------------------------------------------------------------------------
# Windows UTF-8 console fix
# ---------------------------------------------------------------------------
# Windows terminals default to cp1252, which cannot encode the Unicode
# box-drawing characters (═ ║ ╔ ╗ etc.) used in the demo output.
# Reconfigure stdout/stderr to UTF-8 so all characters render correctly.
# `errors="replace"` ensures a single bad character never crashes the whole run.
# We use isinstance(io.TextIOWrapper) so type-checkers know .reconfigure exists.
if isinstance(sys.stdout, io.TextIOWrapper):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if isinstance(sys.stderr, io.TextIOWrapper):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
# Add the project root to sys.path so all package imports work correctly
# regardless of where Python is invoked from.
_PROJECT_ROOT = Path(__file__).parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Imports (after path setup)
# ---------------------------------------------------------------------------
from agents.orchestrator_agent import OrchestratorAgent

# ---------------------------------------------------------------------------
# ANSI colour constants
# ---------------------------------------------------------------------------
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RESET = "\033[0m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_RED = "\033[91m"
_CYAN = "\033[96m"
_MAGENTA = "\033[95m"
_BLUE = "\033[94m"
_WHITE = "\033[97m"

# Wide border strings used in section banners
_DOUBLE = "═" * 70
_SINGLE = "─" * 70
_WAVE = "≈" * 70


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _banner(title: str, subtitle: str = "", colour: str = _CYAN) -> None:
    """Print a wide double-border banner for a top-level demo section."""
    print(f"\n{colour}{_BOLD}")
    print(f"╔{_DOUBLE}╗")
    print(f"║  {title:<68}║")
    if subtitle:
        print(f"║  {_DIM}{subtitle:<68}{_RESET}{colour}{_BOLD}║")
    print(f"╚{_DOUBLE}╝{_RESET}")


def _divider(label: str = "", colour: str = _DIM) -> None:
    """Print a thin divider line, optionally with a centred label."""
    if label:
        pad = max(0, 68 - len(label))
        left = pad // 2
        right = pad - left
        print(f"\n{colour}{'─' * left}  {label}  {'─' * right}{_RESET}\n")
    else:
        print(f"{colour}{_SINGLE}{_RESET}")


def _scenario_header(num: int, title: str, patient: str, desc: str) -> None:
    """Print a prominent scenario header with patient info and description."""
    colour = [_CYAN, _YELLOW, _MAGENTA][num - 1]
    print(f"\n\n{colour}{_BOLD}")
    print(f"┏{_DOUBLE}┓")
    print(f"┃  SCENARIO {num}: {title:<57}┃")
    print(f"┃  Patient : {patient:<58}┃")
    print(f"┃  {_DIM}{desc:<68}{_RESET}{colour}{_BOLD}┃")
    print(f"┗{_DOUBLE}┛{_RESET}\n")


def _protocol_legend() -> None:
    """Print the colour/tag legend so readers know what each prefix means."""
    print(f"{_DIM}")
    print(f"  ┌─ OUTPUT LEGEND {'─' * 52}┐")
    print(
        f"  │  {_RESET}{_CYAN}{_BOLD}[MCP]{_RESET}{_DIM}               "
        f"Model Context Protocol — tool call to MCP Server          │"
    )
    print(
        f"  │  {_RESET}{_MAGENTA}{_BOLD}[A2A]{_RESET}{_DIM}               "
        f"Agent-to-Agent Protocol — task dispatch between agents     │"
    )
    print(
        f"  │  {_RESET}{_BLUE}{_BOLD}[OrchestratorAgent]{_RESET}{_DIM}  "
        f"Main coordinator log messages                              │"
    )
    print(
        f"  │  {_RESET}{_YELLOW}{_BOLD}[SchedulingAgent]{_RESET}{_DIM}    "
        f"Appointment booking specialist                             │"
    )
    print(
        f"  │  {_RESET}\033[94m{_BOLD}[RecordsAgent]{_RESET}{_DIM}       "
        f"Patient medical records specialist                         │"
    )
    print(
        f"  │  {_RESET}\033[38;5;208m{_BOLD}[PharmacyAgent]{_RESET}{_DIM}      "
        f"Prescription & pharmacy specialist                         │"
    )
    print(f"  └{'─' * 69}┘{_RESET}\n")


def _print_result_summary(result: Dict[str, Any], scenario_num: int) -> None:
    """Print a compact summary of a handle_request() result dict."""
    status = result.get("status", "unknown")
    status_icon = (
        f"{_GREEN}✓ SUCCESS{_RESET}"
        if status == "success"
        else f"{_YELLOW}~ PARTIAL{_RESET}"
        if status == "partial_success"
        else f"{_RED}✗ FAILED{_RESET}"
    )
    errors = result.get("errors", [])
    colour = [_CYAN, _YELLOW, _MAGENTA][scenario_num - 1]

    print(f"\n{colour}{_BOLD}┌─ Scenario {scenario_num} Outcome {'─' * 52}┐{_RESET}")
    print(f"  Status     : {status_icon}")
    print(f"  Request    : {result.get('request_type', '?')!r}")
    print(f"  Patient    : {result.get('patient_id', '?')!r}")
    print(f"  Timestamp  : {result.get('timestamp', '?')}")
    if errors:
        print(f"  Errors     :")
        for err in errors:
            print(f"    {_RED}• {err}{_RESET}")
    else:
        print(f"  Errors     : {_GREEN}none{_RESET}")
    print(f"{colour}{_BOLD}└{'─' * 68}┘{_RESET}")


# ---------------------------------------------------------------------------
# Individual scenario runners
# ---------------------------------------------------------------------------


def run_scenario_1(orchestrator: OrchestratorAgent) -> Dict[str, Any]:
    """
    Scenario 1 — Full Patient Checkup for P001 (Alice Johnson).

    Demonstrates A2A fan-out: the Orchestrator calls all three sub-agents
    (RecordsAgent, PharmacyAgent, SchedulingAgent) and aggregates the results.

    Protocol interactions:
      A2A:
        • discover("get_medical_history")  → RecordsAgent
        • send_task → RecordsAgent.process_task(intent="get_medical_history")
        • discover("check_prescriptions")  → PharmacyAgent
        • send_task → PharmacyAgent.process_task(intent="check_prescriptions")
        • discover("check_availability")   → SchedulingAgent
        • send_task → SchedulingAgent.process_task(intent="check_availability")

      MCP (called inside sub-agents):
        • RecordsAgent   → get_patient_record("P001"), get_appointments("P001")
        • PharmacyAgent  → get_prescriptions("P001")
        • SchedulingAgent→ list_available_slots("Dr. Smith", "2025-08-20")
    """
    _scenario_header(
        num=1,
        title="Full Patient Checkup",
        patient="P001 — Alice Johnson",
        desc=(
            "Fan-out to ALL 3 sub-agents: records + prescriptions + availability. "
            "Shows richest A2A + MCP interaction."
        ),
    )

    result = orchestrator.handle_request(
        patient_id="P001",
        request_type="full_checkup",
        doctor="Dr. Smith",  # optional: check availability for Alice's doctor
        date="2025-08-20",  # optional: on this date
    )

    _print_result_summary(result, scenario_num=1)
    return result


def run_scenario_2(orchestrator: OrchestratorAgent) -> Dict[str, Any]:
    """
    Scenario 2 — Book an Appointment for P002 (Bob Williams).

    Demonstrates A2A discovery + write delegation: the Orchestrator
    discovers the SchedulingAgent, checks slot availability via MCP,
    then books a new appointment that is persisted to appointments.json.

    Protocol interactions:
      A2A:
        • discover("schedule_appointment") → SchedulingAgent
        • send_task → SchedulingAgent.process_task(intent="schedule_appointment")

      MCP (called inside SchedulingAgent):
        • list_available_slots("Dr. Lee", "2025-08-15")
        • book_appointment("P002", "2025-08-15", "10:00", "Dr. Lee")
    """
    _scenario_header(
        num=2,
        title="Book an Appointment",
        patient="P002 — Bob Williams",
        desc=(
            "A2A discovery → SchedulingAgent. MCP: check availability, "
            "then book_appointment (writes to appointments.json)."
        ),
    )

    result = orchestrator.handle_request(
        patient_id="P002",
        request_type="book_appointment",
        date="2025-08-15",
        time="10:00",
        doctor="Dr. Lee",
    )

    _print_result_summary(result, scenario_num=2)
    return result


def run_scenario_3(orchestrator: OrchestratorAgent) -> Dict[str, Any]:
    """
    Scenario 3 — Prescription Status Check for P001 (Alice Johnson).

    Demonstrates A2A discovery + read delegation for the pharmacy domain.
    Alice has two medications: Metformin 500mg (2 refills left) and
    Lisinopril 10mg (0 refills — needs doctor auth). The PharmacyAgent
    surfaces this disparity with per-medication analysis and actionable alerts.

    Protocol interactions:
      A2A:
        • discover("check_prescriptions") → PharmacyAgent
        • send_task → PharmacyAgent.process_task(intent="check_prescriptions")

      MCP (called inside PharmacyAgent):
        • get_prescriptions("P001")
    """
    _scenario_header(
        num=3,
        title="Prescription Status Check",
        patient="P001 — Alice Johnson",
        desc=(
            "A2A discovery → PharmacyAgent. MCP: get_prescriptions. "
            "Shows refill analysis + alerts for medications needing doctor auth."
        ),
    )

    result = orchestrator.handle_request(
        patient_id="P001",
        request_type="prescriptions",
    )

    _print_result_summary(result, scenario_num=3)
    return result


# ---------------------------------------------------------------------------
# Post-run analysis
# ---------------------------------------------------------------------------


def _print_protocol_analysis(results: list) -> None:
    """
    Print an educational breakdown of which protocol was used at each step
    across all three scenarios. Helps the reader map log output → protocol.
    """
    print(f"\n{_MAGENTA}{_BOLD}")
    print(f"╔{_DOUBLE}╗")
    print(f"║  PROTOCOL FLOW ANALYSIS — How MCP + A2A Worked Together{' ' * 13}║")
    print(f"╚{_DOUBLE}╝{_RESET}")

    sections = [
        (
            _CYAN,
            "A2A PROTOCOL  (Agent-to-Agent)",
            [
                "Registry Registration  — All 3 sub-agents published AgentCards at startup.",
                "  Cards carry: agent_id, name, capabilities[], endpoint, auth_scheme.",
                "",
                "Discovery  — OrchestratorAgent called registry.discover(capability)",
                "  rather than hard-coding which agent to call. This means agents can",
                "  be swapped or added without changing the orchestrator.",
                "",
                "Task Dispatch  — A2AClient.send_task() performed 3 protocol steps:",
                "  1. Capability check  — verified AgentCard declares the intent",
                "  2. Auth simulation   — added Bearer token header",
                "  3. HTTP POST sim     — called agent.process_task(A2ATask)",
                "",
                "Response  — Each sub-agent returned an A2AResponse with:",
                "  task_id (correlation), agent_id, status, result{}, message",
                "",
                "Fan-out (Scenario 1)  — Orchestrator called all 3 agents and merged",
                "  3 × A2AResponse objects into one unified checkup report.",
            ],
        ),
        (
            _YELLOW,
            "MCP PROTOCOL  (Model Context Protocol)",
            [
                "Server  — MCPServer registered 5 tools at startup:",
                "  get_patient_record, get_appointments, book_appointment,",
                "  get_prescriptions, list_available_slots",
                "",
                "Client  — Each sub-agent owns an MCPClient instance.",
                "  Agents call: mcp_client.call_tool('tool_name', **kwargs)",
                "  The client logs [MCP] → CALL and [MCP] ← RESULT for every call.",
                "",
                "Tools called across all 3 scenarios:",
                "  get_patient_record(P001)              [RecordsAgent, Scenario 1]",
                "  get_appointments(P001)                [RecordsAgent, Scenario 1]",
                "  get_prescriptions(P001)               [PharmacyAgent, Scenarios 1+3]",
                "  list_available_slots(Dr.Smith, ...)   [SchedulingAgent, Scenario 1]",
                "  list_available_slots(Dr.Lee, ...)     [SchedulingAgent, Scenario 2]",
                "  book_appointment(P002, ...)           [SchedulingAgent, Scenario 2]",
                "",
                "Separation of concerns  — Agents never read JSON files directly.",
                "  All data access is mediated by the MCP layer.",
            ],
        ),
        (
            _GREEN,
            "KEY ARCHITECTURAL DECISIONS",
            [
                "1. Orchestrator has NO MCP client  — it only uses A2A. Data access",
                "   is fully delegated to specialist sub-agents. This keeps the",
                "   orchestrator replaceable with an LLM planner without data coupling.",
                "",
                "2. Sub-agents are A2A servers AND MCP clients simultaneously.",
                "   Each agent is a skill-specific node in the protocol stack:",
                "   User → [A2A] → OrchestratorAgent → [A2A] → SubAgent → [MCP] → Data",
                "",
                "3. AgentCard capabilities drive routing  — the orchestrator never",
                "   imports or names sub-agents directly. It asks the registry for",
                "   'who can do X?' and delegates to whoever responds.",
                "",
                "4. Errors stay in-band  — all agents return A2AResponse(status=failed)",
                "   instead of raising exceptions, keeping orchestrator logic simple.",
            ],
        ),
    ]

    for colour, heading, bullets in sections:
        print(f"\n  {colour}{_BOLD}{'─' * 66}")
        print(f"  {heading}")
        print(f"  {'─' * 66}{_RESET}")
        for bullet in bullets:
            if bullet == "":
                print()
            else:
                print(f"  {_DIM}{bullet}{_RESET}")


def _print_data_file_note() -> None:
    """Note that appointments.json was modified by Scenario 2."""
    print(
        f"\n{_YELLOW}{_BOLD}  ┌─ Data File Side-Effect ─────────────────────────────────────────┐"
    )
    print(
        f"  │{_RESET}  Scenario 2 called the MCP {_BOLD}book_appointment{_RESET}{_YELLOW}{_BOLD} tool, which          │"
    )
    print(
        f"  │{_RESET}  appended a new appointment to {_BOLD}data/appointments.json{_RESET}{_YELLOW}{_BOLD}.          │"
    )
    print(
        f"  │{_RESET}  Check the file to see the persisted booking!                     │"
    )
    print(
        f"  └─────────────────────────────────────────────────────────────────┘{_RESET}"
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """
    Main demo runner for the MediAssist Healthcare Agent System.

    Bootstraps one OrchestratorAgent (which internally creates all sub-agents,
    the MCP Server, and the A2A Registry), then runs all three demo scenarios
    in sequence, printing the full protocol trace as they execute.

    Exit codes:
        0  — all scenarios completed (even with partial/failed outcomes)
        1  — fatal error during system bootstrap (e.g., missing data files)
    """
    # ── Welcome banner ────────────────────────────────────────────────────
    print(f"\n{_BOLD}{_WHITE}")
    print(f"{'═' * 72}")
    print(f"  {'MediAssist — Healthcare Agent System':^68}")
    print(f"  {'Multi-Agent Demo: MCP + A2A Protocols':^68}")
    print(f"{'═' * 72}{_RESET}")
    print(
        f"\n  {_DIM}This demo shows how AI agents collaborate using two open protocols:\n"
        f"  • MCP  (Model Context Protocol)  — agents ↔ data/tools\n"
        f"  • A2A  (Agent-to-Agent Protocol) — agents ↔ agents\n{_RESET}"
    )
    _protocol_legend()

    # ── System bootstrap ──────────────────────────────────────────────────
    # All initialisation (MCP Server, sub-agents, A2A Registry) happens
    # inside OrchestratorAgent.__init__(). Watch the [A2A] REGISTERED and
    # [MCP] connected log lines appear here.
    print(f"{_BOLD}  Initialising system...{_RESET}\n")
    try:
        orchestrator = OrchestratorAgent()
    except Exception as exc:
        print(f"\n{_RED}{_BOLD}FATAL: Could not initialise OrchestratorAgent:{_RESET}")
        print(f"  {exc}")
        print(
            f"\n  Make sure you are running from the project root directory and\n"
            f"  that the data/ directory contains patients.json, appointments.json,\n"
            f"  and prescriptions.json.\n"
        )
        sys.exit(1)

    _divider("System Ready — Starting Demo Scenarios")

    # ── Scenario 1: Full Checkup ──────────────────────────────────────────
    result_1 = run_scenario_1(orchestrator)

    _divider()
    input(
        f"\n  {_DIM}[ Press ENTER to continue to Scenario 2: Book an Appointment ]{_RESET}  "
    ) if _is_interactive() else _pause(0.5)

    # ── Scenario 2: Book Appointment ──────────────────────────────────────
    result_2 = run_scenario_2(orchestrator)

    _divider()
    input(
        f"\n  {_DIM}[ Press ENTER to continue to Scenario 3: Prescription Check ]{_RESET}  "
    ) if _is_interactive() else _pause(0.5)

    # ── Scenario 3: Prescription Check ───────────────────────────────────
    result_3 = run_scenario_3(orchestrator)

    _divider()

    # ── Session Summary ───────────────────────────────────────────────────
    orchestrator.print_session_summary()

    # ── Protocol Analysis ─────────────────────────────────────────────────
    _print_protocol_analysis([result_1, result_2, result_3])

    # ── Data file side-effect note ────────────────────────────────────────
    _print_data_file_note()

    # ── Closing banner ────────────────────────────────────────────────────
    print(f"\n{_BOLD}{_WHITE}")
    print(f"{'═' * 72}")
    print(f"  {'Demo Complete':^68}")
    print(f"  {'All 3 scenarios executed — MCP + A2A protocols demonstrated':^68}")
    print(f"{'═' * 72}{_RESET}\n")


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def _is_interactive() -> bool:
    """
    Return True if we appear to be running in an interactive terminal.

    Used to decide whether to pause with input() (interactive) or a short
    time.sleep() (non-interactive, e.g., CI / piped output).
    """
    return sys.stdout.isatty() and sys.stdin.isatty()


def _pause(seconds: float) -> None:
    """Sleep briefly so demo output is easier to follow in non-interactive mode."""
    time.sleep(seconds)


# ---------------------------------------------------------------------------
# Entry point guard
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
