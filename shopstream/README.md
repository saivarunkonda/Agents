# 🛍️ ShopStream — AI Shopping Assistant

> A real-time streaming shopping assistant that demonstrates the **AG-UI** and **MCP** protocols working together over **Server-Sent Events**.

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Protocol Deep-Dives](#protocol-deep-dives)
   - [MCP — Model Context Protocol](#mcp--model-context-protocol)
   - [AG-UI — Agent-User Interface Protocol](#ag-ui--agent-user-interface-protocol)
   - [SSE — Server-Sent Events Transport](#sse--server-sent-events-transport)
4. [Directory Structure](#directory-structure)
5. [Quick Start](#quick-start)
6. [API Reference](#api-reference)
7. [AG-UI Event Lifecycle](#ag-ui-event-lifecycle)
8. [MCP Tool Registry](#mcp-tool-registry)
9. [Data Model](#data-model)
10. [Educational Notes](#educational-notes)

---

## Overview

ShopStream lets a user type a natural-language shopping query and immediately see the AI agent's work stream live to the browser: which tools it calls, how the product list builds up, and the final recommendation — all word-by-word as it is generated.

| Feature | Technology |
|---|---|
| Real-time event streaming | AG-UI Protocol over SSE |
| Tool invocation | MCP (Model Context Protocol) |
| Web framework | FastAPI + uvicorn |
| Frontend | Vanilla JS + EventSource API |
| Product data | Mock JSON catalog (18 products) |

**No LLM API key required.** All agent logic is deterministic Python, so you can study the protocol mechanics without any external dependencies.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                          User's Browser                             │
│                                                                     │
│   ┌───────────────────┐        ┌──────────────────────────────┐    │
│   │   Search Form     │        │  AG-UI Event Handlers        │    │
│   │  (user_id, query) │        │  onmessage(sseEvent) {       │    │
│   └────────┬──────────┘        │    switch(agEvent.type) {    │    │
│            │ click             │      RUN_STARTED  → UI reset │    │
│            ▼                   │      TOOL_CALL_*  → tool log │    │
│   new EventSource("/stream")   │      STATE_DELTA  → products │    │
│            │                   │      TEXT_MESSAGE → response │    │
│            │ SSE connection     │      RUN_FINISHED → done     │    │
└────────────┼───────────────────┴──────────────────┬────────────────┘
             │                                       │
             │ HTTP GET /stream                      │ "data: {...}\n\n"
             │ ?user_id=user123                      │  (AG-UI events)
             │ &query=wireless+headphones            │
             ▼                                       │
┌────────────────────────────────────────────────────┴───────────────┐
│                    FastAPI Server  (backend/main.py)                │
│                                                                     │
│   GET /stream → StreamingResponse(event_generator(), text/event-stream)
│                                   │                                 │
│                         async for sse_chunk:                        │
│                              yield chunk  ──────────────────────────┘
│                                   │
│                                   ▼
│              ┌────────────────────────────────────┐                 │
│              │        ShoppingAgent               │                 │
│              │   (backend/agents/shopping_agent.py)│                 │
│              │                                    │                 │
│              │  async def process_query():        │                 │
│              │    yield RUN_STARTED               │                 │
│              │    yield STATE_SNAPSHOT            │                 │
│              │    yield TEXT_MESSAGE_START        │                 │
│              │    yield TEXT_MESSAGE_CONTENT×N    │                 │
│              │    ┌── MCP tool call ──────────┐   │                 │
│              │    │ yield TOOL_CALL_START      │   │                 │
│              │    │   mcp_client.call_tool()   │   │                 │
│              │    │ yield TOOL_CALL_END        │   │                 │
│              │    └────────────────────────────┘   │                 │
│              │    yield STATE_DELTA (products)     │                 │
│              │    yield STATE_DELTA (recs)         │                 │
│              │    yield TEXT_MESSAGE_CONTENT×N     │                 │
│              │    yield TEXT_MESSAGE_END           │                 │
│              │    yield RUN_FINISHED               │                 │
│              └──────────────┬─────────────────────┘                 │
│                             │ mcp_client.call_tool(name, **kwargs)   │
│                             ▼                                        │
│              ┌────────────────────────────────────┐                 │
│              │         MCPClient                  │                 │
│              │  (backend/mcp/mcp_client.py)       │                 │
│              │                                    │                 │
│              │  session_id = uuid4()              │                 │
│              │  [MCP Session:abc12345] → CALL     │                 │
│              │  [MCP Session:abc12345] ← OK 42ms  │                 │
│              └──────────────┬─────────────────────┘                 │
│                             │ server.call_tool(name, **kwargs)       │
│                             ▼                                        │
│              ┌────────────────────────────────────────────────────┐ │
│              │               MCPServer                            │ │
│              │        (backend/mcp/mcp_server.py)                 │ │
│              │                                                    │ │
│              │  Tool Registry:                                    │ │
│              │  ┌──────────────────────┬─────────────────────┐   │ │
│              │  │ search_products      │ query, category,    │   │ │
│              │  │                      │ max_price           │   │ │
│              │  ├──────────────────────┼─────────────────────┤   │ │
│              │  │ get_product_details  │ product_id          │   │ │
│              │  ├──────────────────────┼─────────────────────┤   │ │
│              │  │ check_inventory      │ product_id          │   │ │
│              │  ├──────────────────────┼─────────────────────┤   │ │
│              │  │ get_user_preferences │ user_id             │   │ │
│              │  ├──────────────────────┼─────────────────────┤   │ │
│              │  │ compare_products     │ product_ids[]       │   │ │
│              │  ├──────────────────────┼─────────────────────┤   │ │
│              │  │ get_recommendations  │ user_id, query      │   │ │
│              │  └──────────────────────┴─────────────────────┘   │ │
│              │                         │                          │ │
│              │                         ▼                          │ │
│              │              data/products.json                    │ │
│              │              data/user_preferences.json            │ │
│              └────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Protocol Deep-Dives

### MCP — Model Context Protocol

MCP is an open standard that lets AI agents **discover and invoke typed tools** through a well-defined interface. Think of it as an "OpenAPI for agent tools."

#### Key concepts in ShopStream

**1. Tool Registration**

Every capability the agent can use is registered with:
- `name` — unique snake_case identifier (e.g. `search_products`)
- `description` — human-readable summary for the LLM / agent planner
- `parameters` — JSON-Schema dict describing required and optional inputs
- `handler` — the Python function that executes the tool

```python
# backend/mcp/mcp_server.py
self._register(
    name="search_products",
    description="Search the product catalog using a free-text query...",
    parameters={
        "type": "object",
        "properties": {
            "query":     {"type": "string",  "description": "Free-text search term"},
            "category":  {"type": "string",  "enum": ["Electronics", "Clothing", "Home"]},
            "max_price": {"type": "number",  "description": "Max price in USD"},
        },
        "required": ["query"],
    },
    handler=self._tool_search_products,
)
```

**2. Tool Invocation**

The agent calls tools through the `MCPClient`, which logs every call with its session ID:

```
[MCP Session:abc12345] → CALL  [search_products]  args={query='wireless headphones'}
[MCP Session:abc12345] ← OK    [search_products]  43.2 ms
```

Every call returns a structured result envelope:

```json
{
  "tool": "search_products",
  "success": true,
  "result": {
    "query": "wireless headphones",
    "total_found": 3,
    "products": [ ... ]
  }
}
```

**3. Session Management**

`MCPClient` generates a `session_id` (UUID) per query run. In production MCP, this ID is exchanged during the `initialize` handshake. Here it's used to correlate log lines across concurrent requests.

**4. Tool Discovery**

`MCPServer.list_tools()` mirrors the MCP `tools/list` operation — it returns serialisable advertisement dicts for every registered tool (without the handler reference).

---

### AG-UI — Agent-User Interface Protocol

AG-UI is an open, lightweight protocol that standardises **how AI agents communicate with frontend UIs in real time**. Instead of one big HTTP response, the agent emits a *stream* of typed events that flow to the browser over SSE.

#### Event Types

| Event Type | Group | Purpose |
|---|---|---|
| `RUN_STARTED` | Lifecycle | A new agent run has begun |
| `RUN_FINISHED` | Lifecycle | The agent run has completed |
| `TEXT_MESSAGE_START` | Text | Opens a new streaming message container |
| `TEXT_MESSAGE_CONTENT` | Text | One token/word chunk to append |
| `TEXT_MESSAGE_END` | Text | Streaming message is complete |
| `TOOL_CALL_START` | Tools | Agent is about to invoke an MCP tool |
| `TOOL_CALL_END` | Tools | MCP tool returned a result |
| `STATE_SNAPSHOT` | State | Replace entire shared state (used at run start) |
| `STATE_DELTA` | State | Patch shared state (used incrementally) |
| `CUSTOM` | Escape | Application-specific events |

#### Event Envelope

Every AG-UI event uses the same outer envelope:

```json
{
  "type":      "TOOL_CALL_START",
  "timestamp": "2024-01-15T10:30:00.123Z",
  "run_id":    "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "data": {
    "tool_call_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
    "tool_name":    "search_products",
    "tool_args":    { "query": "wireless headphones" }
  }
}
```

#### Shared State

AG-UI defines a **shared state** object that both the agent and the UI maintain in sync:

- `STATE_SNAPSHOT` → replace the entire state (sent once at run start)
- `STATE_DELTA` → apply a shallow patch to specific keys

The JavaScript `agentState` object in `app.js` mirrors whatever the Python agent publishes. This is the key mechanism that drives product card rendering — when the agent calls `search_products`, it emits a `STATE_DELTA` with the new products list, and the browser immediately renders the cards.

#### Text Streaming Pattern

The three text events work together like a cursor:

```
TEXT_MESSAGE_START   → create message container, show cursor
TEXT_MESSAGE_CONTENT → append "Let " to container
TEXT_MESSAGE_CONTENT → append "me " to container
TEXT_MESSAGE_CONTENT → append "find " to container
... (one event per word)
TEXT_MESSAGE_END     → hide cursor, mark message complete
```

This is identical to how a real LLM streaming integration would work — the only difference is that here we split on whitespace rather than receiving model tokens.

---

### SSE — Server-Sent Events Transport

AG-UI rides on top of the W3C **Server-Sent Events** standard, which is built into every modern browser via the `EventSource` API.

#### Wire format

Each AG-UI event is encoded as a single SSE message:

```
data: {"type":"STATE_DELTA","timestamp":"2024-01-15T10:30:01.456Z","data":{"patch":{"products":[...]}}}\n\n
```

Rules:
- Lines starting with `data:` carry the payload
- A **blank line** (`\n\n`) terminates each message
- The browser's `EventSource` parses this framing automatically

#### Required HTTP headers

```
Content-Type:      text/event-stream
Cache-Control:     no-cache
X-Accel-Buffering: no        ← disables nginx proxy buffering
Connection:        keep-alive
```

#### Browser-side consumption

```javascript
// app.js — opening the AG-UI stream
const es = new EventSource('/stream?user_id=user123&query=wireless+headphones');

es.addEventListener('message', (sseEvent) => {
    const agEvent = JSON.parse(sseEvent.data);   // parse the AG-UI envelope
    switch (agEvent.type) {
        case 'RUN_STARTED':         handleRunStarted(agEvent);      break;
        case 'TEXT_MESSAGE_CONTENT': handleTextMessageContent(agEvent); break;
        case 'TOOL_CALL_START':     handleToolCallStart(agEvent);   break;
        case 'STATE_DELTA':         handleStateDelta(agEvent);      break;
        case 'RUN_FINISHED':        handleRunFinished(agEvent);     break;
        // ... etc.
    }
});
```

Because `EventSource` handles reconnection automatically, the client is resilient to brief network hiccups — though for one-shot agent runs you should close the connection explicitly on `RUN_FINISHED`.

---

## Directory Structure

```
project2_shopstream/
├── README.md                        ← you are here
├── requirements.txt                 ← fastapi, uvicorn[standard]
│
├── backend/
│   ├── __init__.py
│   ├── main.py                      ← FastAPI app, SSE /stream endpoint
│   │
│   ├── mcp/
│   │   ├── __init__.py
│   │   ├── mcp_server.py            ← Tool registry + 6 shopping tools
│   │   └── mcp_client.py            ← Session manager + call logger
│   │
│   ├── agui/
│   │   ├── __init__.py
│   │   └── agui_protocol.py         ← Event types, envelope, SSE formatter
│   │                                   + helper factories (make_run_started, etc.)
│   └── agents/
│       ├── __init__.py
│       └── shopping_agent.py        ← Async generator pipeline
│                                       (yields SSE-formatted AG-UI events)
│
├── frontend/
│   ├── index.html                   ← Single-page UI
│   ├── style.css                    ← Modern white/blue design, animations
│   └── app.js                       ← EventSource consumer + AG-UI dispatcher
│
└── data/
    ├── products.json                ← 18 products (Electronics, Clothing, Home)
    └── user_preferences.json        ← 3 user profiles with budgets + brand prefs
```

---

## Quick Start

### 1. Install dependencies

```bash
cd Agent/project2_shopstream
pip install -r requirements.txt
```

### 2. Start the server

```bash
uvicorn backend.main:app --reload --port 8000
```

You should see:

```
[ShopStream] Initialised — MCP server ready with tools:
  • search_products: Search the product catalog using a free-text query...
  • get_product_details: Retrieve the full details of a single product...
  • check_inventory: Check the current stock level and availability...
  • get_user_preferences: Fetch a user's saved preferences, budget...
  • compare_products: Generate a side-by-side comparison of products...
  • get_recommendations: Return the top 3 recommended products...
INFO:     Uvicorn running on http://0.0.0.0:8000
```

### 3. Open the browser

Navigate to **http://localhost:8000**

### 4. Try a search

- Select a shopper profile (Alex, Jordan, or Sam)
- Type a query like `wireless headphones` or click a quick-chip
- Press **Search** and watch the AG-UI events stream in real time

### Alternative — inspect raw SSE

Open a new browser tab and visit:

```
http://localhost:8000/stream?user_id=user123&query=wireless+headphones
```

You will see the raw `data: {...}\n\n` SSE messages arrive one by one — this is the exact wire format `EventSource` parses in `app.js`.

---

## API Reference

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Serves `frontend/index.html` |
| `GET` | `/stream` | AG-UI SSE stream — main endpoint |
| `GET` | `/health` | Liveness check + MCP tool list |
| `GET` | `/api/products` | Full product catalogue (JSON) |
| `GET` | `/api/users` | Available user IDs and names |
| `GET` | `/api/tools` | MCP tool registry (schemas) |
| `GET` | `/static/*` | Static frontend assets |

### `/stream` parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `user_id` | string | No | `user123` | Shopper profile to load |
| `query` | string | **Yes** | — | Free-text shopping query |

---

## AG-UI Event Lifecycle

Below is the complete sequence of AG-UI events emitted for a single `process_query()` call:

```
Phase 1 — Run Startup
─────────────────────
→ RUN_STARTED          { run_id, metadata: { user_id, query, agent } }
→ STATE_SNAPSHOT       { snapshot: { status:"started", products:[], query, ... } }

Phase 2 — Opening Message
──────────────────────────
→ TEXT_MESSAGE_START   { message_id, role:"assistant" }
→ TEXT_MESSAGE_CONTENT { message_id, delta:"Let " }
→ TEXT_MESSAGE_CONTENT { message_id, delta:"me " }
   ... (word by word)

Phase 3 — search_products MCP call
────────────────────────────────────
→ TOOL_CALL_START      { tool_call_id, tool_name:"search_products",
                          tool_args: { query } }
   [MCPServer executes search_products handler]
→ TOOL_CALL_END        { tool_call_id, tool_name:"search_products",
                          success:true, result:{...}, summary:"Found 3 products" }
→ STATE_DELTA          { patch: { products:[...], status:"searching" } }

Phase 4 — get_user_preferences MCP call
─────────────────────────────────────────
→ TOOL_CALL_START      { tool_call_id, tool_name:"get_user_preferences",
                          tool_args: { user_id } }
   [MCPServer executes get_user_preferences handler]
→ TOOL_CALL_END        { tool_call_id, tool_name:"get_user_preferences", ... }
→ STATE_DELTA          { patch: { status:"personalising", budget_max:300 } }

Phase 5 — get_recommendations MCP call
────────────────────────────────────────
→ TOOL_CALL_START      { tool_call_id, tool_name:"get_recommendations",
                          tool_args: { user_id, query } }
   [MCPServer scores all products against user prefs]
→ TOOL_CALL_END        { tool_call_id, tool_name:"get_recommendations", ... }
→ STATE_DELTA          { patch: { recommendations:[...], status:"comparing" } }

Phase 6 — compare_products MCP call (top 2)
─────────────────────────────────────────────
→ TOOL_CALL_START      { tool_call_id, tool_name:"compare_products",
                          tool_args: { product_ids:["E001","E003"] } }
   [MCPServer builds side-by-side comparison]
→ TOOL_CALL_END        { tool_call_id, tool_name:"compare_products", ... }
→ STATE_DELTA          { patch: { comparison:{...} } }

Phase 7 — Streaming Recommendation Text
─────────────────────────────────────────
→ STATE_DELTA          { patch: { status:"responding" } }
→ TEXT_MESSAGE_CONTENT { message_id, delta:"I " }
→ TEXT_MESSAGE_CONTENT { message_id, delta:"found " }
   ... (full recommendation, word by word)
→ TEXT_MESSAGE_END     { message_id }

Phase 8 — Run Teardown
───────────────────────
→ STATE_DELTA          { patch: { status:"done", message:"..." } }
→ RUN_FINISHED         { run_id, final_state:{...}, mcp_summary:{ total_calls:4 } }
```

Total AG-UI events per run: **~80–150** (varies with query length and recommendation text)

---

## MCP Tool Registry

All tools are registered in `backend/mcp/mcp_server.py` and called via `MCPClient`.

### `search_products`

```
Arguments:  query (required), category (optional), max_price (optional)
Returns:    { query, filters, total_found, products[] }
Algorithm:  Token-based relevance scoring across name + description + tags,
            sorted by score descending, rating as tiebreaker.
```

### `get_product_details`

```
Arguments:  product_id (required)
Returns:    { found, product }
```

### `check_inventory`

```
Arguments:  product_id (required)
Returns:    { product_id, product_name, stock, availability, in_stock }
Labels:     "In Stock" | "Limited Stock" | "Low Stock — only a few left!" | "Out of Stock"
```

### `get_user_preferences`

```
Arguments:  user_id (required)
Returns:    { found, user_id, preferences: { budget_max, preferred_brands,
              preferred_categories, past_purchases, wishlist, notes } }
Fallback:   Returns safe defaults for unknown user IDs (no error thrown)
```

### `compare_products`

```
Arguments:  product_ids[] (required, 2–4 items)
Returns:    { products[], comparison: { price, rating, stock, category, tags },
              summary: { cheapest, best_rated, best_value } }
Value score: rating / (price / 100)  — higher = better value for money
```

### `get_recommendations`

```
Arguments:  user_id (required), query (required)
Returns:    { user_id, query, budget_max, recommendations[3] }
           Each rec: { rank, product, score, reason }

Scoring model (transparent, no LLM):
  +3  preferred brand appears in product name
  +2  preferred category matches product category
  +2  query relevance (token match, capped at 2)
  +1  product is on user's wishlist
  -10 product already purchased (deprioritise)
  -5  product price exceeds budget_max
  tie broken by rating
```

---

## Data Model

### Product (`data/products.json`)

```json
{
  "id":          "E001",
  "name":        "Sony WH-1000XM5 Headphones",
  "category":    "Electronics",
  "price":       279.99,
  "rating":      4.8,
  "stock":       15,
  "description": "Industry-leading noise cancellation...",
  "tags":        ["wireless", "noise-cancellation", "audio", "sony", "premium"]
}
```

**18 products** across three categories:
- **Electronics** (6): headphones, earbuds, Bluetooth speaker, TV, mouse
- **Clothing** (6): sneakers, jeans, fleece jacket, down vest, rain jacket, running shoes
- **Home** (6): pressure cooker, vacuum, smart bulbs, coffee maker, pillow, robot vacuum

### User Profile (`data/user_preferences.json`)

```json
{
  "user123": {
    "name":                 "Alex Johnson",
    "budget_max":           300,
    "preferred_brands":     ["Sony", "Apple", "Logitech"],
    "preferred_categories": ["Electronics"],
    "past_purchases":       ["E001", "E006"],
    "wishlist":             ["C003", "H002"]
  }
}
```

**Three demo users:**
| User ID | Name | Budget | Focus |
|---|---|---|---|
| `user123` | Alex Johnson | $300 | Premium electronics |
| `user456` | Jordan Lee | $100 | Budget-conscious, home |
| `user789` | Sam Rivera | $200 | Outdoor clothing |

---

## Educational Notes

### Why async generators for streaming?

`ShoppingAgent.process_query()` is an `async def` function that uses `yield` — making it an **async generator**. This is the ideal pattern for SSE streaming because:

1. Each `yield` immediately sends a chunk to the browser (zero buffering)
2. `await asyncio.sleep()` between yields yields control back to the event loop, so multiple concurrent requests are handled without blocking
3. The caller (`StreamingResponse` in FastAPI) just iterates the generator — it doesn't need to know anything about AG-UI or MCP

### Why does the agent emit both TOOL_CALL events AND STATE_DELTA?

These serve different consumers:

- `TOOL_CALL_START/END` → drives the **tool-call log** in the UI — shows the developer the MCP mechanics
- `STATE_DELTA` → drives the **product cards** and **status bar** — updates the application state the user sees

A production frontend might hide the tool-call log from end users while still relying on `STATE_DELTA` for rendering.

### Why is STATE_SNAPSHOT sent only once?

`STATE_SNAPSHOT` replaces the *entire* shared state, so it's expensive for the frontend to process (it must re-render everything). It's used exactly once — at the start of each run — to establish a clean baseline. All subsequent updates use `STATE_DELTA` (partial patches), which are cheap to apply and only trigger re-renders of the affected UI sections.

### Extending ShopStream with a real LLM

To connect a real LLM (e.g. OpenAI, Anthropic, Ollama):

1. Replace `_build_response_text()` in `shopping_agent.py` with a streaming LLM call
2. Feed the tool results as context into the prompt
3. Stream the model's token deltas as `TEXT_MESSAGE_CONTENT` events — the AG-UI protocol and the frontend require no changes

The MCP tool calls already produce all the structured data the LLM would need.

---

*ShopStream — AG-UI + MCP demonstration project*