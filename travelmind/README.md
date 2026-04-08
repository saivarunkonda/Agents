# TravelMind 🌍✈️ — Autonomous Travel Booking Agent

> **A complete demonstration of A2A (Agent2Agent Protocol) and AP2 (Agent Payments Protocol) working together in an autonomous multi-agent travel booking system.**

---

## Overview

TravelMind shows what happens when a user says:

> *"Book me a trip to Paris for 3 nights, budget $2000"*

…and a team of autonomous AI agents handles **everything** — flight search, hotel selection, optional car rental, budget enforcement, and secure payment — without the user lifting a finger beyond approving the final cart.

The system demonstrates two cutting-edge agentic protocols:

| Protocol | Role in TravelMind |
|----------|-------------------|
| **A2A** (Agent2Agent) | Agents discover each other via a registry, authenticate with JWT-like tokens, and exchange typed Task/Response messages |
| **AP2** (Agent Payments Protocol) | A two-mandate model (IntentMandate + CartMandate) ensures agents can never overspend or substitute items without a signed audit trail |

---

## Architecture

```
User Request ("Book me a trip to Paris, 3 nights, $2000")
    │
    ▼
OrchestratorAgent
    │
    ├──[AP2]──► Create Intent Mandate (BEFORE any search begins)
    │            mandate_id  : INTENT-A1B2C3D4E5F6
    │            spending_cap: $2000.00  ← hard limit, cryptographically locked
    │            destination : Paris
    │            travel_dates: {outbound: 2025-09-15, return: 2025-09-18}
    │            signature   : sha256(mandate_id|user_id|cap|dest|...) ✓
    │
    ├──[A2A DISCOVERY]──► A2A Registry
    │      finds: FlightAgent, HotelAgent, CarRentalAgent, PaymentAgent
    │      (zero hard-coded references — pure capability-based discovery)
    │
    ├──[A2A AUTH+TASK]──► FlightAgent
    │      token  : eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJvcmNoZXN...
    │      intent : find_flights {dest=Paris, date=2025-09-15}
    │      response: [FL003=$480, FL001=$650, FL002=$820]  ✓
    │
    ├──[A2A AUTH+TASK]──► HotelAgent
    │      token  : eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJvcmNoZXN...
    │      intent : find_hotels {city=Paris, nights=3, min_stars=3}
    │      response: [HT001=4★ $540, HT002=3★ $285]  ✓
    │
    ├──[A2A AUTH+TASK]──► CarRentalAgent  (optional)
    │      token  : eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJvcmNoZXN...
    │      intent : find_cars {city=Paris, days=3}
    │      response: [CR001=$135, CR002=$195]  ✓
    │
    ├──[AP2]──► Create Cart Mandate
    │            items: [FL003=$480.00, HT001=$540.00]
    │            total: $1020.00
    │            verify_mandate_chain() → $1020 < $2000  ✓
    │            signature: sha256(cart_id|items|total|...) ✓
    │
    ├──[User Approval]──► Cart displayed to user
    │            CartMandate.user_approved = True
    │            CartMandate.status        = "approved"
    │
    └──[A2A AUTH+TASK]──► PaymentAgent
           token  : eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJvcmNoZXN...
           intent : process_payment {cart_mandate, intent_mandate}
           │
           ├──[AP2]──► verify_mandate_chain() (independent 2nd check) ✓
           │
           └──[AP2]──► PaymentProcessor.process_payment(cart_mandate)
                        Pre-charge checks (5/5):
                          [PASS] 1/5 user_approved == True          ✓
                          [PASS] 2/5 status == "approved"           ✓
                          [PASS] 3/5 signature integrity            ✓
                          [PASS] 4/5 amount sanity ($0 < $1020 < $50k) ✓
                          [PASS] 5/5 item integrity (2 items valid) ✓
                        Transaction TXN-4F2A1C...: $1020.00 charged ✓
                        IntentMandate INTENT-A1B2C3... → FULFILLED  ✓
```

---

## AP2 Two-Mandate Model

The Agent Payments Protocol introduces a **two-mandate model** that separates *intent* from *execution*:

