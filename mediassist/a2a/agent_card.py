"""
Agent Card - A2A Protocol Discovery Metadata
=============================================
PROTOCOL CONCEPT: Agent Card
------------------------------
In the Agent-to-Agent (A2A) Protocol, an "Agent Card" is the foundational
discovery primitive. It is a structured, machine-readable document that an
agent publishes so that other agents (and human operators) can understand:

  - WHO   the agent is        (agent_id, name)
  - WHAT  it can do           (capabilities list)
  - WHERE to reach it         (endpoint URL)
  - HOW   to authenticate     (auth_scheme)
  - WHY   it exists           (description)

Think of it as an agent's "business card" or OpenAPI spec — it answers all
the questions a caller needs before initiating a conversation.

REAL-WORLD A2A AGENT CARD FORMAT:
----------------------------------
In the official A2A specification, an Agent Card is a JSON document served
at a well-known URL (typically  /.well-known/agent.json  on the agent's host).
A minimal real Agent Card looks like:

    {
        "name": "SchedulingAgent",
        "description": "Books and manages patient appointments.",
        "url": "https://scheduling.mediassist.internal/a2a",
        "version": "1.0.0",
        "capabilities": {
            "streaming": false,
            "pushNotifications": false
        },
        "skills": [
            { "id": "schedule_appointment", "name": "Schedule Appointment" },
            { "id": "check_availability",   "name": "Check Availability"   }
        ],
        "authentication": { "schemes": ["Bearer"] }
    }

OUR SIMPLIFIED VERSION:
-----------------------
We capture the same essential ideas in a Python dataclass:
  - capabilities  ≈  skills[].id  (what the agent can do)
  - endpoint      ≈  url          (where to send tasks)
  - auth_scheme   ≈  authentication.schemes[0]

The A2ARegistry uses AgentCards to answer discovery queries like:
  "Which agent can handle the 'check_prescriptions' capability?"

HOW IT FLOWS:
-------------
  1. Each sub-agent (SchedulingAgent, RecordsAgent, PharmacyAgent) creates
     its own AgentCard at startup and registers it with the A2ARegistry.

  2. When the OrchestratorAgent needs a capability, it calls:
         registry.discover("schedule_appointment")
     and receives the matching AgentCard.

  3. The Orchestrator passes that AgentCard to A2AClient.send_task(), which
     uses the endpoint and auth_scheme to route and authenticate the request.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class AgentCard:
    """
    A2A Agent Card — machine-readable identity and capability declaration.

    PROTOCOL ROLE:
    --------------
    This is the primary discovery artifact in A2A. Every agent that wants
    to be *reachable* by other agents must publish an AgentCard. Every agent
    that wants to *call* another agent uses AgentCards to decide who to call
    and how to reach them.

    The registry indexes AgentCards by their capabilities, enabling
    intent-based discovery:  "I need X done" → "AgentY can do X".

    Attributes:
        agent_id     : Globally unique identifier for this agent instance.
                       Used to route responses and detect duplicate registrations.
                       Example: "scheduling-agent-001"

        name         : Human-readable display name of the agent.
                       Example: "SchedulingAgent"

        description  : Natural-language explanation of the agent's purpose.
                       In a real LLM-powered orchestrator, this text would be
                       included in the prompt so the LLM can decide which agent
                       to delegate to. Example: "Handles appointment booking."

        capabilities : List of skill identifiers this agent supports.
                       The A2ARegistry uses these strings for discovery queries.
                       Example: ["schedule_appointment", "check_availability"]

        endpoint     : The simulated URL where this agent accepts A2A tasks.
                       In production this would be a real HTTPS endpoint.
                       Example: "http://localhost:8001/a2a"

        version      : Semantic version string for the agent's API contract.
                       Allows orchestrators to pin to compatible versions.
                       Example: "1.0.0"

        auth_scheme  : Authentication method required to call this agent.
                       Common real-world values: "Bearer", "ApiKey", "None".
                       Here we default to "Bearer" to show the concept without
                       implementing actual token verification.

        metadata     : Arbitrary key-value pairs for additional context
                       (e.g., rate limits, SLA guarantees, owner team).
                       Kept as a plain dict for extensibility.
    """

    agent_id: str
    name: str
    description: str
    capabilities: List[str]
    endpoint: str
    version: str = "1.0.0"
    auth_scheme: str = "Bearer"
    metadata: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Capability helpers
    # ------------------------------------------------------------------

    def supports(self, capability: str) -> bool:
        """
        Return True if this agent declares support for the given capability.

        The A2ARegistry calls this method when answering a discovery query.
        Matching is case-insensitive so "Schedule_Appointment" and
        "schedule_appointment" are treated as the same capability.

        Args:
            capability: The capability identifier to check.

        Returns:
            True if the capability is in this agent's declared list.

        Example:
            >>> card = AgentCard(agent_id="s1", name="Sched", ...)
            >>> card.supports("schedule_appointment")
            True
            >>> card.supports("get_prescriptions")
            False
        """
        return capability.lower() in [c.lower() for c in self.capabilities]

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """
        Serialise this AgentCard to a plain dictionary.

        In the real A2A spec, this is what gets served at the
        /.well-known/agent.json endpoint and exchanged over the wire.
        Agents parse this JSON to populate their local AgentCard objects.

        Returns:
            A JSON-serialisable dict representing this AgentCard.
        """
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "description": self.description,
            "capabilities": self.capabilities,
            "endpoint": self.endpoint,
            "version": self.version,
            "auth_scheme": self.auth_scheme,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentCard":
        """
        Deserialise an AgentCard from a plain dictionary.

        This mirrors what a real A2A client would do after fetching the
        /.well-known/agent.json document from a remote agent host:
        parse the JSON and reconstruct a local AgentCard object.

        Args:
            data: A dict as returned by to_dict() or parsed from JSON.

        Returns:
            A new AgentCard instance populated from the dict.
        """
        return cls(
            agent_id=data["agent_id"],
            name=data["name"],
            description=data["description"],
            capabilities=data["capabilities"],
            endpoint=data["endpoint"],
            version=data.get("version", "1.0.0"),
            auth_scheme=data.get("auth_scheme", "Bearer"),
            metadata=data.get("metadata", {}),
        )

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    def pretty(self) -> str:
        """
        Return a multi-line human-readable summary of this AgentCard.

        Useful for logging, debugging, and demo output — shows everything
        an orchestrator agent would "see" when it discovers this agent.

        Returns:
            A formatted string block describing this AgentCard.
        """
        caps = ", ".join(self.capabilities) if self.capabilities else "(none)"
        meta_str = ""
        if self.metadata:
            meta_items = ", ".join(f"{k}={v!r}" for k, v in self.metadata.items())
            meta_str = f"\n    metadata     : {meta_items}"

        return (
            f"┌─ AgentCard ─────────────────────────────────────────\n"
            f"│  agent_id    : {self.agent_id}\n"
            f"│  name        : {self.name}\n"
            f"│  description : {self.description}\n"
            f"│  capabilities: [{caps}]\n"
            f"│  endpoint    : {self.endpoint}\n"
            f"│  version     : {self.version}\n"
            f"│  auth_scheme : {self.auth_scheme}"
            f"{meta_str}\n"
            f"└─────────────────────────────────────────────────────"
        )

    def __str__(self) -> str:
        return (
            f"AgentCard(id={self.agent_id!r}, name={self.name!r}, "
            f"caps={self.capabilities})"
        )

    def __repr__(self) -> str:
        return self.__str__()
