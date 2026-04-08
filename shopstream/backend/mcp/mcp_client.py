# backend/mcp/mcp_client.py
#
# =============================================================================
# MCP CLIENT — Model Context Protocol Client
# =============================================================================
#
# In the MCP architecture, the CLIENT sits between the agent and the server.
# Its responsibilities are:
#
#   1. SESSION MANAGEMENT
#      Every conversation gets a unique session_id (UUID).  All log lines are
#      tagged with this ID so you can correlate tool calls across a single
#      shopping session even when multiple users are active concurrently.
#
#   2. TOOL INVOCATION FACADE
#      call_tool(name, **kwargs) is the single method the agent needs.  It
#      delegates to the MCPServer, captures timing, and logs the outcome.
#      The agent never talks to the server directly — it always goes through
#      this client, which mirrors the transport boundary in a real MCP setup
#      (where the client would serialise the call over stdio or HTTP).
#
#   3. CALL HISTORY
#      The client keeps an in-memory log of every tool call made during the
#      session.  This is useful for debugging, auditing, and for the agent to
#      reflect on what it has already fetched.
#
#   4. OBSERVABILITY
#      Every call is printed to stdout with a structured prefix so it is easy
#      to trace the agent's reasoning in the terminal while the SSE stream
#      is flowing to the browser.
#
# =============================================================================

import time
import uuid
from typing import Any

from .mcp_server import MCPServer


class MCPClient:
    """
    MCP Client — the agent's gateway to all shopping tools.

    Each MCPClient instance represents one logical MCP session.  In a
    production system this would hold a network connection to the MCP server
    process; here it holds a direct reference to the in-process MCPServer.

    Usage:
        client = MCPClient()
        result = client.call_tool("search_products", query="wireless headphones")
        print(result)   # {"tool": "search_products", "success": True, "result": {...}}
    """

    def __init__(self, server: MCPServer | None = None):
        # MCP CONCEPT: Session Identifier
        # A UUID that uniquely identifies this client↔server session.
        # Real MCP implementations exchange this during the "initialize"
        # handshake.  We generate it here to keep things self-contained.
        self.session_id: str = str(uuid.uuid4())

        # Attach to the provided server (or create a fresh one).
        # In production MCP, the client would hold a transport handle
        # (e.g. a subprocess pipe or an HTTP session) rather than a direct
        # object reference.
        self._server: MCPServer = server or MCPServer()

        # MCP CONCEPT: Call History
        # Ordered list of every tool call made in this session.
        # Each entry is a dict: { name, kwargs, result, duration_ms, timestamp }
        self.call_history: list[dict] = []

        self._log(
            f"Session opened. Server has {len(self._server.list_tools())} registered tools."
        )

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def call_tool(self, tool_name: str, **kwargs: Any) -> dict:
        """
        MCP CONCEPT: Tool Call
        Invoke a named tool on the connected MCP server.

        Parameters
        ----------
        tool_name : str
            The registered tool name (e.g. "search_products").
        **kwargs
            Arguments forwarded verbatim to the tool handler.

        Returns
        -------
        dict
            Always returns a result envelope:
                {
                    "tool"    : str,   # echo of the tool name
                    "success" : bool,  # True if the handler ran without error
                    "result"  : any,   # the tool's return value (or error msg)
                }
        """
        self._log(f">> CALL  [{tool_name}]  args={self._fmt_args(kwargs)}")

        start_time = time.perf_counter()
        result = self._server.call_tool(tool_name, **kwargs)
        elapsed_ms = round((time.perf_counter() - start_time) * 1000, 1)

        status = "OK " if result["success"] else "ERR"
        self._log(f"<< {status}   [{tool_name}]  {elapsed_ms} ms")

        # Record in call history
        entry = {
            "tool": tool_name,
            "kwargs": kwargs,
            "result": result,
            "duration_ms": elapsed_ms,
            "timestamp": time.time(),
        }
        self.call_history.append(entry)

        return result

    # ------------------------------------------------------------------
    # Discovery helpers
    # ------------------------------------------------------------------

    def list_tools(self) -> list[dict]:
        """
        MCP CONCEPT: Tool Discovery
        Ask the server which tools are available.  In the MCP spec this maps
        to the tools/list request sent during or after the initialize handshake.
        """
        tools = self._server.list_tools()
        self._log(f"tools/list → {[t['name'] for t in tools]}")
        return tools

    def get_tool_schema(self, tool_name: str) -> dict | None:
        """
        Return the parameter schema for a single tool, or None if not found.
        Useful for agents that want to validate arguments before calling.
        """
        for tool in self._server.list_tools():
            if tool["name"] == tool_name:
                return tool
        return None

    # ------------------------------------------------------------------
    # Session introspection
    # ------------------------------------------------------------------

    def get_call_summary(self) -> dict:
        """
        Return a summary of all tool calls made in this session.
        Handy for diagnostics and for composing the final agent state snapshot.
        """
        return {
            "session_id": self.session_id,
            "total_calls": len(self.call_history),
            "calls": [
                {
                    "tool": entry["tool"],
                    "duration_ms": entry["duration_ms"],
                    "success": entry["result"]["success"],
                }
                for entry in self.call_history
            ],
        }

    def close(self) -> None:
        """
        MCP CONCEPT: Session Teardown
        Signal that this session is complete.  In a real MCP client this would
        close the transport connection and free server-side resources.
        """
        self._log(
            f"Session closed after {len(self.call_history)} tool call(s). "
            f"Total tools available: {len(self._server.list_tools())}"
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _log(self, message: str) -> None:
        """
        Emit a structured log line tagged with the session ID.

        Format:  [MCP Session:<short-uuid>] <message>

        Using a short (8-char) prefix of the UUID keeps logs readable while
        still being unique enough to distinguish concurrent sessions.
        """
        short_id = self.session_id[:8]
        print(f"[MCP Session:{short_id}] {message}", flush=True)

    @staticmethod
    def _fmt_args(kwargs: dict) -> str:
        """Format kwargs for compact log output, truncating long values."""
        parts = []
        for k, v in kwargs.items():
            if isinstance(v, str) and len(v) > 50:
                v_repr = f'"{v[:47]}..."'
            elif isinstance(v, list) and len(v) > 4:
                v_repr = f"[{v[:4]}... +{len(v) - 4}]"
            else:
                v_repr = repr(v)
            parts.append(f"{k}={v_repr}")
        return "{" + ", ".join(parts) + "}"