```
┌─────────────────────────────────────────────────────────────────────┐
│  STEP 1 — IntentMandate  (created FIRST, before any search begins)  │
├─────────────────────────────────────────────────────────────────────┤
│  mandate_id      : INTENT-A1B2C3D4E5F6                              │
│  user_id         : alice@travelmind.example                         │
│  intent          : "Book round trip to Paris, 3 nights, <$2000"     │
│  spending_cap    : $2000.00  ← HARD LIMIT, locked at creation       │
│  destination     : Paris                                            │
│  travel_dates    : {outbound: 2025-09-15, return: 2025-09-18}       │
│  constraints     : {min_hotel_stars: 3}                             │
│  signature       : sha256(id|user|cap|dest|dates|constraints|...)   │
│  status          : active  →  fulfilled  (after successful payment) │
└─────────────────────────────────────────────────────────────────────┘
                            │
            ... agents search for options using A2A ...
                            │
┌─────────────────────────────────────────────────────────────────────┐
│  STEP 2 — CartMandate  (created AFTER concrete options are found)   │
├─────────────────────────────────────────────────────────────────────┤
│  cart_id         : CART-9F8E7D6C5B4A                                │
│  intent_mandate_id: INTENT-A1B2C3D4E5F6   ← links back to intent   │
│  items:                                                             │
│    [0] FLIGHT   Norse Atlantic NYC→Paris $480.00                    │
│    [1] HOTEL    Hotel Le Marais (4★) 3 nights $540.00               │
│  total_amount   : $1020.00                                          │
│  signature      : sha256(cart_id|items|total|created_at|...)        │
│  user_approved  : True  (after user review or delegated mandate)    │
│  status         : pending_approval → approved → charged             │
└─────────────────────────────────────────────────────────────────────┘
                            │
        verify_mandate_chain() checks all of:
          ✓  cart.intent_mandate_id  ==  intent.mandate_id   (link integrity)
          ✓  cart.user_id            ==  intent.user_id      (user consistency)
          ✓  cart.currency           ==  intent.currency     (currency match)
          ✓  cart.total ($1020)      <=  intent.cap ($2000)  (spending cap)
          ✓  destination "paris" found in cart items         (dest. check)
          ✓  intent.status           ==  "active"            (mandate alive)
          ✓  cart signature re-derived matches stored value  (tamper detection)
          ✓  min_hotel_stars constraint satisfied            (constraint check)
```

### Why Two Mandates?

| Concern | How AP2 Solves It |
|---------|------------------|
| Agent overspending | `spending_cap` on the IntentMandate is set **before** any prices are seen; it cannot be raised retroactively |
| Silent item substitution | CartMandate signature covers the full item list; any post-signing change invalidates the signature |
| No audit trail | Both mandates are stored with timestamps and signatures; the full chain from intent → cart → receipt is immutable |
| Autonomous spending safety | The "delegated" approval mode lets agents spend autonomously **within** pre-agreed constraints — the AP2 contract still enforces the cap |

---

## A2A Protocol Flow

The A2A (Agent2Agent) protocol governs how agents discover, authenticate with, and communicate with each other.

```
Caller (OrchestratorAgent)                    Server (e.g. FlightAgent)
        │                                              │
        │  1. DISCOVERY                                │
        │  ─────────────────────────────────────────► │
        │  get_registry().discover("find_flights")     │
        │  returns: FlightAgent's AgentCard            │
        │                                              │
        │  2. AUTHENTICATION                           │
        │  generate_token(caller_id)                   │
        │  → eyJhbGciOiJIUzI1NiJ9.eyJzdWI...         │
        │  verify_token(token, caller_id) → True       │
        │                                              │
        │  3. TASK DISPATCH                            │
        │  ─────────────────────────────────────────► │
        │  A2ATask {                                   │
        │    task_id    : TASK-4A2B1C3D5E              │
        │    sender_id  : orchestrator-agent-01        │
        │    receiver_id: flight-agent-01              │
        │    intent     : "find_flights"               │
        │    payload    : {dest: Paris, date: ...}     │
        │    auth_token : eyJhbGciOiJIUzI1NiJ9...     │
        │    status     : pending → processing         │
        │  }                                           │
        │                                              │
        │  4. RESPONSE                                 │
        │  ◄───────────────────────────────────────── │
        │  A2AResponse {                               │
        │    task_id    : TASK-4A2B1C3D5E              │
        │    agent_id   : flight-agent-01              │
        │    status     : "success"                    │
        │    result     : {flights: [...]}             │
        │    message    : "Found 3 flight(s)..."       │
        │    duration_ms: 12.4                         │
        │  }                                           │
```

