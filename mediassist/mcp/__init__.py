"""
MCP (Model Context Protocol) Package
=====================================
This package implements a simulated version of the Model Context Protocol.

WHAT IS MCP?
------------
MCP is a protocol that allows AI agents to connect to external "tool servers"
in a standardized way. Instead of hard-coding how to call a database or API,
an agent discovers and invokes *tools* exposed by an MCP Server.

Think of it like USB for AI agents:
  - The MCP Server exposes a set of named tools (functions) with descriptions.
  - The MCP Client connects to the server and calls those tools by name.
  - The agent never needs to know the underlying implementation details.

HOW IT IS USED HERE:
--------------------
  MCP Server  -->  Wraps access to: patients.json, appointments.json,
                   prescriptions.json (simulated medical databases)

  MCP Client  -->  Used by each sub-agent (SchedulingAgent, RecordsAgent,
                   PharmacyAgent) to call server tools like:
                     - get_patient_record(patient_id)
                     - book_appointment(patient_id, date, time, doctor)
                     - get_prescriptions(patient_id)

In a real MCP implementation the client would communicate over a network
transport (HTTP/SSE or stdio). Here we simulate that with direct Python
calls, preserving all the protocol semantics (tool registry, named dispatch,
structured results).
"""

from .mcp_client import MCPClient
from .mcp_server import MCPServer

__all__ = ["MCPServer", "MCPClient"]
