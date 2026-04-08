"""
MCP Client - Model Context Protocol Client Implementation
==========================================================
PROTOCOL CONCEPT: MCP Client
------------------------------
In the Model Context Protocol, a "Client" is the consumer-side component
that lives inside an AI agent. Its responsibilities are:

  1. CONNECT   - Establish a session with an MCP Server (here: hold a
                 reference to the MCPServer instance; in production this
                 would be an HTTP/SSE or stdio connection).

  2. DISCOVER  - Ask the server which tools are available via list_tools().
                 An LLM-powered agent would feed these descriptions into
                 its context window to decide which tool to call.

  3. INVOKE    - Call a specific tool by name with structured arguments,
                 receive a structured result envelope back.

  4. LOG       - Every call is logged with a [MCP] prefix so the protocol
                 flow is visible. In production this feeds into tracing
                 systems (OpenTelemetry, LangSmith, etc.).

WHY A SEPARATE CLIENT CLASS?
-----------------------------
Separating the client from the server means:
  - Agents never import server internals (loose coupling).
  - The client can be swapped for a real HTTP client without changing agents.
  - All tool-call logging, error handling, and retries live in one place.

HOW IT IS USED:
---------------
  Each sub-agent (SchedulingAgent, RecordsAgent, PharmacyAgent) holds its
  own MCPClient instance and calls tools like:

      result = mcp_client.call_tool("get_patient_record", patient_id="P001")

  The client logs the call, dispatches to the server, logs the result, and
  returns the data payload so the agent can act on it.
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from .mcp_server import MCPServer

# ---------------------------------------------------------------------------
# ANSI colour codes for prettier console output
# (gracefully ignored on terminals that don't support them)
# ---------------------------------------------------------------------------
_CYAN = "\033[96m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_RED = "\033[91m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


def _fmt_mcp(text: str) -> str:
    """Wrap text in the [MCP] tag with cyan colouring."""
    return f"{_CYAN}{_BOLD}[MCP]{_RESET} {text}"


def _fmt_args(kwargs: Dict[str, Any]) -> str:
    """
    Render keyword arguments as a compact, readable string for log output.
    Truncates long string values so logs stay on one line.
    """
    parts = []
    for key, value in kwargs.items():
        if isinstance(value, str) and len(value) > 60:
            value = value[:57] + "..."
        parts.append(f"{key}={value!r}")
    return ", ".join(parts)


def _fmt_result(result: Dict[str, Any]) -> str:
    """
    Render the result envelope compactly for log output.
    Shows status and a brief preview of the data field.
    """
    status = result.get("status", "unknown")
    message = result.get("message", "")
    data = result.get("data")

    # Summarise data so the log line doesn't get too long
    if data is None:
        data_preview = "null"
    elif isinstance(data, list):
        data_preview = f"[{len(data)} item(s)]"
    elif isinstance(data, dict):
        keys = list(data.keys())
        data_preview = "{" + ", ".join(keys) + "}"
    else:
        data_str = str(data)
        data_preview = data_str[:80] + "..." if len(data_str) > 80 else data_str

    colour = _GREEN if status == "ok" else _RED
    return (
        f"{colour}status={status!r}{_RESET}  message={message!r}  data={data_preview}"
    )


# ---------------------------------------------------------------------------
# MCPClient
# ---------------------------------------------------------------------------


class MCPClient:
    """
    MCP (Model Context Protocol) Client.

    PROTOCOL ROLE: Client
    ----------------------
    The client is the agent-facing interface to an MCP Server. Every agent
    that needs to access external data or perform actions holds an instance
    of MCPClient and uses it exclusively – the agent never touches the
    server or the data files directly.

    This enforces the MCP separation of concerns:
      Agent logic   -->  MCPClient  -->  MCPServer  -->  Data / APIs

    Attributes:
        server      : The MCPServer this client is connected to.
        client_id   : A label identifying which agent owns this client
                      (used in log output for traceability).
        call_history: A list of all tool calls made in this session.
                      Useful for debugging and audit trails.
    """

    def __init__(self, server: MCPServer, client_id: str = "MCPClient") -> None:
        """
        Initialise the client and "connect" to the given server.

        In a real MCP implementation, __init__ would:
          - Open a transport connection (HTTP, stdio, WebSocket).
          - Perform a capabilities handshake.
          - Cache the server's tool list for the agent's context window.

        Here we simply store the server reference and announce the
        connection in the log.

        Args:
            server   : The MCPServer instance to connect to.
            client_id: A human-readable label for this client (e.g. the
                       name of the agent that owns it). Appears in logs.
        """
        self.server: MCPServer = server
        self.client_id: str = client_id
        self.call_history: List[Dict[str, Any]] = []

        # Announce connection (mirrors the MCP "initialize" handshake)
        print(
            _fmt_mcp(
                f"Client {_BOLD}{self.client_id!r}{_RESET}{_CYAN} connected "
                f"to server {_BOLD}{self.server.name!r}{_RESET}{_CYAN}. "
                f"Available tools: {[t['name'] for t in self.server.list_tools()]}"
            )
        )

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def call_tool(self, tool_name: str, **kwargs: Any) -> Dict[str, Any]:
        """
        Invoke a named tool on the connected MCP Server.

        PROTOCOL FLOW (per call):
          1. Client validates the tool name is known (optional pre-check).
          2. Client sends a 'tools/call' request: { name, arguments }.
          3. Server executes the tool and returns a result envelope.
          4. Client logs and returns the result to the calling agent.

        In the real MCP spec the wire message looks like:
          {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": { "name": "<tool_name>", "arguments": { ... } }
          }

        Args:
            tool_name : The registered name of the tool to invoke.
            **kwargs  : Named arguments forwarded to the tool function.

        Returns:
            The result envelope dict from the server:
              {
                "status" : "ok" | "error",
                "message": str,
                "data"   : Any   # None on error
              }

        Note:
            This method never raises; errors are surfaced inside the
            returned envelope (status == "error") so agents can handle
            them gracefully without try/except boilerplate.
        """
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        args_str = _fmt_args(kwargs)

        # ── Log the outgoing request ──────────────────────────────────────
        print(
            _fmt_mcp(
                f"{_YELLOW}→ CALL{_RESET}   "
                f"[{timestamp}] "
                f"{_BOLD}{tool_name}{_RESET}({args_str})  "
                f"| caller={self.client_id!r}"
            )
        )

        # ── Dispatch to the server ────────────────────────────────────────
        result = self.server.call_tool(tool_name, **kwargs)

        # ── Log the incoming response ─────────────────────────────────────
        print(
            _fmt_mcp(
                f"{_GREEN}← RESULT{_RESET}  "
                f"[{timestamp}] "
                f"{_BOLD}{tool_name}{_RESET}  "
                f"| {_fmt_result(result)}"
            )
        )

        # ── Record in session history ─────────────────────────────────────
        self.call_history.append(
            {
                "timestamp": timestamp,
                "tool": tool_name,
                "arguments": kwargs,
                "status": result.get("status"),
                "message": result.get("message"),
            }
        )

        return result

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def list_available_tools(self) -> List[Dict[str, str]]:
        """
        Ask the server for its full tool catalogue.

        Corresponds to the MCP 'tools/list' request. An LLM-powered agent
        would call this once at startup and include the tool descriptions
        in its system prompt so it knows what capabilities are available.

        Returns:
            List of dicts, each with 'name' and 'description' keys.
        """
        tools = self.server.list_tools()
        print(
            _fmt_mcp(
                f"LIST_TOOLS request from {self.client_id!r} → "
                f"{len(tools)} tool(s) returned by {self.server.name!r}"
            )
        )
        return tools

    def get_session_summary(self) -> Dict[str, Any]:
        """
        Return a summary of all tool calls made in this session.

        Useful for audit logging, debugging, or feeding back to a
        supervising agent to understand what actions were taken.

        Returns:
            Dict containing total calls, breakdown by tool name, and
            counts of successful vs failed calls.
        """
        total = len(self.call_history)
        success = sum(1 for c in self.call_history if c["status"] == "ok")
        failed = total - success

        by_tool: Dict[str, int] = {}
        for call in self.call_history:
            by_tool[call["tool"]] = by_tool.get(call["tool"], 0) + 1

        return {
            "client_id": self.client_id,
            "server": self.server.name,
            "total_calls": total,
            "successful": success,
            "failed": failed,
            "calls_by_tool": by_tool,
        }

    def __repr__(self) -> str:
        return (
            f"MCPClient("
            f"client_id={self.client_id!r}, "
            f"server={self.server.name!r}, "
            f"calls_made={len(self.call_history)})"
        )