### AgentCard — Agent Identity and Discovery

Every agent publishes an `AgentCard` to the `A2ARegistry` on startup:

```python
AgentCard(
    agent_id        = "flight-agent-01",
    name            = "FlightAgent",
    description     = "Searches and books flights via A2A",
    capabilities    = ["find_flights", "book_flight"],
    endpoint        = "https://agents.travelmind.internal/flight/v1",
    supported_tasks = ["find_flights", "book_flight"],
    auth_required   = True,
    version         = "1.0.0",
    metadata        = {"instance": <FlightAgent object>}
)
```

The OrchestratorAgent discovers agents by capability string — no hard-coded references:

```python
card = registry.discover_one("find_flights")  # returns FlightAgent's card
```

---

## Directory Structure

```
project3_travelmind/
│
├── README.md                       ← You are here
├── requirements.txt                ← No external dependencies (stdlib only)
├── main.py                         ← Entry point — runs 3 demo scenarios
│
├── a2a/                            ← Agent2Agent Protocol implementation
│   ├── __init__.py
│   ├── agent_card.py               ← AgentCard dataclass (identity + capabilities)
│   ├── a2a_protocol.py             ← A2ATask, A2AResponse, A2ARegistry, A2AClient
│   └── auth.py                     ← Simulated JWT token generation + verification
│
├── ap2/                            ← Agent Payments Protocol implementation
│   ├── __init__.py
│   ├── mandate.py                  ← IntentMandate + CartMandate dataclasses
│   ├── ap2_protocol.py             ← Mandate creation, signing, chain verification
│   └── payment_processor.py        ← 5-check pre-charge verification + charge sim
│
├── agents/                         ← All agent implementations
│   ├── __init__.py
│   ├── orchestrator_agent.py       ← Master coordinator (A2A caller + AP2 client)
│   ├── flight_agent.py             ← A2A server: find_flights + book_flight
│   ├── hotel_agent.py              ← A2A server: find_hotels + book_hotel
│   ├── car_rental_agent.py         ← A2A server: find_cars + rent_car
│   └── payment_agent.py            ← A2A server + AP2 bridge: process_payment
│
└── data/                           ← Mock travel inventory (JSON)
    ├── flights.json                ← 5 flight records (Paris + London routes)
    ├── hotels.json                 ← 5 hotel records (Paris + London)
    └── cars.json                   ← 3 car rental records (Paris + London)
```

---

## Demo Scenarios

### Scenario 1 — Interactive User Purchase (Paris, $2000)

```
User: "Book me a trip to Paris, 3 nights, budget $2000, September 15th"

Flow:
  [AP2]  Intent Mandate created — spending_cap=$2000, dest=Paris
  [A2A]  Discover FlightAgent, HotelAgent, PaymentAgent
  [A2A]  FlightAgent → [FL003 $480, FL001 $650, FL002 $820]
  [A2A]  HotelAgent  → [HT001 4★ $540, HT002 3★ $285]
  [ORCH] Select: FL003($480) + HT001($540) = $1020 ✓ under $2000
  [AP2]  Cart Mandate created — total=$1020
  [AP2]  verify_mandate_chain() — $1020 <= $2000 ✓
  [USER] Cart displayed, user approves
  [A2A]  PaymentAgent processes payment
  [AP2]  TXN-XXXX: $1020 charged ✓
  [AP2]  IntentMandate → FULFILLED

Result: ✓ SUCCESS  |  Cost: $1020.00  |  Headroom: $980.00
```

### Scenario 2 — Delegated / Autonomous Purchase (London + Car, $1500)

