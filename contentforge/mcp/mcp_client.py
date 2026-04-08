# =============================================================================
# mcp/mcp_client.py  —  MCP Client (Model Context Protocol)
# =============================================================================
#
# WHY THIS FILE EXISTS
# --------------------
# The MCPServer hosts all the tools, but agents should NOT call the server
# directly (e.g. server._tool_search_topic(...)).  Calling private methods
# bypasses logging, error handling, and the standard response envelope.
#
# The MCPClient is the thin, well-defined interface between agents and the
# server.  It provides:
#
#   1. UNIFORM INVOCATION  — agents always call mcp.call_tool(name, **kwargs)
#                            regardless of which tool they need.  One method
#                            to learn, one method to test.
#
#   2. OBSERVABILITY       — every tool call is logged with [MCP] tags so you
#                            can see exactly when each agent used the tool layer.
#                            In a real system, this is where you would emit
#                            metrics to Prometheus or traces to OpenTelemetry.
#
#   3. DECOUPLING          — agents import MCPClient, not MCPServer.  The server
#                            implementation can change completely (swap files for
#                            a database, a REST API, or a remote MCP server) and
#                            no agent code needs updating — only the client changes.
#
#   4. TOOL DISCOVERY      — agents can ask "what tools are available?" before
#                            deciding what to do, enabling dynamic, self-adapting
#                            agent behaviour.
#
#   5. RESULT UNWRAPPING   — the client provides a convenience method that
#                            extracts just the result payload from the standard
#                            MCP response envelope, reducing boilerplate in agents.
#
# MCP in the REAL WORLD
# ----------------------
# Anthropic's Model Context Protocol (announced 2024) defines a JSON-RPC 2.0
# based protocol where:
#   - Servers expose "tools", "resources", and "prompts" over stdio or HTTP/SSE
#   - Clients connect to one or more servers and aggregate their capabilities
#   - LLM hosts (Claude Desktop, Cursor, etc.) auto-discover and invoke tools
#
# ContentForge uses the SAME conceptual model but implemented in pure Python
# without the network layer, so we can focus on the protocol semantics
# rather than the transport mechanics.
#
# The key concepts map directly:
#   Real MCP: client.initialize() → tools/list → tools/call
#   ContentForge: MCPClient(server) → list_tools() → call_tool(name, **kwargs)
#
# USAGE PATTERN (in any agent)
# ------------------------------
#   # At startup: the agent receives a pre-configured MCPClient instance
#   result = self.mcp.call_tool("search_topic", topic="artificial intelligence")
#   if result["success"]:
#       facts = result["result"]["facts"]
#
#   # Or using the convenience wrapper that raises on failure:
#   facts_data = self.mcp.call_tool_unwrap("search_topic", topic="AI")
# =============================================================================

from datetime import datetime
from typing import Any, Dict, List, Optional

from mcp.mcp_server import MCPServer


