"""
MCP Server - Model Context Protocol Server Implementation
==========================================================
PROTOCOL CONCEPT: MCP Server
------------------------------
In the Model Context Protocol, a "Server" is a process that exposes a set of
named *tools* to any connected client (agent). Each tool has:
  - A unique name          (used to call it)
  - A human-readable description  (so an LLM agent can decide which to use)
  - A callable function    (the actual implementation)

The server acts as the bridge between AI agents and real-world data sources.
Here, our "data sources" are the JSON files in the data/ directory, simulating
actual medical databases that would live behind a secure API in production.

WHY MCP?
--------
Without MCP, every agent would need custom, hard-coded logic to query each
database. With MCP, agents just say "call tool X with args Y" and the server
handles everything. This makes agents:
  - Composable  : any agent can use any tool
  - Maintainable: change DB logic in one place (the server)
  - Discoverable: agents can list available tools at runtime

TOOLS IMPLEMENTED:
------------------
  1. get_patient_record(patient_id)
  2. get_appointments(patient_id)
  3. book_appointment(patient_id, date, time, doctor)
  4. get_prescriptions(patient_id)
  5. list_available_slots(doctor, date)
"""

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

# Resolve the data directory relative to this file so the server works
# regardless of where Python is invoked from.
_DATA_DIR = Path(__file__).parent.parent / "data"


def _load_json(filename: str) -> Any:
    """Load and return the contents of a JSON file from the data directory."""
    filepath = _DATA_DIR / filename
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_json(filename: str, data: Any) -> None:
    """Persist data back to a JSON file in the data directory."""
    filepath = _DATA_DIR / filename
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# Tool result helpers
# ---------------------------------------------------------------------------


def _ok(data: Any, message: str = "Success") -> Dict[str, Any]:
    """Wrap a successful tool result in a standard envelope."""
    return {"status": "ok", "message": message, "data": data}


def _err(message: str) -> Dict[str, Any]:
    """Wrap an error result in a standard envelope."""
    return {"status": "error", "message": message, "data": None}


# ---------------------------------------------------------------------------
# Individual tool implementations
# Each function is a pure callable that represents one MCP tool.
# ---------------------------------------------------------------------------


def _tool_get_patient_record(patient_id: str) -> Dict[str, Any]:
    """
    MCP Tool: get_patient_record
    ----------------------------
    Retrieves the full patient record for a given patient ID.

    In a real system this would query a HIPAA-compliant EHR (Electronic
    Health Record) database. Here we read from patients.json.

    Args:
        patient_id: The unique patient identifier (e.g. "P001").

    Returns:
        Standard MCP result envelope containing the patient dict, or an
        error envelope if the patient is not found.
    """
    patients = _load_json("patients.json")
    for patient in patients:
        if patient["id"] == patient_id:
            return _ok(patient, f"Patient record found for {patient_id}")
    return _err(f"No patient found with ID '{patient_id}'")


def _tool_get_appointments(patient_id: str) -> Dict[str, Any]:
    """
    MCP Tool: get_appointments
    --------------------------
    Retrieves all appointments (past and future) for a given patient.

    Args:
        patient_id: The unique patient identifier.

    Returns:
        Standard MCP result envelope containing a list of appointment dicts.
    """
    appointments = _load_json("appointments.json")
    patient_appts = [a for a in appointments if a["patient_id"] == patient_id]
    if patient_appts:
        return _ok(
            patient_appts,
            f"Found {len(patient_appts)} appointment(s) for {patient_id}",
        )
    return _ok([], f"No appointments found for patient {patient_id}")