```
Corporate travel manager pre-authorises a London trip via AP2 Intent Mandate.
Agent books completely autonomously — no live user interaction.

Flow:
  [AP2]  Intent Mandate created — spending_cap=$1500, dest=London, delegated=True
  [A2A]  Discover FlightAgent, HotelAgent, CarRentalAgent, PaymentAgent
  [A2A]  FlightAgent   → [FL005 $380, FL004 $420]
  [A2A]  HotelAgent    → [HT004 3★ $220]
  [A2A]  CarRentalAgent → [CR003 Economy $80]
  [ORCH] Select: FL005($380) + HT004($220) + CR003($80) = $680 ✓ under $1500
  [AP2]  Cart Mandate created — total=$680
  [AP2]  verify_mandate_chain() — $680 <= $1500 ✓
  [AP2]  user_approve_cart(method="delegated") — pre-authorised ✓
  [A2A]  PaymentAgent processes payment autonomously
  [AP2]  TXN-XXXX: $680 charged ✓

Result: ✓ SUCCESS  |  Cost: $680.00  |  Headroom: $820.00
        Agent acted within mandate constraints — no human in the loop
```

### Scenario 3 — Budget Exceeded / AP2 Mandate Blocks Overspend (Paris, $700)

```
User requests Paris trip with only $700 budget.
Cheapest available combination: $480 flight + $285 hotel = $765 > $700.

Flow:
  [AP2]  Intent Mandate created — spending_cap=$700, dest=Paris
  [A2A]  FlightAgent → [FL003 $480, FL001 $650, FL002 $820]
  [A2A]  HotelAgent  → [HT001 4★ $540, HT002 3★ $285]
  [ORCH] Try FL003($480) + HT001($540) = $1020  EXCEEDS $700 ✗
  [ORCH] Try FL003($480) + HT002($285) = $765   EXCEEDS $700 ✗
  [ORCH] Try FL001($650) + HT002($285) = $935   EXCEEDS $700 ✗
  [ORCH] All combinations exhausted — minimum possible = $765
  [ORCH] Refusing to create CartMandate that would violate IntentMandate cap

Result: ✗ FAILED
        Error: No flight + hotel combination fits within the $700 budget.
               Cheapest available = $765 ($480 flight + $285 hotel).
        No payment attempted. AP2 mandate enforced correctly. ✓
```

---

## Running the Demo

### Prerequisites

- Python 3.8 or newer
- No external packages required (pure stdlib)

### Quick Start

```bash
cd Agent/project3_travelmind
python main.py
```

The demo will prompt you to press Enter between scenarios. To run non-interactively (e.g. in a pipe or CI):

```bash
echo "" | python main.py           # auto-accept all prompts (Unix)
python main.py < /dev/null          # same effect
```

### Running Individual Scenarios

```python
import sys
sys.path.insert(0, ".")            # run from project3_travelmind/

from main import run_scenario_1, run_scenario_2, run_scenario_3

result = run_scenario_1()          # Interactive Paris booking
result = run_scenario_2()          # Delegated London + car booking
result = run_scenario_3()          # Budget exceeded rejection
```

### Expected Output Structure

Each scenario prints:

1. **Agent startup** — agents load data files and register in the A2A registry
2. **[AP2] Intent Mandate** — mandate creation with spending cap and signature
3. **[A2A] Discovery** — registry lookup for each required capability
4. **[A2A] Auth + Task** — token generation, task dispatch, agent response
5. **[AP2] Cart Mandate** — item assembly, total computation, signature
6. **[AP2] Verify Chain** — 7–8 check-by-check verification log
7. **[USER] Approval** — cart display and approval (interactive/delegated)
8. **[A2A] Payment** — PaymentAgent dispatch with pre-charge checks
9. **[AP2] Receipt** — transaction ID, receipt ID, mandate FULFILLED
10. **Summary box** — ASCII table with all booking references

---

## Module Reference

### `a2a/agent_card.py` — `AgentCard`