class MCPClient:
    """
    MCP Client — the agent's interface to all MCP tools.

    Every agent in ContentForge receives one MCPClient instance at
    construction time.  Agents invoke tools exclusively through this client;
    they never interact with the MCPServer directly.

    This "client/server" separation mirrors real distributed systems:
      - The server is a service that COULD run on a different machine
      - The client is a local proxy that handles the transport details
      - Agents only ever see the client's clean interface

    MCP CONCEPT: Client-Server Tool Access
    ----------------------------------------
    In Anthropic's MCP spec:
      - A "host" (e.g. Claude Desktop) contains the LLM and manages clients
      - Clients maintain 1:1 sessions with specific MCP servers
      - Servers expose tools, resources, and prompts over the wire
      - The LLM never directly accesses resources — always through the client

    ContentForge mirrors this exactly:
      - Agents = the "host" (they contain the reasoning logic)
      - MCPClient = the "client" (manages server access)
      - MCPServer = the "server" (hosts tool implementations + data)

    Parameters
    ----------
    server : MCPServer
        The MCP server instance to connect to.  In a networked system this
        would be replaced with an HTTP session, a stdio subprocess handle,
        or a gRPC stub.

    agent_id : str, optional
        The ID of the agent using this client.  Used in log output to make
        it clear WHICH agent is calling WHICH tool.  Defaults to "unknown".

    verbose : bool, optional
        If True (default), print detailed [MCP] log lines for every call.
        Set to False in tests or when you want less console noise.
    """

    def __init__(
        self,
        server: MCPServer,
        agent_id: str = "unknown",
        verbose: bool = True,
    ) -> None:
        self._server = server
        self._agent_id = agent_id
        self._verbose = verbose

        # ----------------------------------------------------------------
        # Call ledger: records every tool invocation this client has made.
        # Useful for per-agent tool usage auditing and debugging.
        # ----------------------------------------------------------------
        self._call_ledger: List[Dict[str, Any]] = []

        # ----------------------------------------------------------------
        # Cache of the tool catalogue, populated lazily on first call to
        # list_tools().  Mirrors how a real MCP client caches the
        # server's capability list after the initial handshake.
        # ----------------------------------------------------------------
        self._tool_catalogue: Optional[List[Dict[str, Any]]] = None

        if self._verbose:
            print(
                f"  [MCP CLIENT] ✓  MCPClient initialized for agent "
                f"'{self._agent_id}'. Connected to {repr(server)}"
            )

    # ------------------------------------------------------------------
    # Core invocation method
    # ------------------------------------------------------------------

    def call_tool(self, tool_name: str, **kwargs) -> Dict[str, Any]:
        """
        Invoke a named tool on the MCP server and return the result envelope.

        MCP CONCEPT: Unified Tool Invocation
        --------------------------------------
        This is the ONE method agents use to access ANY external resource.
        Whether an agent needs to search facts, check readability, or publish
        an article — it always calls call_tool() with just the tool name and
        parameters.

        This uniformity is powerful because:
          - Agents don't need to know WHERE data comes from (file? API? DB?)
          - Error handling is consistent across all tool types
          - The client can add cross-cutting concerns (retries, caching,
            rate limiting, auth headers) in ONE place without touching agents
          - Tool calls are logged and auditable automatically

        Log format
        ----------
        Each call emits a log line in the format:
            [MCP] AgentID → tool_name(param=value, ...) → success/failure

        This makes the MCP "layer" clearly visible in the console output,
        distinct from [ACP] messages (inter-agent) and agent logic output.

        Parameters
        ----------
        tool_name : str
            Name of the tool to invoke (must be registered on the server).
        **kwargs
            Tool-specific keyword arguments.  See MCPServer._register_all_tools()
            for each tool's parameter contract.

        Returns
        -------
        Dict[str, Any]
            Standard MCP response envelope:
            {
                "success"   : bool,     # True if tool executed without error
                "tool"      : str,      # which tool was called
                "result"    : Any,      # the actual return value (None on error)
                "error"     : str|None, # error description (None on success)
                "timestamp" : str       # ISO-8601 time of execution
            }

        Notes
        -----
        This method NEVER raises an exception.  If the server errors, the
        response envelope carries success=False and an error description.
        This design keeps agent code clean — agents check result["success"]
        rather than wrapping every call in try/except.
        """
        call_start = datetime.now()

        # ---- Build a readable parameter summary for logging ----------
        param_summary = self._format_params(kwargs)

        # ---- Log the outgoing call (MCP layer visibility) -----------
        if self._verbose:
            print(f"\n  [MCP] {self._agent_id} → {tool_name}({param_summary})")

        # ---- Dispatch to the server ---------------------------------
        response = self._server.call_tool(tool_name, **kwargs)

        # ---- Calculate call duration --------------------------------
        duration_ms = round((datetime.now() - call_start).total_seconds() * 1000, 1)

        # ---- Log the result -----------------------------------------
        if self._verbose:
            if response["success"]:
                # Show a brief summary of what came back
                result_summary = self._summarise_result(response["result"])
                print(f"  [MCP] ✓  {tool_name} → {result_summary} ({duration_ms}ms)")
            else:
                print(
                    f"  [MCP] ✗  {tool_name} FAILED: {response['error']} "
                    f"({duration_ms}ms)"
                )

        # ---- Record in the ledger -----------------------------------
        ledger_entry = {
            "tool": tool_name,
            "params": kwargs,
            "success": response["success"],
            "duration_ms": duration_ms,
            "timestamp": call_start.isoformat(),
            "agent_id": self._agent_id,
            "error": response.get("error"),
        }
        self._call_ledger.append(ledger_entry)

        return response

    # ------------------------------------------------------------------
    # Convenience wrappers
    # ------------------------------------------------------------------

    def call_tool_unwrap(self, tool_name: str, **kwargs) -> Any:
        """
        Invoke a tool and return ONLY the result payload (unwrapped).

        This is a convenience method for agents that have already validated
        the tool exists and just want the data without dealing with the
        response envelope.

        Unlike call_tool(), this method WILL raise ValueError if the tool
        call fails.  Use it when a tool failure should stop processing
        (e.g. "I cannot write an article without research data").

        Parameters
        ----------
        tool_name : str
            Name of the tool to invoke.
        **kwargs
            Tool-specific keyword arguments.

        Returns
        -------
        Any
            The value stored in response["result"].

        Raises
        ------
        ValueError
            If the tool call fails (success=False).
        """
        response = self.call_tool(tool_name, **kwargs)
        if not response["success"]:
            raise ValueError(
                f"MCP tool '{tool_name}' failed for agent '{self._agent_id}': "
                f"{response['error']}"
            )
        return response["result"]

    def call_tool_safe(self, tool_name: str, default: Any = None, **kwargs) -> Any:
        """
        Invoke a tool and return the result payload, or a default on failure.

        This is a convenience method for agents where a tool failure is
        non-fatal — the agent can continue with degraded data.
        Example: "If SEO keywords are unavailable, continue without them."

        Parameters
        ----------
        tool_name : str
            Name of the tool to invoke.
        default : Any, optional
            Value to return if the tool call fails. Defaults to None.
        **kwargs
            Tool-specific keyword arguments.

        Returns
        -------
        Any
            result["result"] on success, or `default` on failure.
        """
        response = self.call_tool(tool_name, **kwargs)
        if response["success"]:
            return response["result"]
        if self._verbose:
            print(
                f"  [MCP] ⚠  '{tool_name}' failed — using default value. "
                f"Error: {response['error']}"
            )
        return default

    # ------------------------------------------------------------------
    # Tool discovery
    # ------------------------------------------------------------------

    def list_tools(self, refresh: bool = False) -> List[Dict[str, Any]]:
        """
        Return the catalogue of all tools available on the connected server.

        MCP CONCEPT: Tool Discovery / Capability Introspection
        --------------------------------------------------------
        Before an agent decides what to do, it CAN query the server to find
        out what tools are available.  This enables:

          - DYNAMIC AGENTS: "I'll use whatever search tool is available,
            whether it's called 'search_topic' or 'vector_search'."

          - VALIDATION: "Does this server have the 'publish_article' tool?
            If not, I should raise an error rather than silently doing nothing."

          - SELF-DOCUMENTATION: The tool list (with descriptions + params)
            is sufficient for an LLM agent to decide which tool to call —
            this is exactly how Claude's tool use works in production.

        Results are cached after the first call (mirrors the MCP handshake
        where the client requests tools/list once and caches the response).
        Pass refresh=True to force a re-fetch (useful if the server changes
        its tool set dynamically).

        Parameters
        ----------
        refresh : bool
            If True, bypass the cache and re-fetch from the server.

        Returns
        -------
        List[Dict[str, Any]]
            List of tool descriptors, each containing:
            {name, description, parameters, call_count}
        """
        if self._tool_catalogue is None or refresh:
            self._tool_catalogue = self._server.list_tools()
            if self._verbose:
                tool_names = [t["name"] for t in self._tool_catalogue]
                print(
                    f"  [MCP] {self._agent_id} discovered "
                    f"{len(self._tool_catalogue)} tools: {tool_names}"
                )
        return self._tool_catalogue

    def has_tool(self, tool_name: str) -> bool:
        """
        Check whether a specific tool is available on the server.

        Agents can use this for graceful degradation:
            if self.mcp.has_tool("advanced_seo"):
                keywords = self.mcp.call_tool("advanced_seo", ...)
            else:
                keywords = self.mcp.call_tool("get_seo_keywords", ...)

        Parameters
        ----------
        tool_name : str
            The tool name to check for.

        Returns
        -------
        bool
        """
        tools = self.list_tools()
        return any(t["name"] == tool_name for t in tools)

    def get_tool_info(self, tool_name: str) -> Optional[Dict[str, Any]]:
        """
        Return the full descriptor for a specific tool, or None if not found.

        Useful for agents that want to validate their parameters against the
        tool's declared parameter schema before making the call.

        Parameters
        ----------
        tool_name : str
            The tool name to look up.

        Returns
        -------
        Dict[str, Any] or None
        """
        tools = self.list_tools()
        for tool in tools:
            if tool["name"] == tool_name:
                return tool
        return None

    def print_tool_catalogue(self) -> None:
        """
        Print a human-readable list of all available tools.

        Useful at startup or for debugging — lets the operator (or developer)
        quickly see what capabilities the MCP server exposes.
        """
        tools = self.list_tools()
        print(f"\n  ── MCP TOOL CATALOGUE (via {self._agent_id}) ──────────────")
        for i, tool in enumerate(tools, 1):
            print(f"  [{i:02d}] {tool['name']}")
            # Truncate long descriptions for readability
            desc = tool["description"]
            if len(desc) > 90:
                desc = desc[:87] + "..."
            print(f"       {desc}")
            params = list(tool["parameters"].keys())
            print(f"       Params: {params}")
        print(f"\n  Total: {len(tools)} tool(s) available")
        print("  ──────────────────────────────────────────────────────────────")

    # ------------------------------------------------------------------
    # Client identity and configuration
    # ------------------------------------------------------------------

    def set_agent_id(self, agent_id: str) -> None:
        """
        Update the agent ID used in log output.

        Allows one MCPClient instance to be re-used by different agents
        (useful in tests or when an agent hands off to a sub-agent).

        Parameters
        ----------
        agent_id : str
            The new agent identifier to use in log lines.
        """
        old_id = self._agent_id
        self._agent_id = agent_id
        if self._verbose:
            print(f"  [MCP CLIENT] Agent ID updated: '{old_id}' → '{agent_id}'")

    def set_verbose(self, verbose: bool) -> None:
        """
        Enable or disable verbose [MCP] logging.

        Parameters
        ----------
        verbose : bool
            True to enable logging, False to silence it.
        """
        self._verbose = verbose

    # ------------------------------------------------------------------
    # Audit & observability
    # ------------------------------------------------------------------

    def get_call_ledger(self) -> List[Dict[str, Any]]:
        """
        Return the full ordered list of all tool calls this client has made.

        MCP CONCEPT: Client-Side Audit Trail
        --------------------------------------
        The call ledger is the MCP equivalent of the ACP message history —
        a complete, ordered record of every tool invocation.  Together they
        give you two complementary views of a pipeline run:

          ACP history  → "How did agents communicate with each other?"
          MCP ledger   → "What external resources did agents access?"

        The combination of both gives you complete pipeline observability:
        you can trace any piece of data in the final article back to its
        source (which tool call retrieved it, which agent requested it,
        at what timestamp).

        Returns a copy of the ledger so callers cannot mutate the history.

        Returns
        -------
        List[Dict[str, Any]]
            Each entry: {tool, params, success, duration_ms, timestamp,
                         agent_id, error}
        """
        return list(self._call_ledger)

    def get_call_stats(self) -> Dict[str, Any]:
        """
        Return summary statistics about this client's tool usage.

        Returns
        -------
        Dict[str, Any]
            {
                total_calls      : int,
                successful_calls : int,
                failed_calls     : int,
                total_duration_ms: float,
                calls_by_tool    : {tool_name: count},
                avg_duration_ms  : float
            }
        """
        total = len(self._call_ledger)
        successful = sum(1 for e in self._call_ledger if e["success"])
        total_duration = sum(e["duration_ms"] for e in self._call_ledger)

        calls_by_tool: Dict[str, int] = {}
        for entry in self._call_ledger:
            tool = entry["tool"]
            calls_by_tool[tool] = calls_by_tool.get(tool, 0) + 1

        return {
            "total_calls": total,
            "successful_calls": successful,
            "failed_calls": total - successful,
            "total_duration_ms": round(total_duration, 1),
            "avg_duration_ms": round(total_duration / max(total, 1), 1),
            "calls_by_tool": calls_by_tool,
        }

    def print_call_summary(self) -> None:
        """
        Print a formatted summary of this client's tool usage.

        Intended to be called after a pipeline run completes to give a
        per-agent breakdown of MCP tool usage.  Complements the MCPServer's
        print_usage_stats() which shows aggregate totals across all clients.
        """
        stats = self.get_call_stats()

        print(f"\n  ── MCP CLIENT SUMMARY ({self._agent_id}) ──────────────────")
        print(f"  Total tool calls  : {stats['total_calls']}")
        print(f"  Successful        : {stats['successful_calls']}")
        print(f"  Failed            : {stats['failed_calls']}")
        print(f"  Total time        : {stats['total_duration_ms']}ms")
        print(f"  Avg per call      : {stats['avg_duration_ms']}ms")

        if stats["calls_by_tool"]:
            print(f"  Calls by tool:")
            for tool_name, count in sorted(
                stats["calls_by_tool"].items(), key=lambda x: x[1], reverse=True
            ):
                bar = "▪" * count
                print(f"    {tool_name:<28}: {bar} ({count})")
        print("  ──────────────────────────────────────────────────────────────")

    def print_call_ledger(self) -> None:
        """
        Print the full ordered call ledger for this client.

        Shows every MCP tool invocation in chronological order with status
        and duration.  Useful for debugging unexpected agent behaviour
        ("why did the EditorAgent call search_topic twice?").
        """
        print(f"\n  ── MCP CALL LEDGER ({self._agent_id}) ────────────────────────")

        if not self._call_ledger:
            print("  (no tool calls recorded)")
            print("  ──────────────────────────────────────────────────────────")
            return

        for i, entry in enumerate(self._call_ledger, 1):
            status_icon = "✓" if entry["success"] else "✗"
            param_keys = list(entry["params"].keys()) if entry["params"] else []
            error_str = f" | ERROR: {entry['error']}" if entry["error"] else ""
            print(
                f"  [{i:02d}] {status_icon} {entry['tool']:<28} "
                f"params={param_keys} "
                f"({entry['duration_ms']}ms)"
                f"{error_str}"
            )

        print("  ──────────────────────────────────────────────────────────────")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_params(kwargs: Dict[str, Any]) -> str:
        """
        Format keyword arguments into a compact, readable string for log output.

        Truncates long string values so log lines stay on one line.
        Handles nested dicts and lists gracefully.

        Parameters
        ----------
        kwargs : Dict[str, Any]
            The tool call parameters.

        Returns
        -------
        str
            e.g. 'topic="artificial intelligence", content_type="blog"'
        """
        if not kwargs:
            return ""

        parts: List[str] = []
        for key, value in kwargs.items():
            if isinstance(value, str):
                # Truncate long strings (e.g. full article content)
                display = value if len(value) <= 40 else value[:37] + "..."
                parts.append(f'{key}="{display}"')
            elif isinstance(value, dict):
                parts.append(f"{key}={{...{len(value)} keys}}")
            elif isinstance(value, list):
                parts.append(f"{key}=[...{len(value)} items]")
            else:
                parts.append(f"{key}={value!r}")

        return ", ".join(parts)

    @staticmethod
    def _summarise_result(result: Any) -> str:
        """
        Produce a compact one-line summary of a tool result for log output.

        Different result types get different summaries:
          - dict  → list the top-level keys
          - list  → show item count
          - str   → show first 50 chars
          - None  → "(None)"
          - other → repr truncated to 60 chars

        Parameters
        ----------
        result : Any
            The tool result to summarise.

        Returns
        -------
        str
            A short, human-readable description of the result.
        """
        if result is None:
            return "(None)"

        if isinstance(result, dict):
            keys = list(result.keys())
            found_status = ""
            # Surface the 'found' flag if present — most useful at a glance
            if "found" in result:
                found_status = f" found={result['found']}"
            if "success" in result:
                found_status = f" success={result['success']}"
            if len(keys) <= 5:
                return f"dict{{{', '.join(keys)}}}{found_status}"
            else:
                return (
                    f"dict{{{', '.join(keys[:4])}, ...+{len(keys) - 4}}}{found_status}"
                )

        if isinstance(result, list):
            return f"list[{len(result)} items]"

        if isinstance(result, str):
            display = result.strip()
            if len(display) <= 60:
                return f'"{display}"'
            return f'"{display[:57]}..."'

        if isinstance(result, bool):
            return str(result)

        if isinstance(result, (int, float)):
            return str(result)

        # Fallback for any other type
        raw = repr(result)
        return raw if len(raw) <= 60 else raw[:57] + "..."

    def __repr__(self) -> str:  # pragma: no cover
        stats = self.get_call_stats()
        return (
            f"MCPClient("
            f"agent={self._agent_id!r}, "
            f"calls={stats['total_calls']}, "
            f"server={repr(self._server)}"
            f")"
        )
