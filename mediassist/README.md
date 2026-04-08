# MediAssist — Healthcare Agent System

> A complete Python demonstration of **MCP (Model Context Protocol)** and **A2A (Agent-to-Agent Protocol)** working together in a realistic multi-agent healthcare scenario.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Architecture](#2-architecture)
3. [How MCP Works in This Project](#3-how-mcp-works-in-this-project)
4. [How A2A Works in This Project](#4-how-a2a-works-in-this-project)
5. [Directory Structure](#5-directory-structure)
6. [How to Run](#6-how-to-run)
7. [Demo Scenarios](#7-demo-scenarios)
8. [Example Output](#8-example-output)
9. [Key Design Decisions](#9-key-design-decisions)
10. [Extending the System](#10-extending-the-system)

---

## 1. Project Overview

MediAssist simulates a hospital coordination system where patients can:
- Get a **full checkup summary** (records + prescriptions + availability)
- **Book appointments** with their doctor
- **Check prescription status** and refill eligibility

The system is built entirely on two open AI-agent protocols:

| Protocol | Full Name              | Purpose in MediAssist                                       |
|----------|------------------------|-------------------------------------------------------------|
| **MCP**  | Model Context Protocol | Agents connect to medical databases (patients, appointments, prescriptions) as external "tool servers" |
| **A2A**  | Agent-to-Agent Protocol| The Orchestrator delegates tasks to specialised sub-agents (SchedulingAgent, RecordsAgent, PharmacyAgent) via structured task/response messages |

**No external packages are required.** The entire project runs on Python 3.7+ standard library.

---

## 2. Architecture

### High-Level System Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                        User / Application                           │
│                    (calls main.py scenarios)                        │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                    handle_request(patient_id, type)
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│                      OrchestratorAgent                               │
│                                                                      │
│  ┌────────────────────┐    ┌─────────────────────────────────────┐  │
│  │   A2ARegistry      │    │   A2AClient                         │  │
│  │                    │    │                                     │  │
│  │ scheduling-agent ──┼──► │  send_task(agent_card, task)        │  │
│  │ records-agent    ──┼──► │    1. capability check              │  │
│  │ pharmacy-agent   ──┼──► │    2. auth (Bearer token)           │  │
│  └────────────────────┘    │    3. dispatch to agent endpoint    │  │
│                            └─────────────────────────────────────┘  │
└───────┬───────────────────────────┬────────────────────┬────────────┘
        │         [A2A]             │      [A2A]         │    [A2A]
        ▼                           ▼                    ▼
┌───────────────┐         ┌─────────────────┐   ┌───────────────────┐
│SchedulingAgent│         │  RecordsAgent   │   │  PharmacyAgent    │
│               │         │                 │   │                   │
│ AgentCard:    │         │ AgentCard:      │   │ AgentCard:        │
│ • schedule_   │         │ • get_patient_  │   │ • check_          │
│   appointment │         │   records       │   │   prescriptions   │
│ • check_      │         │ • get_medical_  │   │ • request_refill  │
│   availability│         │   history       │   │                   │
│               │         │                 │   │                   │
│  MCPClient ◄──┼──┐      │  MCPClient ◄────┼─┐ │  MCPClient ◄──────┼─┐
└───────────────┘  │      └─────────────────┘ │ └───────────────────┘ │
                   │ [MCP]                     │[MCP]                  │[MCP]
                   └───────────────────────────┴───────────────────────┘
                                               │
                                               ▼
                   ┌───────────────────────────────────────────────────┐
                   │               MCP Server                          │
                   │         "MediAssist MCP Server"                   │
                   │                                                   │
                   │  Tools Registry:                                  │
                   │  ┌─────────────────────────────────────────────┐  │
                   │  │ get_patient_record(patient_id)               │  │
                   │  │ get_appointments(patient_id)                 │  │
                   │  │ book_appointment(patient_id,date,time,doctor)│  │
                   │  │ get_prescriptions(patient_id)                │  │
                   │  │ list_available_slots(doctor, date)           │  │
                   │  └─────────────────────────────────────────────┘  │
                   └─────────────┬─────────────┬─────────────┬─────────┘
                                 │             │             │
                                 ▼             ▼             ▼
                          patients.json  appointments  prescriptions
                                           .json           .json
```

### Protocol Layering

```
  ┌─────────────────────────────────────────────────────────────┐
  │  Layer 2 — Agent Collaboration                              │
  │  [ A2A Protocol ]                                           │
  │  OrchestratorAgent ←── A2ATask / A2AResponse ──→ SubAgents  │
  ├─────────────────────────────────────────────────────────────┤
  │  Layer 1 — Tool / Data Access                               │
  │  [ MCP Protocol ]                                           │
  │  SubAgents ←── call_tool / result ──→ MCPServer (databases) │
  └─────────────────────────────────────────────────────────────┘
```

---

## 3. How MCP Works in This Project

### What is MCP?

**Model Context Protocol (MCP)** is an open standard that lets AI agents connect to external data sources and tools in a standardised way — like a USB interface for AI agents.

Instead of every agent hard-coding how to query a specific database or API, agents connect to an **MCP Server** that exposes a registry of named **tools**. The agent calls `call_tool("tool_name", **kwargs)` without needing to know anything about the underlying implementation.

### MCP Components in MediAssist

#### `mcp/mcp_server.py` — The MCP Server

The server is the authoritative registry of tools. At startup it registers 5 tools:

```
Tool Name                 | Description
──────────────────────────┼────────────────────────────────────────────────
get_patient_record        │ Fetch demographics + conditions for a patient
get_appointments          │ List all appointments for a patient
book_appointment          │ Create and persist a new appointment
get_prescriptions         │ Retrieve active medications + refill counts
list_available_slots      │ Check open time slots for a doctor on a date
```

Each tool is stored as a dict `{ "name": ..., "description": ..., "fn": callable }`. When a client calls `call_tool("get_patient_record", patient_id="P001")`, the server looks up the tool by name, executes the function, and returns a standard result envelope:

```python
{
    "status":  "ok" | "error",
    "message": "Patient record found for P001",
    "data":    { "id": "P001", "name": "Alice Johnson", ... }
}
```

#### `mcp/mcp_client.py` — The MCP Client

The client is what agents use to talk to the server. Each sub-agent owns one `MCPClient` instance. Calling `mcp_client.call_tool(...)` produces two log lines:

```
[MCP] → CALL    [10:23:45.123] get_patient_record(patient_id='P001')   | caller='RecordsAgent'
[MCP] ← RESULT  [10:23:45.124] get_patient_record  | status='ok'  message='Patient record found'
```

This mirrors the real MCP wire format where calls are JSON-RPC messages:
```json
{
  "jsonrpc": "2.0",
  "method":  "tools/call",
  "params":  { "name": "get_patient_record", "arguments": { "patient_id": "P001" } }
}
```

#### How MCP enforces separation of concerns

```
  PharmacyAgent                MCPClient              MCPServer
       │                           │                      │
       │  call_tool("get_          │                      │
       │  prescriptions",          │                      │
       │  patient_id="P001")  ────►│                      │
       │                           │  dispatch(tool_name, │
       │                           │  **kwargs)      ────►│
       │                           │                      │  reads prescriptions.json
       │                           │◄──── result envelope─┤
       │◄──── result dict ─────────│                      │
```

The `PharmacyAgent` never touches `prescriptions.json` directly. If the storage layer changes (e.g., from JSON to a PostgreSQL database), only the MCP Server changes — all three sub-agents stay exactly the same.

---

## 4. How A2A Works in This Project

### What is A2A?

**Agent-to-Agent (A2A) Protocol** is an open standard (proposed by Google, 2025) that defines how one AI agent can discover, authenticate with, and delegate tasks to another agent.

Without A2A, every multi-agent system needs custom glue code between every pair of agents. With A2A, agents communicate through a standard set of primitives that make the system composable, extensible, and auditable.

### A2A Components in MediAssist

#### `a2a/agent_card.py` — AgentCard

An **AgentCard** is a machine-readable identity document every agent publishes. It answers:
- **WHO** the agent is (`agent_id`, `name`)
- **WHAT** it can do (`capabilities` list)
- **WHERE** to reach it (`endpoint` URL)
- **HOW** to authenticate (`auth_scheme`)

Example — the SchedulingAgent's card:
```python
AgentCard(
    agent_id     = "scheduling-agent-001",
    name         = "SchedulingAgent",
    description  = "Specialist agent for patient appointment scheduling.",
    capabilities = ["schedule_appointment", "check_availability"],
    endpoint     = "http://localhost:8001/a2a",
    auth_scheme  = "Bearer",
)
```

In real A2A, this card is served as JSON at `/.well-known/agent.json` on the agent's host.

#### `a2a/a2a_protocol.py` — A2ATask, A2AResponse, A2AClient, A2ARegistry

**A2ATask** — the work-request object:
```python
A2ATask(
    task_id     = "uuid-auto-generated",
    sender_id   = "orchestrator-agent-001",
    receiver_id = "scheduling-agent-001",
    intent      = "schedule_appointment",
    payload     = { "patient_id": "P002", "date": "2025-08-15",
                    "time": "10:00", "doctor": "Dr. Lee" },
    status      = "pending"  # → in_progress → completed | failed
)
```

**A2AResponse** — the reply from the receiving agent:
```python
A2AResponse(
    task_id  = "same-uuid-as-task",   # for correlation
    agent_id = "scheduling-agent-001",
    status   = "completed",
    result   = { "appointment": { "id": "A003", ... } },
    message  = "Appointment A003 confirmed for P002 with Dr. Lee on 2025-08-15 at 10:00."
)
```

**A2ARegistry** — the discovery service where agents register and are found:
```python
registry.register(scheduling_agent.agent_card)  # agent self-registers
card = registry.discover("schedule_appointment") # orchestrator queries by capability
# → Returns SchedulingAgent's AgentCard
```

**A2AClient** — the sender-side component that drives the full task lifecycle:

```
Step 1: Capability Check
  "Does SchedulingAgent's AgentCard declare 'schedule_appointment'?"
  → Yes → proceed | No → RuntimeError (fail fast)

Step 2: Authentication
  Simulate adding: Authorization: Bearer eyJhbGci...orchestrator...scheduling
  (In production: sign a JWT with private key, verified by receiver)

Step 3: Task Dispatch
  POST http://localhost:8001/a2a
  Body: { task_id, sender_id, receiver_id, intent, payload }
  (Simulated: calls agent.process_task(task) in-process)

Step 4: Response Handling
  Deserialise A2AResponse → update task.status → return to orchestrator
```

### The Full A2A + MCP Flow (Scenario 2 — Book Appointment)

```
main.py
  │
  │ orchestrator.handle_request("P002", "book_appointment",
  │                              date="2025-08-15", time="10:00", doctor="Dr. Lee")
  ▼
OrchestratorAgent._handle_book_appointment()
  │
  ├─[A2A DISCOVER]─► registry.discover("schedule_appointment")
  │                         └─► scans registered AgentCards
  │                         └─► finds SchedulingAgent card
  │                  ◄─ returns AgentCard
  │
  ├─ builds A2ATask(intent="schedule_appointment", payload={...})
  │
  ├─[A2A SEND]──────► a2a_client.send_task(agent_card, task, scheduling_agent.process_task)
  │                         ├─ [A2A] ● CAPABILITY CHECK  → ✓ OK
  │                         ├─ [A2A] ● AUTH  Bearer eyJ...
  │                         ├─ [A2A] → SEND TASK  POST http://localhost:8001/a2a
  │                         │
  │                         └─► SchedulingAgent.process_task(task)
  │                                 │
  │                                 ├─[MCP CALL]─► mcp_client.call_tool(
  │                                 │              "list_available_slots",
  │                                 │              doctor="Dr. Lee", date="2025-08-15")
  │                                 │           ◄─ { status:"ok", data:{slots:[...]} }
  │                                 │
  │                                 ├─ validates "10:00" is in available slots
  │                                 │
  │                                 ├─[MCP CALL]─► mcp_client.call_tool(
  │                                 │              "book_appointment",
  │                                 │              patient_id="P002", date="2025-08-15",
  │                                 │              time="10:00", doctor="Dr. Lee")
  │                                 │           ◄─ { status:"ok", data:{id:"A003",...} }
  │                                 │
  │                                 └─► returns A2AResponse(status="completed", ...)
  │                         │
  │                         └─[A2A RESPONSE]─► ✓ RESPONSE OK  task_id=...  status='completed'
  │
  └─► formats and returns booking confirmation to main.py
```

---

## 5. Directory Structure

```
project1_mediassist/
│
├── README.md                   ← You are here
├── requirements.txt            ← stdlib only; no external packages
├── main.py                     ← Entry point — runs all 3 demo scenarios
│
├── mcp/                        ← Model Context Protocol layer
│   ├── __init__.py             ← Package docstring explaining MCP concepts
│   ├── mcp_server.py           ← Simulated MCP Server with 5 registered tools
│   └── mcp_client.py           ← MCP Client with [MCP] logging on every call
│
├── a2a/                        ← Agent-to-Agent Protocol layer
│   ├── __init__.py             ← Package docstring explaining A2A concepts
│   ├── agent_card.py           ← AgentCard dataclass (discovery metadata)
│   └── a2a_protocol.py         ← A2ATask, A2AResponse, A2AClient, A2ARegistry
│
├── agents/                     ← Agent implementations
│   ├── __init__.py             ← Architecture overview + agent role summary
│   ├── orchestrator_agent.py   ← Main brain: A2A client + registry coordinator
│   ├── scheduling_agent.py     ← A2A server: appointment booking specialist
│   ├── records_agent.py        ← A2A server: medical records specialist
│   └── pharmacy_agent.py       ← A2A server: prescription management specialist
│
└── data/                       ← Mock medical databases (JSON)
    ├── patients.json            ← Patient demographics + conditions
    ├── appointments.json        ← Scheduled appointments (written by Scenario 2)
    └── prescriptions.json       ← Active medications + refill counts
```

---

## 6. How to Run

### Prerequisites

- **Python 3.7 or newer** (uses `dataclasses`, f-strings, `pathlib` — all stdlib)
- No `pip install` required

### Run the Demo

```bash
# Navigate to the project root
cd Agent/project1_mediassist

# Run the entry point
python main.py
```

Or from any directory:

```bash
python Agent/project1_mediassist/main.py
```

### What You Will See

The terminal output is colour-coded. Each log prefix tells you which protocol layer is active:

| Prefix                 | Colour  | Meaning                                          |
|------------------------|---------|--------------------------------------------------|
| `[MCP]`                | Cyan    | Model Context Protocol — tool call to MCP Server |
| `[A2A]`                | Magenta | Agent-to-Agent — task dispatch between agents    |
| `[OrchestratorAgent]`  | Blue    | Main coordinator log messages                    |
| `[SchedulingAgent]`    | Teal    | Appointment booking specialist                   |
| `[RecordsAgent]`       | Purple  | Medical records specialist                       |
| `[PharmacyAgent]`      | Orange  | Prescription management specialist               |

The demo pauses between scenarios (press **Enter** in interactive mode, or proceeds automatically when piped).

---

## 7. Demo Scenarios

### Scenario 1 — Full Patient Checkup (P001 / Alice Johnson)

**What happens:** The OrchestratorAgent fans out A2A tasks to **all three sub-agents** simultaneously, then aggregates the three responses into one comprehensive report.

**Protocols exercised:**
```
A2A:  3× discover() queries
      3× task dispatches (RecordsAgent, PharmacyAgent, SchedulingAgent)
      3× A2AResponse aggregation

MCP:  get_patient_record("P001")
      get_appointments("P001")
      get_prescriptions("P001")
      list_available_slots("Dr. Smith", "2025-08-20")
```

**Output:** Unified checkup report with patient demographics, conditions, appointment history, prescription status with refill alerts, and available slots for Dr. Smith on 2025-08-20.

---

### Scenario 2 — Book an Appointment (P002 / Bob Williams)

**What happens:** The OrchestratorAgent discovers the SchedulingAgent via the A2A Registry, then delegates a booking request. The SchedulingAgent first verifies the slot is available via MCP, then writes a new appointment via MCP.

**Protocols exercised:**
```
A2A:  1× discover("schedule_appointment")
      1× task dispatch → SchedulingAgent
      1× A2AResponse with booking confirmation

MCP:  list_available_slots("Dr. Lee", "2025-08-15")    [read]
      book_appointment("P002", "2025-08-15",             [write]
                       "10:00", "Dr. Lee")
```

**Side effect:** `data/appointments.json` is updated with the new appointment. You can open it after the demo to see the persisted record.

---

### Scenario 3 — Prescription Status Check (P001 / Alice Johnson)

**What happens:** The OrchestratorAgent discovers the PharmacyAgent and delegates a read-only prescription query. Alice has two medications: `Metformin 500mg` (2 refills — OK) and `Lisinopril 10mg` (0 refills — requires doctor authorisation). The PharmacyAgent surfaces this with per-medication analysis and actionable alerts.

**Protocols exercised:**
```
A2A:  1× discover("check_prescriptions")
      1× task dispatch → PharmacyAgent
      1× A2AResponse with prescription report

MCP:  get_prescriptions("P001")
```

**Output:** Per-medication refill status table with eligibility analysis (days since last fill) and alerts flagging the Lisinopril situation.

---

## 8. Example Output

Below is an abbreviated excerpt showing the key protocol interactions:

```
════════════════════════════════════════════════════════════════════════
                MediAssist — Healthcare Agent System
                Multi-Agent Demo: MCP + A2A Protocols
════════════════════════════════════════════════════════════════════════

╔══════════════════════════════════════════════════════════════╗
║  MediAssist — System Bootstrap                               ║
╚══════════════════════════════════════════════════════════════╝
[OrchestratorAgent] [10:23:44.001] Starting OrchestratorAgent ...
[OrchestratorAgent] [10:23:44.002] ▶ Step 1: Initialising MCP Server ...
[OrchestratorAgent] [10:23:44.003] ✓ MCP Server ready: 5 tools registered

── Initialising SchedulingAgent ───────────────────────────────
[MCP] Client 'SchedulingAgent' connected to server 'MediAssist MCP Server'.
      Available tools: ['get_patient_record', 'get_appointments',
                        'book_appointment', 'get_prescriptions',
                        'list_available_slots']

── Initialising RecordsAgent ──────────────────────────────────
[MCP] Client 'RecordsAgent' connected to server 'MediAssist MCP Server'.

── Initialising PharmacyAgent ─────────────────────────────────
[MCP] Client 'PharmacyAgent' connected to server 'MediAssist MCP Server'.

[A2A] ● REGISTERED  agent_id='scheduling-agent-001'  name='SchedulingAgent'
          capabilities=['schedule_appointment', 'check_availability']
          endpoint='http://localhost:8001/a2a'
[A2A] ● REGISTERED  agent_id='records-agent-001'  name='RecordsAgent'
          capabilities=['get_patient_records', 'get_medical_history']
          endpoint='http://localhost:8002/a2a'
[A2A] ● REGISTERED  agent_id='pharmacy-agent-001'  name='PharmacyAgent'
          capabilities=['check_prescriptions', 'request_refill']
          endpoint='http://localhost:8003/a2a'

┌─────────────────────────────────────────────────
│  A2A REGISTRY — Registered Agents
├─────────────────────────────────────────────────
│  SchedulingAgent       caps: schedule_appointment, check_availability
│  id=scheduling-agent-001  endpoint=http://localhost:8001/a2a
│
│  RecordsAgent          caps: get_patient_records, get_medical_history
│  id=records-agent-001  endpoint=http://localhost:8002/a2a
│
│  PharmacyAgent         caps: check_prescriptions, request_refill
│  id=pharmacy-agent-001 endpoint=http://localhost:8003/a2a
└─────────────────────────────────────────────────


┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃  SCENARIO 1: Full Patient Checkup                                    ┃
┃  Patient : P001 — Alice Johnson                                      ┃
┃  Fan-out to ALL 3 sub-agents: records + prescriptions + availability ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛

── Fan-out Step 1/3 — RecordsAgent: get_medical_history ──────────────

[A2A] ● DISCOVER  Searching 3 registered agent(s) for capability 'get_medical_history' ...
[A2A] ✓ FOUND     capability='get_medical_history' → agent='RecordsAgent'
                  endpoint='http://localhost:8002/a2a'
[A2A] ● CAPABILITY CHECK  Does RecordsAgent support intent 'get_medical_history'?
[A2A] ✓ CAPABILITY OK  RecordsAgent declares 'get_medical_history'
[A2A] ● AUTH  Scheme='Bearer'  Token=eyJhbGci...orchestr...records-a
[A2A] → SEND TASK  POST http://localhost:8002/a2a
        task_id=4f3a1b2c...  intent='get_medical_history'
        payload_keys=['patient_id']  from='orchestrator-agent-001' → to='records-agent-001'

[RecordsAgent] [10:23:44.201] ▶ TASK RECEIVED  intent='get_medical_history'
[RecordsAgent] [10:23:44.202] → [MCP] Fetching patient demographics ...
[MCP] → CALL    [10:23:44.203] get_patient_record(patient_id='P001') | caller='RecordsAgent'
[MCP] ← RESULT  [10:23:44.204] get_patient_record  | status='ok'
                 message='Patient record found for P001'  data={id, name, dob, ...}
[RecordsAgent] [10:23:44.205] ✓ Demographics retrieved: Alice Johnson  conditions=['hypertension', 'diabetes']
[RecordsAgent] [10:23:44.206] → [MCP] Fetching appointment history ...
[MCP] → CALL    [10:23:44.207] get_appointments(patient_id='P001') | caller='RecordsAgent'
[MCP] ← RESULT  [10:23:44.208] get_appointments  | status='ok'
                 message='Found 1 appointment(s) for P001'  data=[1 item(s)]
[RecordsAgent] [10:23:44.209] ✓ HISTORY COMPILED: Alice Johnson — 1 appt(s), 1 upcoming

[A2A] ✓ RESPONSE OK  task_id=4f3a1b2c...  agent='records-agent-001'
                      status='completed'

── Fan-out Step 2/3 — PharmacyAgent: check_prescriptions ─────────────

[A2A] ● DISCOVER  Searching 3 registered agent(s) for capability 'check_prescriptions' ...
[A2A] ✓ FOUND     capability='check_prescriptions' → agent='PharmacyAgent'
[A2A] → SEND TASK  POST http://localhost:8003/a2a  intent='check_prescriptions'

[PharmacyAgent] [10:23:44.301] ▶ TASK RECEIVED  intent='check_prescriptions'
[PharmacyAgent] [10:23:44.302] → [MCP] Fetching prescription records ...
[MCP] → CALL    [10:23:44.303] get_prescriptions(patient_id='P001') | caller='PharmacyAgent'
[MCP] ← RESULT  [10:23:44.304] get_prescriptions | status='ok'
                 message='Found 2 prescription(s) for P001'  data=[2 item(s)]
[PharmacyAgent] [10:23:44.305] ✓ Retrieved 2 prescription(s). Analysing refill status ...
[PharmacyAgent] [10:23:44.306]   Metformin 500mg          refills=2  status=OK
[PharmacyAgent] [10:23:44.307]   Lisinopril 10mg           refills=0  status=EMPTY

[A2A] ✓ RESPONSE OK  status='completed'  message='Patient P001: 2 active prescription(s)...'

── Fan-out Step 3/3 — SchedulingAgent: check_availability ────────────

[A2A] ● DISCOVER  Searching 3 registered agent(s) for capability 'check_availability' ...
[A2A] ✓ FOUND     capability='check_availability' → agent='SchedulingAgent'
[A2A] → SEND TASK  POST http://localhost:8001/a2a  intent='check_availability'

[SchedulingAgent] [10:23:44.401] ▶ TASK RECEIVED  intent='check_availability'
[MCP] → CALL    [10:23:44.402] list_available_slots(doctor='Dr. Smith', date='2025-08-20')
[MCP] ← RESULT  [10:23:44.403] list_available_slots | status='ok'
                 data={doctor, date, available_slots}

[A2A] ✓ RESPONSE OK  status='completed'  message='Found 14 available slot(s)...'

┌─ FULL CHECKUP RESULT ──────────────────────────────────────────────┐
╔════════════════════════════════════════════════════════════════╗
║            MediAssist — FULL CHECKUP REPORT                    ║
╚════════════════════════════════════════════════════════════════╝
  Patient ID : P001
  Protocol   : A2A fan-out × 3 agents + MCP data retrieval

══ 1. PATIENT RECORDS  [via RecordsAgent → A2A → MCP] ══════════
  Name        : Alice Johnson
  Date of Birth: 1985-03-15
  Doctor      : Dr. Smith
  Conditions  : Hypertension, Diabetes
  Appointments: 1 on record
    • [A001] 2025-08-10 at 09:00 — Dr. Smith [CONFIRMED]

══ 2. PRESCRIPTION STATUS  [via PharmacyAgent → A2A → MCP] ════
  • Metformin 500mg          refills=2  [OK]    last: 2025-07-01
  • Lisinopril 10mg           refills=0  [EMPTY] last: 2025-06-15
  Alerts:
    ⚠ Lisinopril 10mg: No refills remaining — doctor authorisation required.

══ 3. AVAILABILITY  [via SchedulingAgent → A2A → MCP] ══════════
  Doctor : Dr. Smith  |  Date : 2025-08-20
  Open slots: 09:00, 09:30, 10:00, 10:30, 11:00, ... (14 slots)

══ 4. CLINICAL ALERTS ══════════════════════════════════════════
  ⚕ Patient has hypertension — ensure blood pressure monitored at every visit.
  ⚕ Patient has diabetes — regular HbA1c checks recommended.
  ⚕ Comorbid hypertension + diabetes: heightened cardiovascular risk.
  ⚕ Lisinopril 10mg: No refills remaining — doctor authorisation required.
└────────────────────────────────────────────────────────────────┘


┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃  SCENARIO 2: Book an Appointment                                     ┃
┃  Patient : P002 — Bob Williams                                       ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛

[A2A] ● DISCOVER  → finds SchedulingAgent
[A2A] → SEND TASK  intent='schedule_appointment'

[SchedulingAgent] → [MCP] list_available_slots("Dr. Lee", "2025-08-15")
                 ← 10:00 is available ✓
[SchedulingAgent] → [MCP] book_appointment("P002", "2025-08-15", "10:00", "Dr. Lee")
                 ← A003 confirmed ✓

┌─ BOOKING CONFIRMATION ────────────────────────────────────────────┐
  ✓ Appointment CONFIRMED
  Appointment ID : A003
  Patient ID     : P002
  Doctor         : Dr. Lee
  Date           : 2025-08-15
  Time           : 10:00
  Status         : CONFIRMED
└────────────────────────────────────────────────────────────────────┘


┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃  SCENARIO 3: Prescription Status Check                               ┃
┃  Patient : P001 — Alice Johnson                                      ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛

[A2A] ● DISCOVER  → finds PharmacyAgent
[A2A] → SEND TASK  intent='check_prescriptions'
[MCP] → CALL    get_prescriptions(patient_id='P001')
[MCP] ← RESULT  [2 item(s)]

┌─ PRESCRIPTION STATUS ─────────────────────────────────────────────┐
  Patient P001: 2 active prescription(s). 1 requires doctor authorisation.

  Medications:
  • Metformin 500mg           refills=2  [OK]   last filled: 2025-07-01  (28d ago)
  • Lisinopril 10mg            refills=0  [EMPTY] last filled: 2025-06-15  (41d ago)

  Alerts:
  ⚠ Lisinopril 10mg: No refills remaining — doctor authorisation required.
└────────────────────────────────────────────────────────────────────┘
```

---

## 9. Key Design Decisions

### 1. Orchestrator has NO MCP client

The `OrchestratorAgent` does **not** hold an `MCPClient`. It only uses A2A. All data access is delegated to specialist sub-agents. This means:
- The orchestrator can be replaced with an LLM planner without changing data schemas
- Sub-agents can be upgraded (new tools, new data sources) without touching the orchestrator
- The separation is enforced by code structure, not just convention

### 2. Capability-based routing (not name-based)

The orchestrator never does:
```python
# ✗ BAD — tight coupling
from agents.scheduling_agent import SchedulingAgent
scheduling_agent.book(...)
```

Instead, it does:
```python
# ✓ GOOD — A2A discovery
agent_card = registry.discover("schedule_appointment")
a2a_client.send_task(agent_card, task, agent.process_task)
```

This means a new specialised booking agent can be added by registering an AgentCard with `capabilities=["schedule_appointment"]` — the orchestrator finds it automatically.

### 3. Errors are in-band, not raised

All agents return `A2AResponse(status="failed", ...)` instead of raising exceptions. This keeps the orchestrator's fan-out loop simple — it can always call `response.succeeded` without try/except wrappers around every A2A call.

### 4. Shared MCP Server, individual MCP Clients

All three sub-agents share the **same `MCPServer` instance** but each has its **own `MCPClient`**. This mirrors a real microservice architecture where multiple services connect to a shared database/API gateway, while each service manages its own connection pool and session state.

### 5. AgentCards carry rich metadata

AgentCards include a `metadata` dict for agent-specific configuration:
```python
metadata={
    "max_booking_window_days": 90,
    "supported_doctors": "all",
    "booking_confirmation": "immediate",
}
```
This allows orchestrators to make routing decisions based on capabilities AND non-functional requirements (SLA, rate limits, etc.) without changing the core A2A protocol.

---

## 10. Extending the System

### Add a new Agent (e.g., LabResultsAgent)

1. Create `agents/lab_results_agent.py` with:
   - An `AgentCard` with `capabilities=["get_lab_results"]`
   - A `process_task(task: A2ATask) -> A2AResponse` method
   - An `MCPClient` for any new MCP tools needed

2. Add the new MCP tool to `mcp/mcp_server.py`:
   ```python
   self.register_tool("get_lab_results", "...", _tool_get_lab_results)
   ```

3. Add a data file `data/lab_results.json`

4. In `orchestrator_agent.py.__init__()`, register the new agent:
   ```python
   self._lab_agent = LabResultsAgent(mcp_server=self._mcp_server)
   self._registry.register(self._lab_agent.agent_card)
   ```

5. Add a new request type handler in `OrchestratorAgent.handle_request()`.

**The existing agents require zero changes.** This is the power of A2A's decoupled architecture.

### Replace in-process simulation with real HTTP

To turn this into a real distributed system:
- Each agent runs in its own process/container
- `MCPClient.call_tool()` → `httpx.post(server_url, json={"method": "tools/call", ...})`
- `A2AClient.send_task()` → `httpx.post(agent_card.endpoint, json=task.to_dict())`
- `A2ARegistry` → a dedicated service or DNS-SD/mDNS discovery

All business logic (agent processing, MCP tool implementations) stays exactly the same.

---

## License

This project is provided as an educational demonstration of MCP and A2A protocols. Use freely for learning, teaching, and building upon.

---

*Built with Python stdlib only — no external dependencies required.*