```python
@dataclass
class AgentCard:
    agent_id        : str          # unique agent identifier
    name            : str          # human-readable name
    description     : str          # what this agent does
    capabilities    : List[str]    # e.g. ["find_flights", "book_flight"]
    endpoint        : str          # simulated HTTPS URL
    supported_tasks : List[str]    # task intent strings handled
    auth_required   : bool = True  # whether tokens are checked
    version         : str = "1.0.0"
    metadata        : dict = {}    # {"instance": <agent object>}
```

### `a2a/auth.py` — Token Functions

| Function | Description |
|----------|-------------|
| `generate_token(agent_id, ttl=3600)` | Creates a fake JWT-like bearer token, stores it in the in-memory token store |
| `verify_token(token, agent_id)` | Returns `True` if the token exists, is unrevoked, unexpired, and belongs to `agent_id` |
| `revoke_token(token)` | Marks a token as revoked in the store |
| `get_token_info(token)` | Returns metadata dict including time remaining |
| `list_active_tokens()` | Returns all unexpired, unrevoked tokens (admin/debug) |

### `a2a/a2a_protocol.py` — Core A2A Classes

**`A2ATask`** — Structured task message:

```python
@dataclass
class A2ATask:
    sender_id   : str
    receiver_id : str
    intent      : str        # e.g. "find_flights"
    payload     : dict       # task parameters
    auth_token  : str        # bearer token
    task_id     : str        # auto-generated TASK-XXXXXXXXXX
    status      : str        # pending → processing → completed/failed
```

**`A2AResponse`** — Structured response:

```python
@dataclass
class A2AResponse:
    task_id     : str
    agent_id    : str
    status      : str        # "success" | "error" | "streaming"
    result      : Any        # domain-specific result payload
    message     : str        # human-readable summary
    duration_ms : float      # processing time
```

**`A2ARegistry`** — Discovery service:

```python
registry = get_registry()             # shared singleton
registry.register(agent_card)         # agent self-registers on startup
registry.discover("find_flights")     # → [FlightAgent card]
registry.discover_one("find_hotels")  # → HotelAgent card (or None)
```

**`A2AClient`** — Full lifecycle client:

```python
client = A2AClient(caller_id="orchestrator-agent-01")
response = client.send_task(
    capability = "find_flights",    # used for registry discovery
    intent     = "find_flights",    # forwarded in the A2ATask
    payload    = {"destination": "Paris", "date": "2025-09-15"},
)
```
`send_task()` handles: discovery → auth token → task dispatch → response logging.

### `ap2/mandate.py` — `IntentMandate` and `CartMandate`

See the **AP2 Two-Mandate Model** section above for full field descriptions.

Status lifecycle:

```
IntentMandate: active → fulfilled | expired | cancelled
CartMandate:   pending_approval → approved → charged | failed | cancelled
```

### `ap2/ap2_protocol.py` — `AP2Protocol`

| Method | Description |
|--------|-------------|
| `create_intent_mandate(user_id, intent, cap, dest, dates, constraints)` | Creates and signs an `IntentMandate`; must be called before any A2A searches |
| `create_cart_mandate(intent_mandate, selected_items)` | Packages selected travel items into a signed `CartMandate` linked to the intent |
| `verify_mandate_chain(cart, intent)` | Runs 7–8 checks; returns `True` only if cart fully satisfies all intent constraints |
| `sign_mandate(mandate)` | SHA-256 signature over key fields + signing secret |
| `user_approve_cart(cart, method)` | Advances cart from `pending_approval` → `approved`; records `approval_method` |
| `mark_intent_fulfilled(mandate_id)` | Advances intent to `fulfilled` after successful payment |
| `mark_cart_charged(cart_id)` | Advances cart to `charged` after successful payment |

### `ap2/payment_processor.py` — `PaymentProcessor`

`process_payment(cart_mandate)` runs five pre-charge verification checks:

| Check | Description |
|-------|-------------|
| 1. User approval gate | `cart_mandate.user_approved` must be `True` |
| 2. Status gate | `cart_mandate.status` must be `"approved"` |
| 3. Signature integrity | Cart signature is re-derived and compared to detect tampering |
| 4. Amount sanity | `0 < total_amount < $50,000` hard ceiling |
| 5. Item integrity | Every item must have `type`, `id`, and non-negative `price` |

