"""
a2a/a2a_protocol.py
====================
Core A2A (Agent-to-Agent) Protocol Implementation
---------------------------------------------------
This module provides the fundamental building blocks for agent-to-agent
communication in TravelMind:

  A2ATask     - Structured task message sent from one agent to another.
  A2AResponse - Structured response returned by the receiving agent.
  A2ARegistry - In-memory discovery registry; agents register their AgentCards
                here and callers can look agents up by capability.
  A2AClient   - High-level client that orchestrates the full
                  discovery → authentication → task dispatch → response cycle
                and emits detailed [A2A] log lines at every step.

Design notes
------------
* All communication is simulated in-process (no real HTTP).  The "endpoint"
  field on AgentCard is a logical URL for documentation purposes.
* Authentication is handled by the a2a.auth module (fake JWT tokens).
* Agents must expose a `process_task(task: A2ATask) -> A2AResponse` method.
* The Registry is a module-level singleton so every component shares one view
  of the agent landscape – just like a real service-discovery system would.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .auth import generate_token, verify_token

# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------


class TaskStatus:
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class ResponseStatus:
    SUCCESS = "success"
    ERROR = "error"
    STREAMING = "streaming"


# ---------------------------------------------------------------------------
# A2ATask
# ---------------------------------------------------------------------------


@dataclass
class A2ATask:
    """
    Represents a unit of work dispatched from one agent to another.

    Fields
    ------
    task_id     : Globally unique identifier for this task (auto-generated
                  if not supplied).
    sender_id   : agent_id of the agent sending the task.
    receiver_id : agent_id of the agent that should handle the task.
    intent      : Short verb-phrase describing what the task is asking for,
                  e.g. "find_flights", "book_hotel", "process_payment".
                  Must match one of the receiver's supported_tasks.
    payload     : Arbitrary dict containing the task parameters.
    auth_token  : Bearer token the sender supplies to prove identity.
    status      : Lifecycle status of the task (see TaskStatus constants).
    created_at  : Unix timestamp when this task was created.
    metadata    : Optional extra key/value pairs (trace IDs, priority, etc.).
    """

    sender_id: str
    receiver_id: str
    intent: str
    payload: Dict[str, Any]
    auth_token: str
    task_id: str = field(
        default_factory=lambda: f"TASK-{uuid.uuid4().hex[:10].upper()}"
    )
    status: str = TaskStatus.PENDING
    created_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "sender_id": self.sender_id,
            "receiver_id": self.receiver_id,
            "intent": self.intent,
            "payload": self.payload,
            "status": self.status,
            "created_at": self.created_at,
            "metadata": self.metadata,
            # auth_token is intentionally omitted from serialisation for safety
        }

    def __str__(self) -> str:
        return (
            f"A2ATask(id={self.task_id}, sender={self.sender_id!r}, "
            f"receiver={self.receiver_id!r}, intent={self.intent!r}, "
            f"status={self.status})"
        )


# ---------------------------------------------------------------------------
# A2AResponse
# ---------------------------------------------------------------------------


@dataclass
class A2AResponse:
    """
    Structured response returned by an agent after processing an A2ATask.

    Fields
    ------
    task_id   : Echoes the task_id of the originating A2ATask.
    agent_id  : agent_id of the agent that produced this response.
    status    : "success", "error", or "streaming" (see ResponseStatus).
    result    : The actual payload returned by the agent.  On success this
                is typically a dict with domain-specific data (flight options,
                hotel list, payment receipt, etc.).  On error it may be None
                or contain an error detail dict.
    message   : Human-readable summary message accompanying the response.
    duration_ms : How long (in milliseconds) the agent spent processing.
    responded_at : Unix timestamp when this response was created.
    """

    task_id: str
    agent_id: str
    status: str
    result: Optional[Any] = None
    message: str = ""
    duration_ms: float = 0.0
    responded_at: float = field(default_factory=time.time)

    # ------------------------------------------------------------------
    # Convenience constructors
    # ------------------------------------------------------------------

    @classmethod
    def success(
        cls,
        task_id: str,
        agent_id: str,
        result: Any,
        message: str = "Task completed successfully.",
        duration_ms: float = 0.0,
    ) -> "A2AResponse":
        return cls(
            task_id=task_id,
            agent_id=agent_id,
            status=ResponseStatus.SUCCESS,
            result=result,
            message=message,
            duration_ms=duration_ms,
        )

    @classmethod
    def error(
        cls,
        task_id: str,
        agent_id: str,
        message: str,
        result: Any = None,
    ) -> "A2AResponse":
        return cls(
            task_id=task_id,
            agent_id=agent_id,
            status=ResponseStatus.ERROR,
            result=result,
            message=message,
        )

    @classmethod
    def streaming(
        cls,
        task_id: str,
        agent_id: str,
        result: Any,
        message: str = "Partial result (streaming).",
    ) -> "A2AResponse":
        return cls(
            task_id=task_id,
            agent_id=agent_id,
            status=ResponseStatus.STREAMING,
            result=result,
            message=message,
        )

    def is_success(self) -> bool:
        return self.status == ResponseStatus.SUCCESS

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "status": self.status,
            "result": self.result,
            "message": self.message,
            "duration_ms": round(self.duration_ms, 2),
            "responded_at": self.responded_at,
        }

    def __str__(self) -> str:
        return (
            f"A2AResponse(task_id={self.task_id}, agent={self.agent_id!r}, "
            f"status={self.status!r}, message={self.message!r})"
        )


# ---------------------------------------------------------------------------
# A2ARegistry  (module-level singleton)
# ---------------------------------------------------------------------------


class A2ARegistry:
    """
    In-memory agent discovery registry.

    Agents call  register(agent_card)  during startup so their capabilities
    are visible to the rest of the system.  The orchestrator calls
    discover(capability)  to find candidate agents at runtime.

    The registry is intentionally a plain Python object that is shared as a
    module-level singleton (see _GLOBAL_REGISTRY below).  In production this
    would be backed by a distributed key-value store or a dedicated discovery
    service.
    """

    def __init__(self) -> None:
        # { agent_id -> AgentCard }
        self._cards: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, agent_card: Any) -> None:
        """
        Register an agent by storing its AgentCard.

        If an agent with the same agent_id is already registered it will be
        overwritten (idempotent re-registration is fine).
        """
        self._cards[agent_card.agent_id] = agent_card
        print(
            f"[A2A] REGISTRY: Registered agent {agent_card.name!r} "
            f"(id={agent_card.agent_id!r}) with capabilities: "
            f"{agent_card.capabilities}"
        )

    def unregister(self, agent_id: str) -> bool:
        """Remove an agent from the registry.  Returns True if it was found."""
        if agent_id in self._cards:
            del self._cards[agent_id]
            return True
        return False

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover(self, capability: str) -> List[Any]:
        """
        Return all AgentCards whose capabilities list contains *capability*.

        Parameters
        ----------
        capability : str
            The capability string to search for, e.g. "find_flights".

        Returns
        -------
        list[AgentCard]
            Possibly empty list of matching cards, ordered by registration time
            (dict insertion order in Python 3.7+).
        """
        return [
            card for card in self._cards.values() if capability in card.capabilities
        ]

    def discover_one(self, capability: str) -> Optional[Any]:
        """
        Return the first AgentCard that advertises *capability*, or None.

        Convenience wrapper around discover() for when exactly one agent
        is expected to handle a capability.
        """
        results = self.discover(capability)
        return results[0] if results else None

    def get_by_id(self, agent_id: str) -> Optional[Any]:
        """Look up an AgentCard directly by its agent_id."""
        return self._cards.get(agent_id)

    def list_all(self) -> List[Any]:
        """Return all registered AgentCards."""
        return list(self._cards.values())

    def summary(self) -> str:
        """Human-readable summary of registered agents."""
        if not self._cards:
            return "A2ARegistry: (empty)"
        lines = ["A2ARegistry contents:"]
        for card in self._cards.values():
            lines.append(
                f"  • {card.name} ({card.agent_id}) — capabilities: {card.capabilities}"
            )
        return "\n".join(lines)

    def __len__(self) -> int:
        return len(self._cards)

    def __repr__(self) -> str:
        return f"A2ARegistry(registered_agents={len(self._cards)})"


# Module-level shared registry instance – import and use this everywhere.
_GLOBAL_REGISTRY = A2ARegistry()


def get_registry() -> A2ARegistry:
    """Return the shared module-level A2ARegistry singleton."""
    return _GLOBAL_REGISTRY


# ---------------------------------------------------------------------------
# A2AClient
# ---------------------------------------------------------------------------


class A2AClient:
    """
    High-level A2A client that orchestrates the full agent communication
    lifecycle on behalf of a caller (usually the OrchestratorAgent).

    Lifecycle for each send_task() call
    -------------------------------------
    1. DISCOVERY  – find the right agent in the registry for the capability.
    2. AUTH       – generate (or reuse) a bearer token; verify it is accepted.
    3. TASK       – package the request as an A2ATask and dispatch it.
    4. RESPONSE   – capture and return the A2AResponse, logging the outcome.

    The client keeps a small per-agent token cache so it does not issue a
    new token on every single task call (mirrors real OAuth2 token caching).

    Parameters
    ----------
    caller_id : str
        The agent_id of the agent that owns this client instance.
    registry  : A2ARegistry, optional
        The registry to use for discovery.  Defaults to the shared global
        registry returned by get_registry().
    verbose   : bool
        When True (default) all [A2A] lifecycle log lines are printed.
    """

    def __init__(
        self,
        caller_id: str,
        registry: Optional[A2ARegistry] = None,
        verbose: bool = True,
    ) -> None:
        self.caller_id = caller_id
        self.registry = registry or get_registry()
        self.verbose = verbose
        # { agent_id -> token_string }
        self._token_cache: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _log(self, message: str) -> None:
        if self.verbose:
            print(message)

    def _get_or_create_token(self, agent_id: str) -> str:
        """
        Return a cached token for agent_id, or generate a fresh one.
        Tokens are re-used until verify_token() reports them as invalid
        (i.e. they have expired or been revoked).
        """
        cached = self._token_cache.get(agent_id)
        if cached and verify_token(cached, self.caller_id):
            return cached
        # Generate a new token scoped to the caller
        new_token = generate_token(self.caller_id)
        self._token_cache[agent_id] = new_token
        return new_token

    # ------------------------------------------------------------------
    # Core send_task method
    # ------------------------------------------------------------------

    def send_task(
        self,
        capability: str,
        intent: str,
        payload: Dict[str, Any],
        agent_card: Optional[Any] = None,
        task_id: Optional[str] = None,
        extra_meta: Optional[Dict[str, Any]] = None,
    ) -> A2AResponse:
        """
        Execute the full A2A communication lifecycle for a single task.

        Parameters
        ----------
        capability  : str
            The capability string used for discovery when agent_card is not
            provided (e.g. "find_flights").
        intent      : str
            The task intent verb; must match the target agent's supported_tasks
            (e.g. "find_flights", "book_hotel").
        payload     : dict
            Task-specific parameters forwarded to the agent.
        agent_card  : AgentCard, optional
            If already known, skip discovery and use this card directly.
        task_id     : str, optional
            Override the auto-generated task ID.
        extra_meta  : dict, optional
            Extra metadata to attach to the A2ATask.

        Returns
        -------
        A2AResponse
            The response from the target agent.  On any error (discovery
            failure, auth failure, unsupported intent, agent exception) an
            A2AResponse with status="error" is returned rather than raising.
        """
        start_time = time.time()

        # ----------------------------------------------------------------
        # Step 1 – DISCOVERY
        # ----------------------------------------------------------------
        if agent_card is None:
            self._log(f"[A2A] DISCOVERY: Finding agent for capability: {capability!r}")
            agent_card = self.registry.discover_one(capability)
            if agent_card is None:
                err_msg = f"No agent found in registry for capability: {capability!r}"
                self._log(f"[A2A] DISCOVERY ERROR: {err_msg}")
                return A2AResponse.error(
                    task_id=task_id or f"TASK-{uuid.uuid4().hex[:10].upper()}",
                    agent_id=self.caller_id,
                    message=err_msg,
                )
            self._log(
                f"[A2A] DISCOVERY: Found agent {agent_card.name!r} "
                f"(id={agent_card.agent_id!r}) at {agent_card.endpoint!r}"
            )
        else:
            self._log(
                f"[A2A] DISCOVERY: Using pre-supplied agent card for "
                f"{agent_card.name!r} (capability={capability!r})"
            )

        # ----------------------------------------------------------------
        # Step 2 – AUTHENTICATION
        # ----------------------------------------------------------------
        self._log(
            f"[A2A] AUTH: Authenticating with {agent_card.name!r} "
            f"(auth_required={agent_card.auth_required})..."
        )

        if agent_card.auth_required:
            auth_token = self._get_or_create_token(agent_card.agent_id)
            # Verify the token we are about to send is still good
            token_ok = verify_token(auth_token, self.caller_id)
            if not token_ok:
                err_msg = (
                    f"Authentication failed: token invalid for caller "
                    f"{self.caller_id!r} with agent {agent_card.name!r}"
                )
                self._log(f"[A2A] AUTH ERROR: {err_msg}")
                return A2AResponse.error(
                    task_id=task_id or f"TASK-{uuid.uuid4().hex[:10].upper()}",
                    agent_id=agent_card.agent_id,
                    message=err_msg,
                )
            self._log(
                f"[A2A] AUTH: Token issued for {self.caller_id!r} — "
                f"preview: {auth_token[:24]}..."
            )
        else:
            auth_token = ""
            self._log(
                f"[A2A] AUTH: No authentication required for {agent_card.name!r}."
            )

        # ----------------------------------------------------------------
        # Step 3 – TASK DISPATCH
        # ----------------------------------------------------------------
        # Check that the target agent supports this intent
        if intent not in agent_card.supported_tasks:
            err_msg = (
                f"Agent {agent_card.name!r} does not support intent {intent!r}. "
                f"Supported: {agent_card.supported_tasks}"
            )
            self._log(f"[A2A] TASK ERROR: {err_msg}")
            return A2AResponse.error(
                task_id=task_id or f"TASK-{uuid.uuid4().hex[:10].upper()}",
                agent_id=agent_card.agent_id,
                message=err_msg,
            )

        task = A2ATask(
            task_id=task_id or f"TASK-{uuid.uuid4().hex[:10].upper()}",
            sender_id=self.caller_id,
            receiver_id=agent_card.agent_id,
            intent=intent,
            payload=payload,
            auth_token=auth_token,
            status=TaskStatus.PENDING,
            metadata=extra_meta or {},
        )

        self._log(
            f"[A2A] TASK: Sending task {task.task_id!r} to {agent_card.name!r} "
            f"| intent={intent!r} | payload_keys={list(payload.keys())}"
        )

        # ----------------------------------------------------------------
        # Step 4 – AGENT INVOCATION  (simulated in-process call)
        # ----------------------------------------------------------------
        try:
            task.status = TaskStatus.PROCESSING
            # The agent object must be resolvable.  The registry card stores a
            # reference to the live agent instance via its 'metadata' dict.
            agent_instance = agent_card.metadata.get("instance")
            if agent_instance is None:
                raise RuntimeError(
                    f"Agent {agent_card.agent_id!r} has no 'instance' bound in "
                    f"its AgentCard.metadata.  Cannot dispatch task."
                )
            response: A2AResponse = agent_instance.process_task(task)
            task.status = TaskStatus.COMPLETED

        except Exception as exc:  # noqa: BLE001
            task.status = TaskStatus.FAILED
            err_msg = f"Agent {agent_card.name!r} raised an exception: {exc}"
            self._log(f"[A2A] TASK EXCEPTION: {err_msg}")
            elapsed = (time.time() - start_time) * 1000
            return A2AResponse.error(
                task_id=task.task_id,
                agent_id=agent_card.agent_id,
                message=err_msg,
            )

        # ----------------------------------------------------------------
        # Step 5 – RESPONSE LOGGING
        # ----------------------------------------------------------------
        elapsed_ms = (time.time() - start_time) * 1000
        response.duration_ms = elapsed_ms

        status_icon = "✓" if response.is_success() else "✗"
        self._log(
            f"[A2A] RESPONSE: Received from {agent_card.name!r} "
            f"[task={task.task_id}] "
            f"status={response.status!r} {status_icon} "
            f"({elapsed_ms:.1f} ms) — {response.message}"
        )

        return response

    # ------------------------------------------------------------------
    # Convenience wrappers
    # ------------------------------------------------------------------

    def send_task_to_agent(
        self,
        agent_card: Any,
        intent: str,
        payload: Dict[str, Any],
    ) -> A2AResponse:
        """
        Shorthand: send a task directly to a known agent card, skipping
        the registry discovery step.
        """
        return self.send_task(
            capability=intent,
            intent=intent,
            payload=payload,
            agent_card=agent_card,
        )

    def broadcast(
        self,
        capability: str,
        intent: str,
        payload: Dict[str, Any],
    ) -> List[A2AResponse]:
        """
        Send the same task to ALL agents that advertise *capability* and
        return a list of responses (one per agent).

        Useful for fan-out patterns where you want to gather results from
        multiple competing providers (e.g. query all hotel agents).
        """
        cards = self.registry.discover(capability)
        if not cards:
            self._log(f"[A2A] BROADCAST: No agents found for capability {capability!r}")
            return []

        self._log(
            f"[A2A] BROADCAST: Sending intent={intent!r} to "
            f"{len(cards)} agent(s) with capability {capability!r}"
        )
        responses = []
        for card in cards:
            resp = self.send_task(
                capability=capability,
                intent=intent,
                payload=payload,
                agent_card=card,
            )
            responses.append(resp)
        return responses
