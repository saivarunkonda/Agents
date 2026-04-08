# =============================================================================
# acp/agent_registry.py  —  ACP Agent Registry
# =============================================================================
#
# WHY THIS FILE EXISTS
# --------------------
# In a distributed multi-agent system, agents come and go dynamically.
# Without a central directory, agents would need to be hard-coded with
# knowledge of every other agent — brittle, unscalable, and fragile.
#
# The ACP Agent Registry solves this with SERVICE DISCOVERY:
#   - Agents REGISTER themselves when they start up, advertising their
#     capabilities (what input they accept, what output they produce).
#   - Other agents (or orchestrators) can QUERY the registry to find out
#     who is available and what they can do — at runtime.
#   - The registry tracks agent STATUS so the orchestrator knows whether
#     an agent is idle, busy, done, or in an error state.
#
# ANALOGY
# -------
# Think of the registry as a professional directory (like LinkedIn for agents):
#   • Each agent has a "profile" (ACPAgentInfo) describing its skills.
#   • The pipeline orchestrator consults the directory before dispatching work.
#   • Status updates let everyone know who is available right now.
#
# ACP vs A2A (Agent-to-Agent Protocol)
# --------------------------------------
# In Google's A2A protocol, an "Agent Card" (hosted at /.well-known/agent.json)
# serves a similar purpose — it describes an agent's capabilities over HTTP.
# ACP's registry is the in-process equivalent: same concept, no network hop.
#
# HOW IT FITS INTO THE PIPELINE
# -------------------------------
#   1. main.py creates one shared ACPAgentRegistry instance.
#   2. Each agent registers an ACPAgentInfo describing itself.
#   3. main.py calls registry.print_registry() to show the full agent roster.
#   4. During processing, each agent calls registry.update_status() to
#      reflect its current state (idle → processing → done / error).
#   5. At any point, any component can call registry.get_agent(id) to
#      fetch an agent's current profile without importing that agent.
# =============================================================================

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# ACPAgentInfo — the agent's self-description ("agent card")
# ---------------------------------------------------------------------------


