"""
a2a/agent_card.py
=================
AgentCard - the identity and capability descriptor for every A2A agent.

Each agent publishes an AgentCard to the A2ARegistry so that other agents
can discover it by capability and know how to authenticate and communicate
with it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class AgentCard:
    """
    Describes an agent's identity, location, and what it can do.

    Fields
    ------
    agent_id        : Unique identifier for the agent (e.g. "flight-agent-01").
    name            : Human-readable display name.
    description     : Short prose description of what the agent does.
    capabilities    : List of capability strings the agent supports.
                      Other agents use these strings to discover this agent
                      via the A2ARegistry  (e.g. ["find_flights", "book_flight"]).
    endpoint        : Simulated HTTPS endpoint URL where the agent "listens".
                      In a real deployment this would be an actual URL.
    supported_tasks : Explicit list of task intent strings the agent handles
                      (mirrors capabilities but expressed as task verbs,
                       e.g. ["find_flights", "book_flight"]).
    auth_required   : Whether callers must supply a valid auth token before
                      the agent will process tasks.  Defaults to True.
    version         : Optional semantic version string for the agent.
    metadata        : Arbitrary extra key/value pairs (e.g. SLA tier, region).
    """

    agent_id: str
    name: str
    description: str
    capabilities: List[str]
    endpoint: str
    supported_tasks: List[str]
    auth_required: bool = True
    version: str = "1.0.0"
    metadata: dict = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def supports_capability(self, capability: str) -> bool:
        """Return True if this agent advertises the requested capability."""
        return capability in self.capabilities

    def supports_task(self, task_intent: str) -> bool:
        """Return True if this agent can handle the given task intent."""
        return task_intent in self.supported_tasks

    def to_dict(self) -> dict:
        """Serialise the AgentCard to a plain dictionary (for logging / wire format)."""
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "description": self.description,
            "capabilities": self.capabilities,
            "endpoint": self.endpoint,
            "supported_tasks": self.supported_tasks,
            "auth_required": self.auth_required,
            "version": self.version,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AgentCard":
        """Deserialise an AgentCard from a plain dictionary."""
        return cls(
            agent_id=data["agent_id"],
            name=data["name"],
            description=data["description"],
            capabilities=data["capabilities"],
            endpoint=data["endpoint"],
            supported_tasks=data["supported_tasks"],
            auth_required=data.get("auth_required", True),
            version=data.get("version", "1.0.0"),
            metadata=data.get("metadata", {}),
        )

    def __str__(self) -> str:
        caps = ", ".join(self.capabilities)
        return (
            f"AgentCard(id={self.agent_id!r}, name={self.name!r}, "
            f"endpoint={self.endpoint!r}, capabilities=[{caps}])"
        )

    def __repr__(self) -> str:
        return self.__str__()