On success: returns receipt dict with `transaction_id`, `receipt_id`, `items_charged`, `charged_at`.

### Agents

| Agent | Role | Capabilities |
|-------|------|-------------|
| `OrchestratorAgent` | Master coordinator (A2A caller, AP2 client) | N/A — not a server |
| `FlightAgent` | A2A server, searches/books flights | `find_flights`, `book_flight` |
| `HotelAgent` | A2A server, searches/books hotels | `find_hotels`, `book_hotel` |
| `CarRentalAgent` | A2A server, searches/rents cars | `find_cars`, `rent_car` |
| `PaymentAgent` | A2A server + AP2 bridge | `process_payment`, `create_mandate`, `verify_mandate` |

---

## Mock Data

### Flights (`data/flights.json`)

| ID | Route | Date | Price | Airline | Duration | Seats |
|----|-------|------|-------|---------|----------|-------|
| FL001 | NYC → Paris | 2025-09-15 | $650 | Air France | 7h 30m | 12 |
| FL002 | NYC → Paris | 2025-09-15 | $820 | Delta | 8h 00m | 5 |
| FL003 | NYC → Paris | 2025-09-15 | $480 | Norse Atlantic | 8h 45m | 30 |
| FL004 | NYC → London | 2025-09-15 | $420 | British Airways | 6h 50m | 8 |
| FL005 | NYC → London | 2025-09-15 | $380 | Virgin Atlantic | 7h 10m | 20 |

### Hotels (`data/hotels.json`)

| ID | City | Name | Stars | /Night | Available | Amenities |
|----|------|------|-------|--------|-----------|-----------|
| HT001 | Paris | Hotel Le Marais | 4★ | $180 | ✓ | wifi, breakfast, gym |
| HT002 | Paris | Ibis Paris Centre | 3★ | $95 | ✓ | wifi |
| HT003 | Paris | The Ritz Paris | 5★ | $850 | ✗ | wifi, spa, pool |
| HT004 | London | Premier Inn London | 3★ | $110 | ✓ | wifi, breakfast |
| HT005 | London | The Savoy | 5★ | $650 | ✓ | wifi, spa, pool |

### Cars (`data/cars.json`)

| ID | City | Type | Company | /Day | Available |
|----|------|------|---------|------|-----------|
| CR001 | Paris | Economy | Europcar | $45 | ✓ |
| CR002 | Paris | Compact | Hertz | $65 | ✓ |
| CR003 | London | Economy | Enterprise | $40 | ✓ |

---

## Key Design Decisions

### Why AP2 Mandate is Created First

The IntentMandate is created **before** the OrchestratorAgent contacts any specialist agent. This is the cornerstone of the AP2 security model:

- Prices are unknown at mandate creation time → the spending cap cannot be inflated retroactively to accommodate expensive options that were already found
- The signature covers the cap value → any attempt to raise the cap after creation would invalidate the signature
- Agents cannot "discover" a $3000 flight and then request the user approve a higher cap — the cap is locked

### Simulated vs Real Cryptography

In this demo, "JWT tokens" and "cryptographic signatures" are simulated using Python's `hashlib.sha256` and `base64`. The structural patterns (token: header.payload.signature, mandate signature over canonical field concatenation) mirror real JWT / ECDSA workflows, but no actual asymmetric keys are used. In a production system:

- A2A auth tokens would be real JWTs signed with the caller's RSA/EC private key
- AP2 mandate signatures would use the user's hardware-backed private key (e.g. passkey or HSM)

### In-Process A2A Communication

All agent communication happens **in-process** (Python method calls). The `endpoint` URLs on AgentCards are illustrative only. In a real deployment, `A2AClient.send_task()` would make an HTTPS POST to `agent_card.endpoint` with the `A2ATask` serialised as JSON, and receive an `A2AResponse` in return.

### Hotel Ranking by Value Score

Hotels are ranked by `stars / price_per_night` rather than simply cheapest-first. This means the OrchestratorAgent will prefer a 4★ hotel at $180/night (score = 0.0222) over a 3★ at $95/night (score = 0.0316)... actually 3★/$95 scores higher, which is correct — better value per dollar. The budget-selection loop then picks the cheapest flight + highest-value hotel that fits within the cap.

