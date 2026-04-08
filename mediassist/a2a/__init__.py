"""
A2A (Agent-to-Agent) Protocol Package
=======================================
This package implements a simulated version of the Agent2Agent (A2A) Protocol,
an open standard proposed by Google for enabling AI agents to communicate,
discover, and collaborate with one another.

WHAT IS A2A?
------------
A2A defines how one agent (the "client") can find, authenticate with, and
delegate tasks to another agent (the "server"). It solves the problem of
agent interoperability: without a standard protocol, every multi-agent system
needs custom glue code between each pair of agents.

Key concepts in A2A:
  ┌─────────────────────────────────────────────────────────────────┐
  │  Agent Card    - A machine-readable "business card" that        │
  │                  describes what an agent can do (its            │
  │                  capabilities, endpoint URL, auth method, etc.) │
  │                                                                 │
  │  A2A Registry  - A discovery service where agents register      │
  │                  their Agent Cards. Other agents query it to    │
  │                  find a capable peer for a given task.          │
  │                                                                 │
  │  A2A Task      - A structured work request sent from one agent  │
  │                  (sender) to another (receiver). Contains an    │
  │                  intent string and a payload dict.              │
  │                                                                 │
  │  A2A Response  - The structured reply from the receiving agent, │
  │                  containing a status, result dict, and message. │
  │                                                                 │
  │  A2A Client    - The component that serialises a Task, sends    │
  │                  it to the correct agent endpoint, and          │
  │                  deserialises the Response.                     │
  └─────────────────────────────────────────────────────────────────┘

HOW IT IS USED HERE:
--------------------
  OrchestratorAgent
      │
      │  (1) Queries A2ARegistry to discover which agent
      │      handles capability "schedule_appointment"
      │      → finds SchedulingAgent's AgentCard
      │
      │  (2) Builds an A2ATask:
      │        { intent: "book_appointment",
      │          payload: { patient_id, date, time, doctor } }
      │
      │  (3) Uses A2AClient.send_task(agent_card, task)
      │      → [A2A] log messages appear here
      │
      │  (4) Receives A2AResponse with booking confirmation
      ▼
  SchedulingAgent.process_task(task)
      │
      │  (5) Uses its MCPClient to call tools on the MCP Server
      │      → [MCP] log messages appear here
      │
      └─▶ Returns A2AResponse back up the chain

WHY A2A?
--------
  - DECOUPLING   : The Orchestrator doesn't need to know HOW the
                   SchedulingAgent books appointments; it just sends a Task.
  - DISCOVERABILITY: New agents can join the system by registering an
                   AgentCard — the Orchestrator finds them automatically.
  - INTEROPERABILITY: Any A2A-compliant agent (Python, Node, Java…) can
                   participate in the same system.
  - AUDITABILITY : Every task handoff is a discrete, logged, structured event.
"""

from .a2a_protocol import A2AClient, A2ARegistry, A2AResponse, A2ATask
from .agent_card import AgentCard

__all__ = [
    "AgentCard",
    "A2ATask",
    "A2AResponse",
    "A2AClient",
    "A2ARegistry",
]