def _tool_book_appointment(
    patient_id: str,
    date: str,
    time: str,
    doctor: str,
) -> Dict[str, Any]:
    """
    MCP Tool: book_appointment
    --------------------------
    Creates a new appointment entry and persists it to the appointments store.

    In production this would:
      - Check doctor availability in a calendar system
      - Send confirmation emails/SMS via a notification service
      - Write to a transactional database with ACID guarantees

    Here we generate a unique appointment ID and append to appointments.json.

    Args:
        patient_id : The patient requesting the appointment.
        date       : Appointment date in YYYY-MM-DD format.
        time       : Appointment time in HH:MM format (24-hour).
        doctor     : The doctor's name (e.g. "Dr. Lee").

    Returns:
        Standard MCP result envelope containing the newly created appointment.
    """
    appointments = _load_json("appointments.json")

    # Generate a simple sequential ID based on existing count
    new_id = f"A{str(len(appointments) + 1).zfill(3)}"

    new_appointment = {
        "id": new_id,
        "patient_id": patient_id,
        "date": date,
        "time": time,
        "doctor": doctor,
        "status": "confirmed",
        "booked_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }

    appointments.append(new_appointment)
    _save_json("appointments.json", appointments)

    return _ok(
        new_appointment,
        f"Appointment {new_id} successfully booked for patient {patient_id} "
        f"with {doctor} on {date} at {time}",
    )


def _tool_get_prescriptions(patient_id: str) -> Dict[str, Any]:
    """
    MCP Tool: get_prescriptions
    ---------------------------
    Retrieves all active prescriptions for a given patient.

    In production this would query a pharmacy management system (PMS) or
    the state's Prescription Drug Monitoring Program (PDMP) database.

    Args:
        patient_id: The unique patient identifier.

    Returns:
        Standard MCP result envelope containing a list of prescription dicts,
        each with medication name, refills remaining, and last fill date.
    """
    prescriptions = _load_json("prescriptions.json")
    patient_rxs = [p for p in prescriptions if p["patient_id"] == patient_id]
    if patient_rxs:
        return _ok(
            patient_rxs,
            f"Found {len(patient_rxs)} prescription(s) for {patient_id}",
        )
    return _ok([], f"No prescriptions found for patient {patient_id}")


def _tool_list_available_slots(doctor: str, date: str) -> Dict[str, Any]:
    """
    MCP Tool: list_available_slots
    ------------------------------
    Returns available appointment time slots for a specific doctor on a date.

    In production this would query the doctor's calendar in a scheduling
    system (e.g. Epic, Calendly, or a custom booking backend), filtering
    out already-booked slots.

    Here we generate a fixed set of candidate slots and subtract any that
    are already booked in appointments.json for that doctor and date.

    Args:
        doctor: The doctor's name (e.g. "Dr. Smith").
        date  : The date to check in YYYY-MM-DD format.

    Returns:
        Standard MCP result envelope containing a list of available time
        strings (e.g. ["09:00", "10:00", "11:00"]).
    """
    # Simulated full daily schedule (30-minute intervals, 9am-5pm)
    all_slots: List[str] = [
        "09:00",
        "09:30",
        "10:00",
        "10:30",
        "11:00",
        "11:30",
        "13:00",
        "13:30",
        "14:00",
        "14:30",
        "15:00",
        "15:30",
        "16:00",
        "16:30",
    ]

    # Remove already-booked slots by checking the appointments store
    appointments = _load_json("appointments.json")
    booked_times = {
        a["time"] for a in appointments if a["doctor"] == doctor and a["date"] == date
    }

    available = [slot for slot in all_slots if slot not in booked_times]

    return _ok(
        {"doctor": doctor, "date": date, "available_slots": available},
        f"Found {len(available)} available slot(s) for {doctor} on {date}",
    )


# ---------------------------------------------------------------------------
# MCPServer class
# ---------------------------------------------------------------------------


class MCPServer:
    """
    Simulated MCP (Model Context Protocol) Server.

    PROTOCOL ROLE: Server
    ----------------------
    The MCP Server is the authoritative registry of tools. It:
      1. Registers tools at startup (name, description, callable)
      2. Exposes a dispatch mechanism so clients can call tools by name
      3. Returns structured result envelopes (status + data)

    In a real MCP deployment this class would run as a separate process
    and communicate over HTTP/SSE or stdio using the MCP JSON-RPC wire
    format. Here, the class simulates that server in-process.

    Attributes:
        name   : Human-readable server name (shown in logs / discovery).
        tools  : Registry mapping tool_name -> tool metadata dict.
    """

    def __init__(self, name: str = "MediAssist MCP Server") -> None:
        self.name = name
        # tools registry: { tool_name: { "description": str, "fn": Callable } }
        self.tools: Dict[str, Dict[str, Any]] = {}
        self._register_all_tools()

    # ------------------------------------------------------------------
    # Tool registration
    # ------------------------------------------------------------------

    def register_tool(
        self,
        name: str,
        description: str,
        fn: Callable,
    ) -> None:
        """
        Register a callable as a named MCP tool.

        This is the MCP concept of "tool declaration". In the real spec,
        the server advertises each tool's name, description, and JSON Schema
        for its parameters. Clients (agents) use the description to decide
        *which* tool to call for a given task.

        Args:
            name        : Unique tool identifier used when calling the tool.
            description : Natural-language explanation of what the tool does.
            fn          : Python callable that implements the tool logic.
        """
        self.tools[name] = {
            "name": name,
            "description": description,
            "fn": fn,
        }

    def _register_all_tools(self) -> None:
        """Register every tool this server exposes on startup."""

        self.register_tool(
            name="get_patient_record",
            description=(
                "Retrieve the full medical record for a patient, including "
                "their name, date of birth, assigned doctor, and known "
                "medical conditions. Requires patient_id."
            ),
            fn=_tool_get_patient_record,
        )

        self.register_tool(
            name="get_appointments",
            description=(
                "Fetch all scheduled appointments for a given patient. "
                "Returns appointment date, time, doctor, and status. "
                "Requires patient_id."
            ),
            fn=_tool_get_appointments,
        )

        self.register_tool(
            name="book_appointment",
            description=(
                "Book a new appointment for a patient with a specific doctor "
                "on a given date and time. Requires patient_id, date "
                "(YYYY-MM-DD), time (HH:MM), and doctor name."
            ),
            fn=_tool_book_appointment,
        )

        self.register_tool(
            name="get_prescriptions",
            description=(
                "Retrieve all active prescriptions for a patient, including "
                "medication names, refills remaining, and last fill date. "
                "Requires patient_id."
            ),
            fn=_tool_get_prescriptions,
        )

        self.register_tool(
            name="list_available_slots",
            description=(
                "List all open appointment time slots for a specific doctor "
                "on a specific date. Useful before booking an appointment. "
                "Requires doctor name and date (YYYY-MM-DD)."
            ),
            fn=_tool_list_available_slots,
        )

    # ------------------------------------------------------------------
    # Tool dispatch
    # ------------------------------------------------------------------

    def call_tool(self, tool_name: str, **kwargs: Any) -> Dict[str, Any]:
        """
        Dispatch a tool call by name with keyword arguments.

        This is the core MCP Server operation. The client sends a tool name
        and arguments; the server looks up the tool in its registry and
        executes it, returning a structured result.

        Args:
            tool_name : The registered name of the tool to invoke.
            **kwargs  : Keyword arguments forwarded to the tool function.

        Returns:
            A result envelope dict with keys: status, message, data.

        Raises:
            KeyError: If the tool_name is not registered (unknown tool).
        """
        if tool_name not in self.tools:
            available = list(self.tools.keys())
            return _err(f"Unknown tool '{tool_name}'. Available tools: {available}")

        tool_fn = self.tools[tool_name]["fn"]
        try:
            return tool_fn(**kwargs)
        except TypeError as exc:
            return _err(f"Invalid arguments for tool '{tool_name}': {exc}")
        except Exception as exc:  # noqa: BLE001
            return _err(f"Tool '{tool_name}' raised an unexpected error: {exc}")

    # ------------------------------------------------------------------
    # Introspection helpers
    # ------------------------------------------------------------------

    def list_tools(self) -> List[Dict[str, str]]:
        """
        Return a summary of all registered tools.

        In real MCP this corresponds to the 'tools/list' request that a
        client sends when it first connects to discover what the server
        can do. The response includes each tool's name and description
        (and, in the full spec, a JSON Schema for its parameters).
        """
        return [
            {"name": t["name"], "description": t["description"]}
            for t in self.tools.values()
        ]

    def __repr__(self) -> str:
        tool_names = list(self.tools.keys())
        return f"MCPServer(name={self.name!r}, tools={tool_names})"