@dataclass
class ACPAgentInfo:
    """
    ACP Agent Info — a structured description of a single agent's identity,
    capabilities, and current operational status.

    This is what an agent "announces" to the world when it starts up.
    The registry stores these announcements and makes them queryable.

    Fields
    ------
    agent_id        Unique identifier for this agent instance.
                    Used as the routing address on the ACP Message Bus.
                    Convention: short, lowercase, hyphen-separated.
                    Examples: "researcher", "writer", "editor"

    name            Human-readable display name.
                    Used in logs and UI output.
                    Examples: "ResearcherAgent", "WriterAgent"

    description     Plain-English description of what this agent does.
                    Should answer: "Given X input, this agent produces Y output
                    by doing Z."  Written for human operators, not machines.

    input_schema    Describes the payload schema this agent expects to receive.
                    Format: a dict whose keys are field names and values are
                    type descriptions (plain strings for readability).
                    Example:
                        {
                            "intent": "str — must be 'research_topic'",
                            "topic":  "str — the subject to research",
                            "content_type": "str — 'blog' | 'technical'"
                        }
                    NOTE: In a production ACP system this would be a full
                    JSON Schema or Pydantic model, enabling automatic
                    validation.  We use plain dicts here for clarity.

    output_schema   Describes the payload schema this agent produces.
                    Same format as input_schema.
                    Consumers of this agent's output use this to understand
                    what fields to expect in the response payload.

    status          Current operational state of the agent.
                    One of: "idle", "processing", "done", "error"
                    Updated via registry.update_status() during pipeline runs.

    pipeline_stage  Numeric ordering hint for display and orchestration.
                    Stage 1 = first in the pipeline, higher = later.
                    Not enforced by ACP — purely informational.

    registered_at   ISO-8601 timestamp of when the agent registered.
                    Auto-populated; agents do not need to set this.

    last_updated    ISO-8601 timestamp of the most recent status change.
                    Auto-populated by update_status().

    tags            Optional list of capability tags for filtering.
                    Examples: ["nlp", "research"], ["seo", "metadata"]
                    Useful when building dynamic pipelines that select
                    agents by capability rather than by hard-coded ID.

    metadata        Free-form dict for any additional agent-specific info.
                    Examples: model version, max token limit, rate limits.
    """

    # ---- identity ----------------------------------------------------------
    agent_id: str = ""
    name: str = ""
    description: str = ""

    # ---- capability schema -------------------------------------------------
    input_schema: Dict[str, Any] = field(default_factory=dict)
    output_schema: Dict[str, Any] = field(default_factory=dict)

    # ---- operational state -------------------------------------------------
    status: str = "idle"  # "idle" | "processing" | "done" | "error"
    pipeline_stage: int = 0  # ordering hint (1 = first, 5 = last, etc.)

    # ---- timestamps --------------------------------------------------------
    registered_at: str = field(default_factory=lambda: datetime.now().isoformat())
    last_updated: str = field(default_factory=lambda: datetime.now().isoformat())

    # ---- discovery metadata ------------------------------------------------
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def status_icon(self) -> str:
        """Return a visual icon for the agent's current status."""
        icons = {
            "idle": "⏸ ",
            "processing": "⚙ ",
            "done": "✓ ",
            "error": "✗ ",
        }
        return icons.get(self.status, "? ")

    def summary_line(self) -> str:
        """
        Return a single formatted line suitable for registry table output.

        Format:
          [stage] icon  agent_id          name                  status
        """
        tags_str = f"  [{', '.join(self.tags)}]" if self.tags else ""
        return (
            f"  Stage {self.pipeline_stage} │ "
            f"{self.status_icon()}{self.status:<12} │ "
            f"{self.agent_id:<20} │ "
            f"{self.name:<22}"
            f"{tags_str}"
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a plain dictionary for logging or JSON output."""
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "status": self.status,
            "pipeline_stage": self.pipeline_stage,
            "registered_at": self.registered_at,
            "last_updated": self.last_updated,
            "tags": self.tags,
            "metadata": self.metadata,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"ACPAgentInfo("
            f"id={self.agent_id!r}, "
            f"status={self.status!r}, "
            f"stage={self.pipeline_stage}"
            f")"
        )


# ---------------------------------------------------------------------------
# ACPAgentRegistry — the central agent directory
# ---------------------------------------------------------------------------


class ACPAgentRegistry:
    """
    ACP Agent Registry — a runtime directory of all agents in the pipeline.

    Responsibilities
    ----------------
    1. REGISTRATION  : Accept and store ACPAgentInfo records when agents
                       announce themselves at startup.
    2. DISCOVERY     : Allow any component to look up agent info by ID,
                       list all agents, or filter by capability tags.
    3. STATUS TRACKING: Maintain up-to-date status for each agent so the
                       orchestrator (and operators) can see pipeline progress.
    4. SCHEMA INTROSPECTION: Expose each agent's input/output schemas so
                       the orchestrator can validate message payloads before
                       dispatching work (optional, but powerful).

    ACP CONCEPT: Why a Registry?
    -----------------------------
    Without a registry, adding a new agent to the pipeline requires editing
    every agent that might need to interact with it — because they would
    need to import it or hard-code its address.

    With a registry:
      • The new agent registers itself with its capabilities.
      • Existing agents (or the orchestrator) query the registry at runtime.
      • Zero changes needed to existing agent code.

    This is the ACP equivalent of Kubernetes' service discovery or
    DNS-based microservice routing: agents find each other by looking up
    a shared directory rather than embedding addresses in their code.

    Singleton pattern note
    ----------------------
    In ContentForge we create one registry instance in main.py and pass it
    to the pipeline builder.  All agents receive the same instance reference.
    In a distributed system you would back this with a key-value store
    (etcd, Consul, Redis) so agents on different machines could share it.
    """

    def __init__(self) -> None:
        # ----------------------------------------------------------------
        # _agents: maps agent_id (str) -> ACPAgentInfo
        # Ordered insertion is preserved (Python 3.7+ dict guarantee) so
        # that print_registry() shows agents in registration order, which
        # typically matches pipeline stage order when main.py registers
        # them sequentially.
        # ----------------------------------------------------------------
        self._agents: Dict[str, ACPAgentInfo] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, agent_info: ACPAgentInfo) -> None:
        """
        Register an agent with the registry.

        ACP CONCEPT: Self-Advertisement
        --------------------------------
        Agents announce themselves to the registry at startup, not at the
        moment they are needed.  This "eager registration" means the
        orchestrator can inspect the full pipeline roster before sending
        the first message — useful for validation and observability.

        Parameters
        ----------
        agent_info : ACPAgentInfo
            The agent's self-description.  Must have a non-empty agent_id.

        Raises
        ------
        ValueError
            If agent_info.agent_id is empty or blank.
        """
        if not agent_info.agent_id or not agent_info.agent_id.strip():
            raise ValueError(
                "ACPAgentRegistry.register() — agent_info.agent_id cannot be "
                "empty.  Every ACP agent must have a unique identifier."
            )

        is_update = agent_info.agent_id in self._agents
        # Stamp the registration time if this is a fresh registration
        if not is_update:
            agent_info.registered_at = datetime.now().isoformat()

        agent_info.last_updated = datetime.now().isoformat()
        self._agents[agent_info.agent_id] = agent_info

        action = "updated" if is_update else "registered"
        print(
            f"  [REGISTRY] ✓  Agent '{agent_info.agent_id}' {action}. "
            f"Stage: {agent_info.pipeline_stage}, "
            f"Status: {agent_info.status}. "
            f"({len(self._agents)} total agents in registry)"
        )

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def get_agent(self, agent_id: str) -> Optional[ACPAgentInfo]:
        """
        Look up a specific agent by its ID.

        Returns None if the agent is not registered (rather than raising,
        so callers can handle missing agents gracefully — e.g. by queuing
        the work and retrying once the agent comes online).

        Parameters
        ----------
        agent_id : str
            The unique agent identifier to look up.

        Returns
        -------
        ACPAgentInfo or None
        """
        return self._agents.get(agent_id, None)

    def get_agent_or_raise(self, agent_id: str) -> ACPAgentInfo:
        """
        Look up a specific agent by its ID, raising if not found.

        Use this when the agent MUST exist (e.g. during pipeline setup
        validation) and a missing registration is a fatal configuration error.

        Raises
        ------
        KeyError
            If no agent with the given agent_id is registered.
        """
        if agent_id not in self._agents:
            available = list(self._agents.keys())
            raise KeyError(
                f"Agent '{agent_id}' is not registered. Available agents: {available}"
            )
        return self._agents[agent_id]

    def list_agents(self) -> List[ACPAgentInfo]:
        """
        Return all registered agents, sorted by pipeline_stage then agent_id.

        The sort order makes the list read like a pipeline flow diagram:
        stage 1 (Researcher) at the top, stage 5 (Publisher) at the bottom.

        Returns
        -------
        List[ACPAgentInfo]
            Sorted list of all registered agent info records.
        """
        return sorted(
            self._agents.values(),
            key=lambda a: (a.pipeline_stage, a.agent_id),
        )

    def list_agent_ids(self) -> List[str]:
        """Return just the agent_id strings of all registered agents."""
        return [a.agent_id for a in self.list_agents()]

    def find_by_tag(self, tag: str) -> List[ACPAgentInfo]:
        """
        Return all agents that carry a given capability tag.

        ACP CONCEPT: Capability-Based Discovery
        -----------------------------------------
        Hard-coding receiver_id = "writer" in the researcher agent couples
        the pipeline topology to the agent implementation.  An alternative
        ACP pattern is to tag agents with their capabilities:

            researcher  → tags: ["research", "fact-gathering"]
            writer      → tags: ["writing", "drafting"]

        Then the orchestrator can find an agent dynamically:
            writers = registry.find_by_tag("writing")
            bus.publish(msg_to(writers[0].agent_id))

        This supports hot-swapping agents (swap a slow writer for a fast one)
        without changing any other agent.

        Parameters
        ----------
        tag : str
            The capability tag to search for (case-insensitive).

        Returns
        -------
        List[ACPAgentInfo]
        """
        tag_lower = tag.lower()
        return [
            a
            for a in self._agents.values()
            if any(t.lower() == tag_lower for t in a.tags)
        ]

    def find_by_status(self, status: str) -> List[ACPAgentInfo]:
        """
        Return all agents currently in a given status.

        Useful for the orchestrator to check:
          - Which agents are idle and available for new work?
          - Which agents have encountered errors?
          - Is the pipeline fully done?

        Parameters
        ----------
        status : str
            One of: "idle", "processing", "done", "error"
        """
        return [a for a in self._agents.values() if a.status == status]

    # ------------------------------------------------------------------
    # Status management
    # ------------------------------------------------------------------

    def update_status(self, agent_id: str, new_status: str) -> None:
        """
        Update the operational status of a registered agent.

        ACP CONCEPT: Live Status Tracking
        ----------------------------------
        Agents call this at key points in their lifecycle:
          - When they start processing a message → "processing"
          - When they finish successfully         → "done"
          - When they encounter an error          → "error"
          - When they are ready for new work      → "idle"

        The registry becomes a live dashboard of pipeline progress.  An
        operator watching the registry in real-time (or a monitoring agent
        polling it) can see exactly which stage the content is at and
        detect stalled agents (stuck in "processing" for too long).

        Parameters
        ----------
        agent_id : str
            The agent to update.
        new_status : str
            The new status.  Should be one of: "idle", "processing",
            "done", "error".  Non-standard values are accepted but a
            warning is printed so developers notice mistakes early.
        """
        valid_statuses = {"idle", "processing", "done", "error"}
        if new_status not in valid_statuses:
            print(
                f"  [REGISTRY] ⚠  Non-standard status '{new_status}' for "
                f"agent '{agent_id}'. Standard values: {sorted(valid_statuses)}"
            )

        if agent_id not in self._agents:
            print(
                f"  [REGISTRY] ✗  Cannot update status — "
                f"agent '{agent_id}' is not registered."
            )
            return

        old_status = self._agents[agent_id].status
        self._agents[agent_id].status = new_status
        self._agents[agent_id].last_updated = datetime.now().isoformat()

        # Visual transition arrow helps readers follow pipeline progression
        print(f"  [REGISTRY] 🔄 Agent '{agent_id}' status: {old_status} → {new_status}")

    def is_pipeline_complete(self) -> bool:
        """
        Return True if ALL registered agents have status "done".

        Convenience check for the orchestrator to know when the full
        pipeline has completed successfully.
        """
        if not self._agents:
            return False
        return all(a.status == "done" for a in self._agents.values())

    def has_errors(self) -> bool:
        """Return True if ANY registered agent has status "error"."""
        return any(a.status == "error" for a in self._agents.values())

    # ------------------------------------------------------------------
    # Schema introspection
    # ------------------------------------------------------------------

    def get_input_schema(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """
        Return the input payload schema for a given agent.

        Orchestrators use this to validate that the payload they are about
        to send matches what the agent expects — catch mismatches at
        dispatch time rather than inside the agent's handler.
        """
        agent = self.get_agent(agent_id)
        return agent.input_schema if agent else None

    def get_output_schema(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """
        Return the output payload schema for a given agent.

        Consumers of an agent's output use this to understand what fields
        to expect in RESPONSE payloads — self-documenting contracts.
        """
        agent = self.get_agent(agent_id)
        return agent.output_schema if agent else None

    # ------------------------------------------------------------------
    # Educational output
    # ------------------------------------------------------------------

    def print_registry(self) -> None:
        """
        Print a formatted table of all registered agents.

        This output is designed to be shown at pipeline startup so that
        anyone running ContentForge immediately sees:
          - Which agents are participating in this pipeline run
          - Their pipeline stage order (execution sequence)
          - Their current status
          - Their capability tags

        ACP CONCEPT: Transparency at Startup
        --------------------------------------
        Printing the registry before the first message is sent mirrors
        real-world practices in distributed systems:
          - Kubernetes prints "pod ready" events as services start
          - Microservice orchestrators log service discovery results
          - CI/CD pipelines list the stages before execution begins

        This transparency makes it easy to spot misconfiguration
        (wrong agent count, agent stuck in "error" before pipeline starts).
        """
        agents = self.list_agents()

        print("\n")
        print("=" * 70)
        print("  ACP AGENT REGISTRY — Registered Pipeline Agents")
        print("=" * 70)
        print(
            "  ACP CONCEPT: Agents register their capabilities here so\n"
            "  the pipeline knows WHO is available and WHAT they can do\n"
            "  before the first message is sent.  This is 'service discovery'.\n"
        )

        if not agents:
            print("  (no agents registered)")
            print("=" * 70)
            return

        # Header row
        print(f"  {'Stage':<7} │ {'Status':<14} │ {'Agent ID':<20} │ Name")
        print(f"  {'─' * 7}─┼─{'─' * 14}─┼─{'─' * 20}─┼─{'─' * 22}")

        for agent in agents:
            print(agent.summary_line())

        print()
        print("  ── AGENT DESCRIPTIONS ──────────────────────────────────────")
        for agent in agents:
            print(f"\n  [{agent.pipeline_stage}] {agent.name} ({agent.agent_id})")
            print(f"      {agent.description}")
            if agent.input_schema:
                print(f"      Input  : {list(agent.input_schema.keys())}")
            if agent.output_schema:
                print(f"      Output : {list(agent.output_schema.keys())}")

        print()
        print(f"  Total agents registered: {len(agents)}")
        print("=" * 70)

    def print_status_board(self) -> None:
        """
        Print a compact live status board of all agents.

        Designed to be called AFTER the pipeline completes to show the
        final state of every agent — did they all finish successfully?
        """
        print("\n")
        print("=" * 50)
        print("  PIPELINE STATUS BOARD")
        print("=" * 50)

        for agent in self.list_agents():
            icon = agent.status_icon()
            print(
                f"  {icon} [{agent.pipeline_stage}] "
                f"{agent.name:<25} — {agent.status.upper()}"
            )

        print()
        if self.is_pipeline_complete():
            print("  ✓  ALL AGENTS DONE — Pipeline completed successfully!")
        elif self.has_errors():
            error_agents = [a.agent_id for a in self.find_by_status("error")]
            print(f"  ✗  ERRORS in: {error_agents}")
        else:
            in_progress = [a.agent_id for a in self.find_by_status("processing")]
            print(f"  ⚙  Still processing: {in_progress}")

        print("=" * 50)

    def __len__(self) -> int:
        return len(self._agents)

    def __repr__(self) -> str:  # pragma: no cover
        agent_ids = list(self._agents.keys())
        return f"ACPAgentRegistry(agents={agent_ids})"
