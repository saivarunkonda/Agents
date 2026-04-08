"""
Agents Package — MediAssist Multi-Agent System
===============================================
This package contains all AI agents in the MediAssist system.

AGENT ARCHITECTURE OVERVIEW:
------------------------------
MediAssist uses a hierarchical multi-agent architecture with one
Orchestrator that delegates to three specialised sub-agents:

    ┌─────────────────────────────────────────────────────────────┐
    │                     USER / APPLICATION                      │
    └──────────────────────────┬──────────────────────────────────┘
                               │  handle_request(patient_id, type)
                               ▼
    ┌─────────────────────────────────────────────────────────────┐
    │                  OrchestratorAgent                          │
    │  • Owns the A2ARegistry (knows all sub-agents)              │
    │  • Owns the A2AClient   (sends tasks to sub-agents)         │
    │  • Routes requests to the right agent(s) via A2A discovery  │
    │  • Combines results from multiple agents for complex tasks   │
    └──────┬────────────────┬────────────────┬────────────────────┘
           │   [A2A]        │   [A2A]        │   [A2A]
           ▼                ▼                ▼
    ┌────────────┐  ┌──────────────┐  ┌───────────────┐
    │ Scheduling │  │    Records   │  │   Pharmacy    │
    │   Agent    │  │    Agent     │  │    Agent      │
    │            │  │              │  │               │
    │ • schedule │  │ • get_patient│  │ • check_rx    │
    │ _appoint-  │  │   _records   │  │ • request_    │
    │  ment      │  │ • get_medical│  │   refill      │
    │ • check_   │  │   _history   │  │               │
    │  availability│  │              │  │               │
    └──────┬─────┘  └──────┬───────┘  └───────┬───────┘
           │  [MCP]        │  [MCP]            │  [MCP]
           └───────────────┴───────────────────┘
                               │
                               ▼
                    ┌──────────────────────┐
                    │      MCP Server      │
                    │  (medical databases) │
                    │  • patients.json     │
                    │  • appointments.json │
                    │  • prescriptions.json│
                    └──────────────────────┘

AGENT ROLES:
------------
  OrchestratorAgent
      The "brain" of the system. Does not talk to databases directly —
      instead it uses A2A to delegate every task to a specialist agent.
      Supports three request types:
        - "full_checkup"      → calls ALL three sub-agents, merges results
        - "book_appointment"  → discovers & delegates to SchedulingAgent
        - "prescriptions"     → discovers & delegates to PharmacyAgent

  SchedulingAgent
      Handles appointment-related tasks. Uses MCP tools:
        - list_available_slots  (checks doctor calendar)
        - book_appointment      (writes new appointment to DB)

  RecordsAgent
      Retrieves and summarises patient health information. Uses MCP tools:
        - get_patient_record  (demographics + conditions)
        - get_appointments    (upcoming visits)

  PharmacyAgent
      Manages prescription status and refill requests. Uses MCP tools:
        - get_prescriptions   (medications + refill counts)

PROTOCOL USAGE SUMMARY:
------------------------
  Every sub-agent:
    1. Declares an AgentCard (A2A — for discovery)
    2. Implements process_task(A2ATask) → A2AResponse  (A2A — task handler)
    3. Uses MCPClient internally to access data  (MCP — data access)

  The OrchestratorAgent:
    1. Registers all sub-agents in an A2ARegistry  (A2A — registration)
    2. Uses A2AClient to send tasks to sub-agents  (A2A — delegation)
    3. Combines sub-agent responses into a unified output
"""

from .orchestrator_agent import OrchestratorAgent
from .pharmacy_agent import PharmacyAgent
from .records_agent import RecordsAgent
from .scheduling_agent import SchedulingAgent

__all__ = [
    "OrchestratorAgent",
    "SchedulingAgent",
    "RecordsAgent",
    "PharmacyAgent",
]
