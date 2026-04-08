"""
A2A Protocol - Agent-to-Agent Protocol Core Implementation
===========================================================
PROTOCOL CONCEPT: Agent-to-Agent (A2A)
----------------------------------------
A2A is an open interoperability protocol that defines how autonomous AI agents
communicate, discover each other, delegate tasks, and exchange results — all
without requiring bespoke integration code between every pair of agents.

Originally proposed by Google (2025), A2A is designed to complement MCP:
  - MCP  connects agents to *tools and data* (external resources)
  - A2A  connects agents to *other agents*   (peer collaboration)

Together they form the two key communication layers of a modern multi-agent
system:

    ┌──────────────────────────────────────────────────────────┐
    │               Multi-Agent Communication Stack            │
    ├──────────────────────────────────────────────────────────┤
    │  Layer 2 — Agent Collaboration   [ A2A Protocol ]        │
    │  OrchestratorAgent ←──A2ATask/A2AResponse──→ SubAgents   │
    ├──────────────────────────────────────────────────────────┤
    │  Layer 1 — Tool / Data Access    [ MCP Protocol ]        │
    │  SubAgents ←──call_tool/result──→ MCPServer (databases)  │
    └──────────────────────────────────────────────────────────┘

COMPONENTS IN THIS FILE:
-------------------------
  A2ATask      - The work-request object sent from one agent to another.
                 Carries an *intent* (what to do) and a *payload* (the data
                 needed to do it). Analogous to an HTTP request body.

  A2AResponse  - The reply from the receiving agent.
                 Carries a *status*, a *result* dict, and a *message*.
                 Analogous to an HTTP response body.

  A2AClient    - The sender-side component. Serialises a Task, locates the
                 target agent via its AgentCard, simulates authentication,
                 dispatches the call, and deserialises the Response.
                 All steps are logged with [A2A] prefixes.

  A2ARegistry  - The discovery service. Agents register their AgentCards here.
                 An orchestrator calls discover(capability) to find which agent
                 can handle a given type of task — without hard-coding agent
                 addresses into the orchestrator's logic.

REAL A2A WIRE FORMAT (for reference):
--------------------------------------
In the official spec, a Task sent over HTTP/SSE looks like:

    POST /a2a HTTP/1.1
    Content-Type: application/json
    Authorization: Bearer <token>

    {
        "jsonrpc": "2.0",
        "id": "task-uuid",
        "method": "tasks/send",
        "params": {
            "id": "task-uuid",
            "message": {
                "role": "user",
                "parts": [{ "text": "Book an appointment for patient P001" }]
            }
        }
    }

We preserve all the conceptual steps (auth header, task ID, message routing,
structured response) while simulating the network transport in-process.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from .agent_card import AgentCard

# ---------------------------------------------------------------------------
# ANSI colour codes for prettier, scannable console output
# ---------------------------------------------------------------------------
_MAGENTA = "\033[95m"
_BLUE = "\033[94m"
_CYAN = "\033[96m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_RED = "\033[91m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RESET = "\033[0m"


def _fmt_a2a(text: str) -> str:
    """Wrap text in the [A2A] tag with magenta colouring."""
    return f"{_MAGENTA}{_BOLD}[A2A]{_RESET} {text}"


def _now() -> str:
    """Return a compact timestamp string for log lines."""
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


# ---------------------------------------------------------------------------
# A2ATask
# ---------------------------------------------------------------------------


@dataclass
class A2ATask:
    """
    A2A Task — a structured work-request from one agent to another.

    PROTOCOL ROLE:
    --------------
    The Task is the fundamental unit of work in A2A. When the Orchestrator
    decides to delegate to a sub-agent it constructs an A2ATask and hands it
    to the A2AClient for delivery.

    In the real A2A spec a Task is called a "message" sent to the target
    agent's /a2a endpoint. It has a unique ID (for correlation), a role
    ("user" or "agent"), and a list of content "parts" (text, files, etc.).

    Our simplified model captures the same semantics:
      - task_id    → the correlation ID (auto-generated UUID if not supplied)
      - sender_id  → which agent is making the request
      - receiver_id→ which agent should handle it (may be empty before discovery)
      - intent     → the high-level action being requested (our "method")
      - payload    → the structured arguments (our "params")
      - status     → lifecycle state of this task

    Task Lifecycle:
    ---------------
        pending  →  in_progress  →  completed
                                 →  failed

    Attributes:
        task_id     : Unique identifier for this task (UUID string).
                      Used by the sender to match responses to requests.
        sender_id   : agent_id of the agent that created this task.
        receiver_id : agent_id of the agent that should process this task.
                      May be an empty string if the orchestrator relies on
                      discovery to determine the target at send time.
        intent      : A short string naming the action requested.
                      Examples: "book_appointment", "get_medical_history",
                      "check_prescriptions", "full_checkup".
        payload     : A dict of named parameters for the intent.
                      Example: {"patient_id": "P001", "date": "2025-08-15"}
        status      : Current lifecycle state of the task.
                      One of: "pending", "in_progress", "completed", "failed".
    """

    sender_id: str
    receiver_id: str
    intent: str
    payload: Dict[str, Any]
    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: str = "pending"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """
        Serialise this task to a plain dict (mirrors the A2A wire format).

        In a real A2A implementation this dict would be JSON-serialised and
        sent in the HTTP POST body to the receiver's /a2a endpoint.
        """
        return {
            "task_id": self.task_id,
            "sender_id": self.sender_id,
            "receiver_id": self.receiver_id,
            "intent": self.intent,
            "payload": self.payload,
            "status": self.status,
        }

    def mark_in_progress(self) -> None:
        """Transition the task to the 'in_progress' state."""
        self.status = "in_progress"

    def mark_completed(self) -> None:
        """Transition the task to the 'completed' state."""
        self.status = "completed"

    def mark_failed(self) -> None:
        """Transition the task to the 'failed' state."""
        self.status = "failed"

    def __str__(self) -> str:
        short_id = self.task_id[:8]
        return (
            f"A2ATask(id={short_id}..., intent={self.intent!r}, "
            f"sender={self.sender_id!r}, receiver={self.receiver_id!r}, "
            f"status={self.status!r})"
        )

    def __repr__(self) -> str:
        return self.__str__()


# ---------------------------------------------------------------------------
# A2AResponse
# ---------------------------------------------------------------------------


@dataclass
class A2AResponse:
    """
    A2A Response — the structured reply from a receiving agent.

    PROTOCOL ROLE:
    --------------
    After an agent finishes processing an A2ATask, it wraps its output in an
    A2AResponse and returns it to the caller (via the A2AClient).

    In the real A2A spec this corresponds to the HTTP response body (or the
    final SSE "task.completed" event for streaming agents). It carries:
      - The original task_id (for correlation — matching request to response)
      - The responding agent's ID
      - A status ("completed" or "failed")
      - A structured result dict (the actual output data)
      - A human-readable message (for logging, display, or LLM consumption)

    Attributes:
        task_id  : The ID of the task this response answers.
                   Allows the sender to match async responses to requests.
        agent_id : The agent_id of the agent that produced this response.
        status   : "completed" if the task succeeded, "failed" otherwise.
        result   : A dict containing the output data of the task.
                   The schema is intent-specific (each agent defines its own).
        message  : A plain-English summary of the outcome.
                   Suitable for display to users or inclusion in LLM prompts.
    """

    task_id: str
    agent_id: str
    status: str
    result: Dict[str, Any]
    message: str

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @property
    def succeeded(self) -> bool:
        """Return True if the task completed successfully."""
        return self.status == "completed"

    @property
    def failed(self) -> bool:
        """Return True if the task failed."""
        return self.status == "failed"

    def to_dict(self) -> Dict[str, Any]:
        """
        Serialise this response to a plain dict.

        Mirrors the JSON body the receiving agent would send back over HTTP
        (or the final artifact in the SSE stream) in real A2A deployments.
        """
        return {
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "status": self.status,
            "result": self.result,
            "message": self.message,
        }

    def summary(self) -> str:
        """Return a compact one-line summary for log display."""
        short_id = self.task_id[:8]
        status_colour = _GREEN if self.succeeded else _RED
        return (
            f"task_id={short_id}...  "
            f"agent={self.agent_id!r}  "
            f"{status_colour}status={self.status!r}{_RESET}  "
            f"message={self.message!r}"
        )

    def __str__(self) -> str:
        short_id = self.task_id[:8]
        return (
            f"A2AResponse(task_id={short_id}..., agent={self.agent_id!r}, "
            f"status={self.status!r}, message={self.message!r})"
        )

    def __repr__(self) -> str:
        return self.__str__()


# ---------------------------------------------------------------------------
# A2AClient
# ---------------------------------------------------------------------------


class A2AClient:
    """
    A2A Client — the sender-side component for agent-to-agent communication.

    PROTOCOL ROLE:
    --------------
    The A2AClient lives inside the OrchestratorAgent (or any agent that wants
    to delegate work). When the orchestrator decides to hand a task to a
    sub-agent it does so through this client.

    The client's responsibilities mirror what a real A2A client library does:
      1. VALIDATE   - Check that the target AgentCard declares the required
                      capability before attempting the call.
      2. AUTHENTICATE- Add auth credentials (simulated "Bearer token" here;
                      real implementations use OAuth2, mTLS, API keys, etc.).
      3. DISPATCH   - Route the task to the correct agent endpoint
                      (here: call the agent's process_task() directly;
                       in production: HTTP POST to agent_card.endpoint).
      4. LOG        - Every protocol step is logged with [A2A] prefix so the
                      full handoff chain is visible in the console output.
      5. RETURN     - Deliver the A2AResponse back to the calling agent.

    Attributes:
        sender_id : The agent_id of the agent that owns this client.
                    Stamped onto every outgoing A2ATask as the sender.
    """

    def __init__(self, sender_id: str) -> None:
        """
        Initialise the A2A Client.

        Args:
            sender_id: The agent_id of the orchestrating agent. This will
                       appear as 'sender_id' on every task this client sends,
                       letting receivers know who is calling them.
        """
        self.sender_id: str = sender_id

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def send_task(
        self,
        agent_card: AgentCard,
        task: A2ATask,
        agent_callable: Callable[[A2ATask], A2AResponse],
    ) -> A2AResponse:
        """
        Send an A2ATask to a target agent and receive an A2AResponse.

        PROTOCOL FLOW:
        --------------
        Step 1 — Capability validation
            Before sending, confirm the target agent's AgentCard lists the
            required capability (derived from the task's intent).
            → Prevents wasted calls to agents that cannot fulfil the request.

        Step 2 — Authentication header simulation
            In real A2A, the client includes an Authorization header with a
            signed Bearer token (JWT) so the receiving agent can verify the
            caller's identity and permissions.
            → Here we print a simulated auth line to show the concept.

        Step 3 — Task dispatch (the "HTTP POST" simulation)
            The task is serialised and sent to agent_card.endpoint.
            → Here we call agent_callable(task) directly; in production this
              would be: httpx.post(agent_card.endpoint, json=task.to_dict())

        Step 4 — Response handling
            The raw response is deserialised into an A2AResponse object.
            → In production: A2AResponse(**response.json())

        Step 5 — Logging
            Every step is logged with [A2A] so operators can trace the full
            multi-agent conversation without a separate tracing tool.

        Args:
            agent_card     : The AgentCard of the target agent (obtained from
                             the A2ARegistry via discover()).
            task           : The A2ATask to send. Its sender_id and receiver_id
                             will be set/overridden to match this client's
                             sender_id and the target agent's agent_id.
            agent_callable : The callable that represents the target agent's
                             request handler. Receives the A2ATask and returns
                             an A2AResponse. (Simulates the HTTP endpoint.)

        Returns:
            The A2AResponse produced by the receiving agent.

        Raises:
            RuntimeError: If the target agent's AgentCard does not declare
                          a capability matching the task's intent.
        """
        ts = _now()

        # ── Stamp sender / receiver onto the task ─────────────────────
        task.sender_id = self.sender_id
        task.receiver_id = agent_card.agent_id

        # ── Step 1: Capability check ──────────────────────────────────
        print(
            _fmt_a2a(
                f"{_YELLOW}● CAPABILITY CHECK{_RESET}  "
                f"[{ts}]  "
                f"Does {_BOLD}{agent_card.name}{_RESET} support intent "
                f"{_BOLD}{task.intent!r}{_RESET}?"
            )
        )

        # We treat the intent string as the capability identifier.
        # If the agent doesn't list it, we raise rather than sending a
        # doomed request (fail fast principle).
        if not agent_card.supports(task.intent):
            # Provide a helpful error showing what the agent *does* support
            print(
                _fmt_a2a(
                    f"{_RED}✗ CAPABILITY MISMATCH{_RESET}  "
                    f"{agent_card.name!r} does not support {task.intent!r}.  "
                    f"Supported: {agent_card.capabilities}"
                )
            )
            raise RuntimeError(
                f"[A2A] Agent '{agent_card.name}' (id={agent_card.agent_id}) "
                f"does not declare capability '{task.intent}'. "
                f"Declared capabilities: {agent_card.capabilities}"
            )

        print(
            _fmt_a2a(
                f"{_GREEN}✓ CAPABILITY OK{_RESET}  "
                f"{agent_card.name!r} declares {task.intent!r}"
            )
        )

        # ── Step 2: Authentication ─────────────────────────────────────
        # Simulate generating a Bearer token (in production: sign a JWT
        # with the orchestrator's private key and the agent's public key).
        simulated_token = (
            f"eyJhbGciOiJSUzI1NiJ9.{self.sender_id[:8]}...{agent_card.agent_id[:8]}"
        )
        print(
            _fmt_a2a(
                f"{_CYAN}● AUTH{_RESET}  [{ts}]  "
                f"Scheme={agent_card.auth_scheme!r}  "
                f"Token={_DIM}{simulated_token}{_RESET}"
            )
        )

        # ── Step 3: Task dispatch ──────────────────────────────────────
        task.mark_in_progress()
        print(
            _fmt_a2a(
                f"{_YELLOW}→ SEND TASK{_RESET}  [{ts}]  "
                f"POST {agent_card.endpoint}  "
                f"task_id={task.task_id[:8]}...  "
                f"intent={task.intent!r}  "
                f"payload_keys={list(task.payload.keys())}  "
                f"{_DIM}from={self.sender_id!r} → to={agent_card.agent_id!r}{_RESET}"
            )
        )

        # ── Execute the agent (simulates the network round-trip) ───────
        response: A2AResponse = agent_callable(task)

        # ── Step 4: Response handling ──────────────────────────────────
        if response.succeeded:
            task.mark_completed()
        else:
            task.mark_failed()

        ts_resp = _now()
        status_icon = (
            f"{_GREEN}✓ RESPONSE OK{_RESET}"
            if response.succeeded
            else f"{_RED}✗ RESPONSE FAILED{_RESET}"
        )
        print(_fmt_a2a(f"{status_icon}  [{ts_resp}]  {response.summary()}"))

        return response

    def __repr__(self) -> str:
        return f"A2AClient(sender_id={self.sender_id!r})"


# ---------------------------------------------------------------------------
# A2ARegistry
# ---------------------------------------------------------------------------


class A2ARegistry:
    """
    A2A Registry — the agent discovery and registration service.

    PROTOCOL ROLE:
    --------------
    The Registry is the Yellow Pages of the multi-agent system. Every agent
    that wants to be reachable registers its AgentCard here at startup. Any
    agent that needs help queries the Registry to find a capable peer.

    This decouples orchestrators from specific agent implementations:
      - WITHOUT registry: Orchestrator hard-codes "call SchedulingAgent for
        bookings". Adding a new agent requires changing the orchestrator.
      - WITH registry:    Orchestrator says "find me an agent that can do
        'schedule_appointment'". New agents join by registering — no
        orchestrator changes needed.

    In a production A2A deployment, the Registry would be:
      - A dedicated service (e.g. an agent directory API)
      - Or a distributed mechanism (DNS-SD, mDNS, service mesh)
      - Or a well-known URL scheme (each host serves /.well-known/agent.json)

    Here we implement it as an in-process dict, preserving all the same
    discovery semantics.

    Attributes:
        _agents : Internal dict mapping agent_id → AgentCard for all
                  registered agents.
    """

    def __init__(self) -> None:
        """Initialise an empty registry."""
        self._agents: Dict[str, AgentCard] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, agent_card: AgentCard) -> None:
        """
        Register an agent's AgentCard with the discovery service.

        PROTOCOL STEP: Agent Registration
        ----------------------------------
        In the real A2A spec, an agent registers by making its AgentCard
        available at /.well-known/agent.json on its host, and optionally by
        publishing to a central directory service.

        Here, sub-agents call this method at startup (orchestrated by the
        OrchestratorAgent) to make themselves discoverable.

        If an agent with the same agent_id is already registered, this call
        updates (overwrites) the existing entry — useful for rolling updates.

        Args:
            agent_card: The AgentCard to register. Its agent_id is used as
                        the unique key in the registry.
        """
        is_update = agent_card.agent_id in self._agents
        self._agents[agent_card.agent_id] = agent_card

        action = "UPDATED" if is_update else "REGISTERED"
        print(
            _fmt_a2a(
                f"{_BLUE}● {action}{_RESET}  "
                f"agent_id={agent_card.agent_id!r}  "
                f"name={agent_card.name!r}  "
                f"capabilities={agent_card.capabilities}  "
                f"endpoint={agent_card.endpoint!r}"
            )
        )

    def deregister(self, agent_id: str) -> bool:
        """
        Remove an agent from the registry.

        Called when an agent shuts down gracefully. Prevents the discovery
        service from routing tasks to an agent that is no longer running.

        Args:
            agent_id: The unique ID of the agent to remove.

        Returns:
            True if the agent was found and removed, False if not found.
        """
        if agent_id in self._agents:
            name = self._agents[agent_id].name
            del self._agents[agent_id]
            print(
                _fmt_a2a(
                    f"{_YELLOW}● DEREGISTERED{_RESET}  "
                    f"agent_id={agent_id!r}  name={name!r}"
                )
            )
            return True
        return False

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover(self, capability: str) -> Optional[AgentCard]:
        """
        Find the first registered agent that supports the given capability.

        PROTOCOL STEP: Agent Discovery
        --------------------------------
        This is the heart of A2A's decoupled architecture. The orchestrator
        does NOT hard-code "call SchedulingAgent". Instead it says:
            "Give me an agent that can do 'schedule_appointment'"

        The registry searches all registered AgentCards and returns the first
        one whose capabilities list includes the requested capability.

        In a production registry this might also consider:
          - Agent health / availability status
          - Load balancing (round-robin among capable agents)
          - SLA requirements (pick the fastest / most reliable agent)
          - Version constraints ("I need at least v2.0 of this capability")

        Args:
            capability: The capability string to search for.
                        Must match (case-insensitively) one of the strings in
                        an AgentCard's capabilities list.

        Returns:
            The first matching AgentCard, or None if no registered agent
            declares the requested capability.
        """
        ts = _now()
        print(
            _fmt_a2a(
                f"{_CYAN}● DISCOVER{_RESET}  [{ts}]  "
                f"Searching {len(self._agents)} registered agent(s) "
                f"for capability {_BOLD}{capability!r}{_RESET} ..."
            )
        )

        for agent_card in self._agents.values():
            if agent_card.supports(capability):
                print(
                    _fmt_a2a(
                        f"{_GREEN}✓ FOUND{_RESET}  [{ts}]  "
                        f"capability={capability!r}  "
                        f"→  agent={agent_card.name!r}  "
                        f"endpoint={agent_card.endpoint!r}"
                    )
                )
                return agent_card

        print(
            _fmt_a2a(
                f"{_RED}✗ NOT FOUND{_RESET}  [{ts}]  "
                f"No agent registered with capability {capability!r}.  "
                f"Registered capabilities: {self._all_capabilities()}"
            )
        )
        return None

    def discover_all(self, capability: str) -> List[AgentCard]:
        """
        Find ALL registered agents that support the given capability.

        Unlike discover() which returns only the first match, this returns
        every capable agent — useful for fan-out scenarios where the
        orchestrator wants to broadcast a task to multiple agents in parallel.

        Args:
            capability: The capability string to search for.

        Returns:
            A list of matching AgentCards (may be empty).
        """
        matches = [card for card in self._agents.values() if card.supports(capability)]
        print(
            _fmt_a2a(
                f"{_CYAN}● DISCOVER_ALL{_RESET}  "
                f"capability={capability!r}  "
                f"→  {len(matches)} match(es): "
                f"{[c.name for c in matches]}"
            )
        )
        return matches

    def get_agent(self, agent_id: str) -> Optional[AgentCard]:
        """
        Retrieve a specific agent's AgentCard by its exact agent_id.

        Used when the orchestrator already knows which agent it wants to call
        (e.g. for direct callbacks or long-running task status checks).

        Args:
            agent_id: The unique agent identifier to look up.

        Returns:
            The matching AgentCard, or None if not registered.
        """
        return self._agents.get(agent_id)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def list_agents(self) -> List[AgentCard]:
        """
        Return all registered AgentCards.

        Useful for health dashboards, admin tools, or an orchestrator that
        wants to build a complete picture of available agents.

        Returns:
            A list of all registered AgentCards (in registration order on
            Python 3.7+ due to dict insertion ordering).
        """
        return list(self._agents.values())

    def _all_capabilities(self) -> List[str]:
        """Return a deduplicated sorted list of every declared capability."""
        caps: List[str] = []
        for card in self._agents.values():
            for cap in card.capabilities:
                if cap not in caps:
                    caps.append(cap)
        return sorted(caps)

    def print_registry(self) -> None:
        """
        Print a formatted table of all registered agents to the console.

        Useful at startup to confirm all expected agents have registered,
        or in debugging sessions to understand the current system topology.
        """
        agents = self.list_agents()
        border = "─" * 60
        print(f"\n{_MAGENTA}{_BOLD}┌{border}┐")
        print(f"│{'  A2A REGISTRY — Registered Agents':^60}│")
        print(f"├{border}┤{_RESET}")
        if not agents:
            print(f"{_MAGENTA}│{'  (no agents registered)':^60}│")
        else:
            for card in agents:
                print(
                    f"{_MAGENTA}│{_RESET}  "
                    f"{_BOLD}{card.name:<22}{_RESET}"
                    f"  caps: {', '.join(card.capabilities)}"
                )
                print(
                    f"{_MAGENTA}│{_RESET}  "
                    f"{_DIM}id={card.agent_id}  "
                    f"endpoint={card.endpoint}{_RESET}"
                )
                print(f"{_MAGENTA}│{_RESET}")
        print(f"{_MAGENTA}{_BOLD}└{border}┘{_RESET}\n")

    def __len__(self) -> int:
        return len(self._agents)

    def __repr__(self) -> str:
        agent_names = [c.name for c in self._agents.values()]
        return f"A2ARegistry(agents={agent_names})"