---

## Log Line Reference

All output is prefixed so you can filter by protocol layer:

| Prefix | Source | Example |
|--------|--------|---------|
| `[AP2]` | AP2Protocol / PaymentProcessor | `[AP2] INTENT MANDATE: Creating purchase intent mandate` |
| `[AP2] PAYMENT` | PaymentProcessor | `[AP2] PAYMENT SUCCESS ✓` |
| `[A2A]` | A2AClient | `[A2A] DISCOVERY: Finding agent for capability: 'find_flights'` |
| `[A2A] AUTH` | A2AClient | `[A2A] AUTH: Token issued for 'orchestrator-agent-01' — preview: eyJhbGc...` |
| `[A2A] TASK` | A2AClient | `[A2A] TASK: Sending task 'TASK-4A2B1C' to 'FlightAgent'` |
| `[A2A] RESPONSE` | A2AClient | `[A2A] RESPONSE: Received from 'FlightAgent' [task=TASK-4A2B1C] status='success' ✓` |
| `[A2A] REGISTRY` | A2ARegistry | `[A2A] REGISTRY: Registered agent 'FlightAgent'` |
| `[ORCHESTRATOR]` | OrchestratorAgent | `[ORCHESTRATOR] Selected combination:` |
| `[FLIGHT AGENT]` | FlightAgent | `[FLIGHT AGENT] Found 3 matching flight(s)` |
| `[HOTEL AGENT]` | HotelAgent | `[HOTEL AGENT] Booking CONFIRMED ✓` |
| `[CAR RENTAL AGENT]` | CarRentalAgent | `[CAR RENTAL AGENT] Found 2 matching car(s)` |
| `[PAYMENT AGENT]` | PaymentAgent | `[PAYMENT AGENT] Mandate chain verified ✓` |

---

## Extending TravelMind

### Adding a New Agent

1. Create `agents/my_agent.py` following the pattern of `flight_agent.py`
2. Define `CAPABILITIES`, `SUPPORTED_TASKS`, `AGENT_ID`, and `ENDPOINT`
3. Implement `process_task(task: A2ATask) -> A2AResponse`
4. Instantiate the agent before calling `orchestrator.plan_trip()` — it self-registers

```python
class InsuranceAgent:
    CAPABILITIES    = ["find_insurance", "buy_insurance"]
    SUPPORTED_TASKS = ["find_insurance", "buy_insurance"]

    def __init__(self):
        self.agent_card = AgentCard(
            agent_id="insurance-agent-01",
            name="InsuranceAgent",
            ...
            metadata={"instance": self},
        )
        get_registry().register(self.agent_card)

    def process_task(self, task: A2ATask) -> A2AResponse:
        if task.intent == "find_insurance":
            return self._handle_find_insurance(task)
        ...
```

### Adding AP2 Constraints

Pass additional constraints in the `constraints` dict when calling `plan_trip()`:

```python
orchestrator.plan_trip(
    ...,
    constraints={
        "min_hotel_stars": 4,
        "max_items": 3,            # AP2 guards against cart bloat
        "include_car_rental": True,
    }
)
```

The `verify_mandate_chain()` method checks `min_hotel_stars` and `max_items` automatically.

### Using Real HTTP

To make the A2A protocol actually network-based, replace the in-process dispatch in `A2AClient.send_task()`:

```python
# Replace this:
response = agent_instance.process_task(task)

# With this (pseudo-code):
import urllib.request, json
req_body = json.dumps(task.to_dict()).encode()
req = urllib.request.Request(
    agent_card.endpoint,
    data=req_body,
    headers={"Authorization": f"Bearer {auth_token}",
             "Content-Type": "application/json"},
)
with urllib.request.urlopen(req) as resp:
    data = json.loads(resp.read())
    response = A2AResponse(**data)
```

---

## License

MIT — free to use, modify, and distribute.

---

*TravelMind is a demonstration project. No real flights, hotels, or payments are processed.*
```

Now let me save the README and run the project to verify everything works: