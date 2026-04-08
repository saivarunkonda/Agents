# ContentForge 📝🤖 — Multi-Agent Content Creation Pipeline

> **A complete demonstration of ACP (Agent Communication Protocol) and MCP (Model Context Protocol) working together in a fully automated, multi-stage content creation system.**

---

## Overview

ContentForge shows what happens when a user says:

> *"Write me a blog post about Artificial Intelligence"*

…and a team of five autonomous AI agents handles **everything** — research, drafting, editing, SEO optimisation, and publishing — communicating exclusively through a standardised message bus, with every external resource accessed through named MCP tools.

The system demonstrates two foundational agentic protocols:

| Protocol | Role in ContentForge |
|----------|----------------------|
| **ACP** (Agent Communication Protocol) | All five agents communicate via a central pub/sub Message Bus. No agent calls another directly. Messages carry standardised envelopes with sender, receiver, type, and correlation IDs. |
| **MCP** (Model Context Protocol) | Every external resource (knowledge base, style guides, SEO database, file system) is accessed exclusively through named tools registered on an MCP Server. Agents never touch files directly. |

---

## Architecture

```
                        ACP MESSAGE BUS
                        (Central Hub — all communication flows here)
                               │
          ┌────────────────────┼─────────────────────┐
          │                    │                     │
   [ResearcherAgent]    [WriterAgent]         [EditorAgent]
   Stage 1: Research    Stage 2: Draft        Stage 3: Edit
   • search_topic       • get_style_guide     • check_readability
   • get_seo_keywords   • save_draft          • grammar_check
          │                    │                     │
          └────────────────────┼─────────────────────┘
                               │
                    [SEOAgent]    →    [PublisherAgent]
                   Stage 4: SEO        Stage 5: Publish
                   • get_seo_keywords  • publish_article
                   • check_readability • BROADCAST to all agents
                               │
                  ─────────────────────────────
                  All agents connect via:

                         MCP CLIENT
                              │
                         MCP SERVER
                         ├── search_topic
                         ├── get_style_guide
                         ├── get_seo_keywords
                         ├── save_draft
                         ├── publish_article
                         ├── check_readability
                         └── grammar_check

  ACP Registry (Service Discovery):
    ResearcherAgent | WriterAgent | EditorAgent | SEOAgent | PublisherAgent
```

---

## Project Structure

```
project4_contentforge/
├── README.md                           ← You are here
├── requirements.txt                    ← No external deps (pure stdlib)
├── main.py                             ← Entry point: wires up and runs the pipeline
│
├── acp/                                ← ACP Protocol Layer
│   ├── __init__.py
│   ├── message.py                      ← ACPMessage envelope + type enums
│   ├── message_bus.py                  ← Central pub/sub message broker
│   └── agent_registry.py              ← Service-discovery registry
│
├── mcp/                                ← MCP Protocol Layer
│   ├── __init__.py
│   ├── mcp_server.py                   ← Tool registry + all tool implementations
│   └── mcp_client.py                   ← Agent-facing tool call interface
│
├── agents/                             ← The five pipeline agents
│   ├── __init__.py
│   ├── base_agent.py                   ← Base class: subscribe, send, broadcast
│   ├── researcher_agent.py             ← Stage 1: research via MCP tools
│   ├── writer_agent.py                 ← Stage 2: draft article using style guide
│   ├── editor_agent.py                 ← Stage 3: readability + grammar review
│   ├── seo_agent.py                    ← Stage 4: keyword optimisation + metadata
│   └── publisher_agent.py             ← Stage 5: publish + broadcast completion
│
└── data/
    ├── knowledge_base.json             ← Mock research facts (AI, climate, space)
    ├── style_guides.json               ← Writing style rules (blog, technical)
    ├── seo_keywords.json               ← Keyword database with search volumes
    └── published/                      ← Output: .md articles + _meta.json sidecars
```

---

## The Two Protocols Explained

### ACP — Agent Communication Protocol

ACP transforms siloed agents into an interoperable network by standardising **how** they communicate, not **what** they do.

#### The Message Envelope

Every single agent interaction in ContentForge uses this envelope:

```python
@dataclass
class ACPMessage:
    message_id:      str             # unique ID for this message
    sender_id:       str             # who sent it
    receiver_id:     Optional[str]   # who should receive it (None = broadcast)
    message_type:    ACPMessageType  # REQUEST / RESPONSE / BROADCAST / ERROR / ACK
    content_type:    ACPContentType  # text/plain, application/json, text/markdown
    payload:         Any             # the actual data
    correlation_id:  Optional[str]   # links this message back to a prior one
    timestamp:       str             # ISO-8601 creation time
    metadata:        dict            # arbitrary key-value context
```

This envelope means the **Message Bus** can route, log, and audit all communication without ever reading the payload — a core ACP principle.

#### The Message Bus (pub/sub)

```
Agent A                  ACP Message Bus              Agent B
   │                           │                          │
   │  publish(msg)             │                          │
   │──────────────────────────>│                          │
   │                           │  deliver to receiver_id  │
   │                           │─────────────────────────>│
   │                           │                          │ handle_message(msg)
```

**Agents never hold references to each other.** They only hold a reference to the bus. This means:
- Adding a new agent = subscribe it to the bus. Zero changes to existing agents.
- Removing an agent = unsubscribe it. Zero changes to existing agents.
- Reordering pipeline stages = change message routing only.

#### ACP Message Types Used

| Type | When Used | Example |
|------|-----------|---------|
| `REQUEST` | Orchestrator kicks off the pipeline | "Please research: artificial intelligence" |
| `RESPONSE` | Agent completes work, passes to next stage | ResearcherAgent → WriterAgent |
| `BROADCAST` | PublisherAgent notifies all agents | "Article published: The Future of AI" |
| `ERROR` | Agent reports a failure | "Topic not found in knowledge base" |

#### ACP Agent Registry (Service Discovery)

Before the first message is sent, every agent registers itself:

```python
ACPAgentInfo(
    agent_id      = "researcher",
    name          = "ResearcherAgent",
    description   = "Researches topics using MCP tools...",
    input_schema  = {"intent": "str", "topic": "str", "content_type": "str"},
    output_schema = {"topic": "str", "facts": "list", "seo_hints": "dict"},
    capabilities  = ["research", "fact-gathering", "seo", "stage-1"],
    status        = "idle"
)
```

The orchestrator can inspect the full pipeline roster **before** sending the first message — true service discovery.

#### ACP Correlation Chains

Every RESPONSE carries the original REQUEST's `message_id` as its `correlation_id`. This creates a traceable thread:

```
[REQUEST  msg_id=A   ] orchestrator → researcher  "research AI"
[RESPONSE msg_id=B   correlation_id=A] researcher → writer    "here are the facts"
[RESPONSE msg_id=C   correlation_id=B] writer → editor        "here is the draft"
[RESPONSE msg_id=D   correlation_id=C] editor → seo           "here is the revision"
[RESPONSE msg_id=E   correlation_id=D] seo → publisher        "here is optimised content"
[BROADCAST msg_id=F  correlation_id=E] publisher → ALL        "article published!"
```

---

### MCP — Model Context Protocol

MCP gives agents a **standardised, named interface** to all external resources. Agents never read files, call APIs, or touch databases directly.

#### Tool Registration

The MCP Server registers tools at startup:

```python
{
    "search_topic": {
        "description": "Search the knowledge base for facts about a topic",
        "parameters": {"topic": "str"},
        "handler": <function>
    },
    "get_style_guide": {
        "description": "Retrieve writing style guidelines for a content type",
        "parameters": {"content_type": "str"},
        "handler": <function>
    },
    ...7 tools total
}
```

#### Tool Calls (Client Side)

```python
# Agent calls a tool by name — never touches the file directly
result = self.mcp.call_tool("search_topic", topic="artificial intelligence")
# [MCP][researcher] → CALL search_topic(topic='artificial intelligence')
# [MCP][researcher] ← RESULT: {facts: [...], key_people: [...], statistics: {...}}
```

#### MCP Tools in ContentForge

| Tool | Used By | What It Does |
|------|---------|--------------|
| `search_topic` | ResearcherAgent | Retrieves facts, key people, statistics from knowledge base |
| `get_style_guide` | WriterAgent | Returns tone, structure, word count targets for content type |
| `get_seo_keywords` | ResearcherAgent, SEOAgent | Returns primary, secondary, long-tail keywords + search volumes |
| `save_draft` | WriterAgent | Persists article draft to the data directory |
| `publish_article` | PublisherAgent | Writes final `.md` and `_meta.json` to `data/published/` |
| `check_readability` | EditorAgent | Returns word count, sentence length, reading score |
| `grammar_check` | EditorAgent | Returns simulated grammar issues list |

#### Why MCP Matters

Without MCP, every agent would need its own file-reading code, database driver, and API client. With MCP:

```
BEFORE MCP:                          AFTER MCP:
ResearcherAgent reads JSON ──┐       ResearcherAgent
WriterAgent reads JSON ──────┤  →        │
EditorAgent reads JSON ──────┤       mcp.call_tool("search_topic")
SEOAgent reads JSON ─────────┘            │
                                      MCP Server reads JSON
```

Change the data source (JSON → database → live API) = change **only** the MCP Server. All five agents continue working unchanged.

---

## Pipeline Flow

```
User Request: "Write a blog post about artificial intelligence"
       │
       ▼
[main.py] Create MCPServer, MCPClient, ACPMessageBus, ACPAgentRegistry
       │
       ▼
[main.py] Instantiate & register 5 agents (all subscribe to bus)
       │
       ▼
[main.py] Publish initial REQUEST → researcher
       │
       ▼ ─────────────────── STAGE 1 ───────────────────
[ResearcherAgent] receives REQUEST
  ├── MCP: search_topic("artificial intelligence")
  │         → facts, key_people, statistics
  ├── MCP: get_seo_keywords("artificial intelligence")
  │         → primary/secondary/long-tail keywords
  └── ACP RESPONSE → writer  {research_brief}
       │
       ▼ ─────────────────── STAGE 2 ───────────────────
[WriterAgent] receives RESPONSE from researcher
  ├── MCP: get_style_guide("blog")
  │         → tone, structure, word count targets
  ├── Generates full article (title + 6 sections + conclusion)
  ├── MCP: save_draft(title, content)
  └── ACP RESPONSE → editor  {draft_content, word_count}
       │
       ▼ ─────────────────── STAGE 3 ───────────────────
[EditorAgent] receives RESPONSE from writer
  ├── MCP: check_readability(content)
  │         → score, avg sentence length, paragraph count
  ├── MCP: grammar_check(content)
  │         → issues list
  ├── Applies improvements (structural, clarity, grammar fixes)
  └── ACP RESPONSE → seo  {revised_content, edits_made, score}
       │
       ▼ ─────────────────── STAGE 4 ───────────────────
[SEOAgent] receives RESPONSE from editor
  ├── MCP: get_seo_keywords("artificial intelligence")
  │         → keyword targets + search volumes
  ├── Analyses keyword density in content
  ├── Generates meta_title, meta_description, tags, slug
  └── ACP RESPONSE → publisher  {final_content, seo_metadata}
       │
       ▼ ─────────────────── STAGE 5 ───────────────────
[PublisherAgent] receives RESPONSE from seo
  ├── MCP: publish_article(title, content, metadata)
  │         → writes data/published/<slug>.md
  │         → writes data/published/<slug>_meta.json
  └── ACP BROADCAST → ALL  "Article published: The Future of AI"
       │
       ▼
[main.py] Prints full ACP message history + pipeline summary
```

---

## ACP vs A2A: Key Difference

This project (ContentForge) uses **ACP**. Project 3 (TravelMind) uses **A2A**. Here is the core difference:

| Aspect | ACP (this project) | A2A (TravelMind) |
|--------|--------------------|------------------|
| Communication style | Pub/Sub via central Message Bus | Direct client→server HTTP-style calls |
| Agent coupling | Loosely coupled (agents don't know each other) | Tightly coupled (client holds server reference) |
| Discovery | Agent Registry (self-registration) | Agent Cards (published capability documents) |
| Best for | Sequential pipelines, complex workflows, observability | Service-to-service task delegation, real-time auth |
| Adding agents | Subscribe to bus — zero other changes | Register Agent Card + update client discovery logic |
| Message tracing | Full history on the bus | Per-task logs on the client |

---

## How to Run

```bash
# Navigate to the project root
cd Agent

# No pip install needed — pure Python stdlib
python project4_contentforge/main.py
```

The pipeline runs **two full articles**:
1. **Artificial Intelligence** (blog format)
2. **Space Exploration** (blog format)

Published articles appear in:
```
Agent/project4_contentforge/data/published/
  ├── the-future-of-artificial-intelligence.md
  ├── the-future-of-artificial-intelligence_meta.json
  ├── exploring-the-final-frontier-space-exploration.md
  └── exploring-the-final-frontier-space-exploration_meta.json
```

---

## Output Files

### `.md` — The Article

Each published article is a Markdown file with YAML front matter:

```yaml
---
title: "The Future of Artificial Intelligence"
slug: the-future-of-artificial-intelligence
date: 2025-09-15T14:23:01
author: ContentForge (ACP+MCP Pipeline)
meta_title: "The Future of Artificial Intelligence | AI Technology Guide"
meta_description: "Discover the latest trends in artificial intelligence..."
tags: [artificial intelligence, AI technology, machine learning, deep learning]
seo_score: 87
reading_time_minutes: 6
word_count: 1243
---

# The Future of Artificial Intelligence
...full article content...
```

### `_meta.json` — Complete Provenance Record

The sidecar JSON captures the full audit trail:
- Research facts used, keywords identified
- Style guide applied, word count targets
- All edits made by the EditorAgent (with categories and impact)
- SEO score, keyword density analysis, meta tags
- Full pipeline stages with MCP tools called at each stage

---

## Educational Log Reading Guide

When you run the pipeline, look for these prefixes:

| Prefix | What It Shows |
|--------|---------------|
| `[MCP SERVER]` | MCP Server loading data files and registering tools |
| `[MCP CLIENT]` | An agent calling a named tool and receiving a result |
| `[BUS]` | ACP Message Bus routing a message to a subscriber |
| `[REGISTRY]` | ACP Registry recording an agent's self-registration |
| `[ACP][ResearcherAgent]` | ResearcherAgent processing or sending an ACP message |
| `[ACP][WriterAgent]` | WriterAgent processing or sending an ACP message |
| `[ACP][EditorAgent]` | EditorAgent processing or sending an ACP message |
| `[ACP][SEOAgent]` | SEOAgent processing or sending an ACP message |
| `[ACP][PublisherAgent]` | PublisherAgent processing or sending an ACP message |
| `[BOOTSTRAP]` | main.py wiring up the shared infrastructure |

At the end of each pipeline run, the **full ACP message history** is printed — showing every single message that flowed through the bus, with sender, receiver, type, and correlation IDs. This is one of ACP's key strengths: **complete observability**.

---

## Requirements

```
Python 3.8+
No external packages required — pure standard library only
```

---

## Summary

ContentForge demonstrates that:

1. **ACP's message bus** enables a fully decoupled, observable, and extensible multi-agent pipeline
2. **MCP's tool layer** gives every agent a uniform, swappable interface to all external resources
3. Together, ACP + MCP allow complex, multi-step content workflows to be built from simple, single-purpose agents that know nothing about each other
4. The correlation chain in ACP messages gives you full end-to-end traceability from the initial request to the published article — for free