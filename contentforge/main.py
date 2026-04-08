# =============================================================================
# main.py  —  ContentForge Pipeline Orchestrator
# =============================================================================
#
# OVERVIEW
# --------
# This file is the entry point for the ContentForge Multi-Agent Content
# Creation Pipeline.  It:
#
#   1. Bootstraps the shared infrastructure:
#        - MCPServer  : hosts all tool implementations (search, style, SEO, etc.)
#        - MCPClient  : agents' interface to MCP tools
#        - ACPMessageBus    : the central pub/sub message broker
#        - ACPAgentRegistry : the agent service-discovery directory
#
#   2. Instantiates and wires up all five pipeline agents:
#        ResearcherAgent → WriterAgent → EditorAgent → SEOAgent → PublisherAgent
#
#   3. Registers each agent in the ACPAgentRegistry
#      (demonstrating ACP's service-discovery capability)
#
#   4. Sends an initial REQUEST message to the ResearcherAgent to kick off
#      the pipeline (demonstrating ACP's message-driven execution model)
#
#   5. The pipeline runs synchronously — each agent processes its message
#      immediately when the bus delivers it, so the entire Researcher →
#      Writer → Editor → SEO → Publisher chain executes within this call.
#
#   6. Prints the full ACP message history at the end
#      (demonstrating ACP's observability guarantee)
#
#   7. Runs the complete pipeline for TWO topics to show that the same
#      pipeline handles any topic without code changes.
#
# PROTOCOLS DEMONSTRATED
# ----------------------
#
#   ACP (Agent Communication Protocol)
#   ------------------------------------
#   • ACPMessage envelope: every inter-agent message uses a standard envelope
#     with sender_id, receiver_id, message_type, content_type, payload,
#     correlation_id, and timestamp.
#   • ACPMessageBus: agents publish to and subscribe from a central bus.
#     They NEVER call each other directly — only the bus.
#   • ACPAgentRegistry: agents self-register their capabilities at startup.
#     Any component can query "who is available and what can they do?" without
#     importing agent classes.
#   • Correlation chains: every RESPONSE carries the correlation_id of the
#     REQUEST that triggered it, enabling full request/response traceability.
#   • Broadcast completion: the PublisherAgent uses a BROADCAST message to
#     announce pipeline completion to ALL agents simultaneously.
#
#   MCP (Model Context Protocol)
#   -----------------------------
#   • MCPServer: registers named tools that agents call by name.
#   • MCPClient: agents call self.mcp.call_tool("tool_name", **params) —
#     they never access files, databases, or APIs directly.
#   • Tool discovery: agents can call mcp.list_tools() to see what's
#     available at runtime, enabling dynamic, self-adapting behaviour.
#   • Separation of concerns: tool IMPLEMENTATIONS live in MCPServer;
#     agent LOGIC lives in the agent classes.  Neither knows the other's internals.
#
# RUNNING
# -------
#   cd Agent/project4_contentforge
#   python main.py
#
#   No dependencies beyond Python 3.9+ standard library.
# =============================================================================

import os
import sys
import time
from datetime import datetime
from typing import Optional

# ---------------------------------------------------------------------------
# Windows UTF-8 fix — box-drawing and emoji characters require UTF-8 output.
# On Windows the default console codec is cp1252 which cannot encode them.
# ---------------------------------------------------------------------------
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        # Python < 3.7 fallback
        import io

        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

# ---------------------------------------------------------------------------
# Ensure the project root is on sys.path so all relative imports work when
# this file is run directly (e.g. `python main.py` from the project directory
# OR `python project4_contentforge/main.py` from the Agent/ directory).
# ---------------------------------------------------------------------------
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

# ---------------------------------------------------------------------------
# ACP imports — the communication protocol layer
# ---------------------------------------------------------------------------
from acp.agent_registry import ACPAgentInfo, ACPAgentRegistry
from acp.message import ACPContentType, ACPMessage, ACPMessageType
from acp.message_bus import ACPMessageBus

# ---------------------------------------------------------------------------
# Agent imports — the intelligent processing units
# ---------------------------------------------------------------------------
from agents.editor_agent import EditorAgent
from agents.publisher_agent import PublisherAgent
from agents.researcher_agent import ResearcherAgent
from agents.seo_agent import SEOAgent
from agents.writer_agent import WriterAgent

# ---------------------------------------------------------------------------
# MCP imports — the tool/resource access layer
# ---------------------------------------------------------------------------
from mcp.mcp_client import MCPClient
from mcp.mcp_server import MCPServer

# =============================================================================
# PIPELINE RUNNER
# =============================================================================


def run_pipeline(
    topic: str,
    content_type: str = "blog",
    mcp_server: Optional[MCPServer] = None,
    verbose_mcp: bool = True,
) -> PublisherAgent:
    """
    Run the complete ContentForge multi-agent content creation pipeline
    for a given topic and content format.

    Pipeline stages:
        1. ResearcherAgent  — gathers facts, key people, statistics, SEO hints
        2. WriterAgent      — composes a full article draft using a style guide
        3. EditorAgent      — checks readability & grammar, improves the draft
        4. SEOAgent         — analyses keyword density, generates SEO metadata
        5. PublisherAgent   — publishes the article and broadcasts completion

    ACP CONCEPT: Pipeline as a Message Chain
    -----------------------------------------
    The pipeline is NOT a for-loop that calls agents sequentially.
    It is a MESSAGE CHAIN: each agent's output becomes the next agent's
    input, but the "wiring" happens through the ACP Message Bus — not
    through direct function calls or shared state.

    The entire pipeline is triggered by ONE message:
        bus.publish(REQUEST to "researcher" with topic=...)

    Everything else — Research → Write → Edit → SEO → Publish —
    happens automatically through cascading ACP messages.

    MCP CONCEPT: Shared Tool Layer
    --------------------------------
    All five agents share the same MCPServer instance (and therefore the
    same tool implementations and data files).  Each agent gets its own
    MCPClient instance so that per-agent call ledgers are independent.
    The server is stateless between calls, making this sharing safe.

    Parameters
    ----------
    topic : str
        The article topic to create content about.
        Examples: "artificial intelligence", "space exploration"

    content_type : str
        Target content format. One of: "blog", "technical", "news".
        Determines which style guide the WriterAgent uses.
        Default: "blog"

    mcp_server : MCPServer, optional
        A pre-existing MCPServer instance to reuse across pipeline runs.
        If None, a new MCPServer is created for this run.
        Passing a shared server avoids reloading data files each run.

    verbose_mcp : bool
        If True, MCP tool calls are logged with [MCP] prefix.
        Set to False to reduce console noise on subsequent runs.
        Default: True

    Returns
    -------
    PublisherAgent
        The PublisherAgent instance after the pipeline has completed.
        Callers can inspect publisher.get_stats() or access
        publisher._publications for post-run analysis.
    """
    run_start = time.time()

    _print_pipeline_header(topic, content_type)

    # =========================================================================
    # STEP 1: BOOTSTRAP INFRASTRUCTURE
    # =========================================================================
    # ACP and MCP infrastructure is set up ONCE per pipeline run and shared
    # across all agents.  This mirrors real-world deployments where:
    #   - The message bus (e.g. RabbitMQ) is a long-lived shared service
    #   - The MCP server (e.g. a FastAPI app) serves multiple agent clients
    #   - Agent instances are created fresh for each task but share infrastructure
    # =========================================================================

    _print_section("STEP 1: Bootstrapping ACP + MCP Infrastructure")

    # ---- 1a: MCP Server -------------------------------------------------
    # The MCPServer is the "tool provider" for the entire pipeline.
    # It loads all data files (knowledge_base.json, style_guides.json,
    # seo_keywords.json) and registers the tool implementations.
    # We optionally reuse a pre-existing server to avoid re-loading files.
    print("\n  [BOOTSTRAP] Creating MCP Server (tool provider)...")
    print(
        "  MCP CONCEPT: The MCPServer is the single source of truth for all\n"
        "  external resources.  Agents never access files or APIs directly.\n"
        "  They call named tools on this server via the MCPClient interface.\n"
    )

    if mcp_server is None:
        mcp_server = MCPServer()
    else:
        print(
            "  [BOOTSTRAP] Reusing existing MCPServer instance (data already loaded)."
        )

    # ---- 1b: ACP Message Bus --------------------------------------------
    # The ACPMessageBus is the central nervous system of the agent pipeline.
    # Every message between agents flows through this single object.
    # Creating a NEW bus for each pipeline run ensures message histories
    # are clean and don't mix across runs.
    print("\n  [BOOTSTRAP] Creating ACP Message Bus (central communication hub)...")
    print(
        "  ACP CONCEPT: The Message Bus decouples agents from each other.\n"
        "  Agents never hold references to other agents — only to the bus.\n"
        "  Adding a new agent = subscribe it to the bus. Zero other changes.\n"
    )
    bus = ACPMessageBus()

    # ---- 1c: ACP Agent Registry -----------------------------------------
    # The ACPAgentRegistry is the service-discovery layer.
    # Agents register their capabilities here so any component can ask:
    # "What agents are available and what can they do?"
    # without importing or instantiating agent classes directly.
    print("\n  [BOOTSTRAP] Creating ACP Agent Registry (service discovery)...")
    print(
        "  ACP CONCEPT: The Registry is like a professional directory for agents.\n"
        "  Each agent publishes its ID, description, input/output schemas,\n"
        "  and current status.  The orchestrator (this file) can inspect\n"
        "  the full pipeline roster before sending the first message.\n"
    )
    registry = ACPAgentRegistry()

    # =========================================================================
    # STEP 2: INSTANTIATE AND WIRE UP ALL AGENTS
    # =========================================================================
    # Each agent is given:
    #   - The shared ACP Message Bus  (its only communication channel)
    #   - Its own MCPClient instance  (its only tool-access channel)
    #   - The shared ACPAgentRegistry (for live status updates)
    #
    # Note: each agent gets a SEPARATE MCPClient so that per-agent call
    # ledgers (the MCP equivalent of per-agent message history) are
    # independent and can be queried individually after the run.
    # =========================================================================

    _print_section("STEP 2: Instantiating and Wiring Up Pipeline Agents")

    print(
        "\n  ACP CONCEPT: Agents subscribe to the bus at construction time.\n"
        "  After this step, each agent has an 'inbox' on the bus — any message\n"
        "  addressed to its agent_id will be delivered to its handle_message().\n"
        "  Notice that agents are created in ANY order; pipeline order is\n"
        "  determined by message routing, not by instantiation order.\n"
    )

    # ---- Create one MCPClient per agent ---------------------------------
    # In production, each agent might connect to a DIFFERENT MCP server
    # (specialised tools for research, a different one for publishing, etc.)
    # Here all agents share the same server but each has its own client proxy.
    researcher_mcp = MCPClient(mcp_server, agent_id="researcher", verbose=verbose_mcp)
    writer_mcp = MCPClient(mcp_server, agent_id="writer", verbose=verbose_mcp)
    editor_mcp = MCPClient(mcp_server, agent_id="editor", verbose=verbose_mcp)
    seo_mcp = MCPClient(mcp_server, agent_id="seo", verbose=verbose_mcp)
    publisher_mcp = MCPClient(mcp_server, agent_id="publisher", verbose=verbose_mcp)

    print()

    # ---- Instantiate all five agents ------------------------------------
    # Each constructor calls bus.subscribe(agent_id, self.handle_message),
    # giving the agent its address on the ACP bus.
    researcher = ResearcherAgent(bus=bus, mcp_client=researcher_mcp, registry=registry)
    writer = WriterAgent(bus=bus, mcp_client=writer_mcp, registry=registry)
    editor = EditorAgent(bus=bus, mcp_client=editor_mcp, registry=registry)
    seo_agent = SEOAgent(bus=bus, mcp_client=seo_mcp, registry=registry)
    publisher = PublisherAgent(bus=bus, mcp_client=publisher_mcp, registry=registry)

    # =========================================================================
    # STEP 3: REGISTER AGENTS IN THE ACP AGENT REGISTRY
    # =========================================================================
    # Each agent calls get_agent_info() to produce a rich ACPAgentInfo record
    # describing its ID, capabilities, input/output schemas, pipeline stage,
    # and tags.  These records are stored in the registry.
    #
    # ACP CONCEPT: Self-Describing Agents
    # -------------------------------------
    # By declaring their schemas in the registry, agents become self-documenting.
    # The orchestrator (or a monitoring tool) can inspect the full pipeline
    # contract without reading source code:
    #   registry.get_agent("researcher").output_schema  →  what the researcher produces
    #   registry.get_agent("writer").input_schema       →  what the writer expects
    # =========================================================================

    _print_section("STEP 3: Registering Agents in the ACP Registry")

    print(
        "\n  ACP CONCEPT: Self-registration at startup mirrors how microservices\n"
        "  register with service meshes (Consul, Kubernetes Service Discovery).\n"
        "  The registry becomes the authoritative source of pipeline topology.\n"
    )

    # Register agents in pipeline order (for clean display)
    for agent in [researcher, writer, editor, seo_agent, publisher]:
        registry.register(agent.get_agent_info())

    # ---- Print the registry (educational: see who is in the pipeline) ---
    registry.print_registry()

    # =========================================================================
    # STEP 4: SHOW MCP TOOL CATALOGUE
    # =========================================================================
    # Before the pipeline runs, show what MCP tools are available.
    # This demonstrates MCP's tool-discovery capability.
    # =========================================================================

    _print_section("STEP 4: MCP Tool Catalogue (Tool Discovery)")

    print(
        "\n  MCP CONCEPT: Before agents start working, any client can query\n"
        "  the MCP server to see what tools are available.  This enables\n"
        "  dynamic agent behaviour: 'I'll use whatever search tool exists.'\n"
    )
    researcher_mcp.print_tool_catalogue()

    # =========================================================================
    # STEP 5: LAUNCH THE PIPELINE (send the first ACP message)
    # =========================================================================
    # The entire ContentForge pipeline is triggered by ONE ACP message:
    # a REQUEST addressed to the ResearcherAgent with the topic to research.
    #
    # ACP CONCEPT: Event-Driven Pipeline Execution
    # ----------------------------------------------
    # We don't call researcher.research(topic) directly.
    # We don't call writer.write(research_data) directly.
    # We PUBLISH a message to the bus and let the agent chain run.
    #
    # This is the fundamental difference between:
    #   - Direct orchestration: orchestrator calls each agent in order
    #   - ACP message-driven:   orchestrator fires one message; agents self-coordinate
    #
    # The bus.publish() call triggers:
    #   1. ResearcherAgent.handle_message()  → calls MCP, sends to WriterAgent
    #   2. WriterAgent.handle_message()      → calls MCP, sends to EditorAgent
    #   3. EditorAgent.handle_message()      → calls MCP, sends to SEOAgent
    #   4. SEOAgent.handle_message()         → calls MCP, sends to PublisherAgent
    #   5. PublisherAgent.handle_message()   → calls MCP, broadcasts to ALL
    #
    # All of this happens synchronously within the single bus.publish() call
    # below because our bus implementation is synchronous (calls handlers
    # immediately).  In a real async system, each step would be a separate
    # async task, but the ACP contract would be identical.
    # =========================================================================

    _print_section(f"STEP 5: Launching Pipeline — Topic: '{topic}'")

    print(
        f"\n  ACP CONCEPT: The pipeline is triggered by ONE REQUEST message.\n"
        f"  We publish this message to the bus; the bus delivers it to the\n"
        f"  ResearcherAgent; the agent chain does the rest.\n"
        f"\n"
        f"  We act as 'the orchestrator' here — an external component that\n"
        f"  knows the pipeline's entry point ('researcher') but nothing about\n"
        f"  the internal agents.  In production this role might be played by\n"
        f"  a REST API endpoint, a scheduler, or a user-facing web application.\n"
    )

    # ---- Build the initial REQUEST payload --------------------------------
    # This is the ONLY message we send manually.  All subsequent messages
    # are sent by the agents themselves via the bus.
    initial_request_payload = {
        "intent": "research_topic",  # tells ResearcherAgent what to do
        "topic": topic,  # the subject to research and write about
        "content_type": content_type,  # "blog", "technical", or "news"
        "requested_at": datetime.now().isoformat(),
        "requested_by": "ContentForge Orchestrator (main.py)",
    }

    # ---- Construct the ACP message envelope ------------------------------
    # We construct this manually here (rather than using agent.send()) because
    # the orchestrator is not itself an ACP agent — it doesn't have an agent_id
    # or a bus subscription.
    #
    # ACP CONCEPT: The Initial Message
    # ----------------------------------
    # This first REQUEST is the "seed" that grows into the entire pipeline.
    # Its message_id becomes the root of the correlation chain — every
    # subsequent message will carry correlation_id = this message's id
    # (or a descendant of it), creating a traceable thread through the history.
    initial_request = ACPMessage(
        sender_id="orchestrator",  # who is sending this?
        receiver_id=ResearcherAgent.AGENT_ID,  # who should receive it?
        message_type=ACPMessageType.REQUEST,  # what type of interaction?
        content_type=ACPContentType.JSON,  # what format is the payload?
        payload=initial_request_payload,
        metadata={
            "pipeline_run_id": f"run_{int(time.time())}",
            "topic": topic,
            "content_type": content_type,
        },
    )

    print(
        f"\n  ── INITIAL ACP REQUEST ──────────────────────────────────────────\n"
        f"  Sender  : orchestrator\n"
        f"  Receiver: {ResearcherAgent.AGENT_ID}\n"
        f"  Type    : {ACPMessageType.REQUEST.value}\n"
        f"  Msg ID  : {initial_request.message_id}\n"
        f"  Payload : topic='{topic}', content_type='{content_type}'\n"
        f"  ────────────────────────────────────────────────────────────────\n"
    )

    print("  FIRING PIPELINE... (synchronous — all 5 stages run in sequence)\n")
    print("  " + "─" * 66)

    # ---- PUBLISH → the entire pipeline runs from this single call --------
    pipeline_start = time.time()
    bus.publish(initial_request)
    pipeline_duration = time.time() - pipeline_start

    print("  " + "─" * 66)
    print(f"\n  Pipeline execution complete in {pipeline_duration:.2f}s.")

    # =========================================================================
    # STEP 6: POST-RUN ANALYSIS
    # =========================================================================
    # Now that the pipeline has finished, we can inspect everything that
    # happened from a bird's-eye view.
    # =========================================================================

    _print_section("STEP 6: Post-Run Analysis")

    # ---- 6a: ACP Registry final status ----------------------------------
    print("\n  ── ACP Registry: Final Agent Status ─────────────────────────────")
    print(
        "  ACP CONCEPT: The registry shows the FINAL status of every agent.\n"
        "  A complete, successful pipeline should show all agents as 'done'.\n"
    )
    registry.print_status_board()

    # ---- 6b: ACP Message Bus history ------------------------------------
    print()
    print(
        "  ACP CONCEPT: The message bus recorded EVERY message that flowed\n"
        "  through the pipeline.  This is ACP's observability guarantee:\n"
        "  you can reconstruct the entire pipeline run from this history.\n"
    )
    bus.print_message_log()

    # ---- 6c: Per-agent statistics ----------------------------------------
    _print_section("Agent Performance Statistics")
    print(
        "\n  Each agent's send/receive/MCP call counts.\n"
        "  This shows how much each agent contributed to the pipeline.\n"
    )
    all_agents = [researcher, writer, editor, seo_agent, publisher]
    for agent in all_agents:
        agent.print_stats()

    # ---- 6d: MCP server usage statistics --------------------------------
    print()
    mcp_server.print_usage_stats()

    # ---- 6e: Per-agent MCP call details ----------------------------------
    print("\n  ── Per-Agent MCP Call Ledgers ───────────────────────────────────")
    print(
        "  MCP CONCEPT: Each agent has its own call ledger — an ordered\n"
        "  record of every tool it called.  Together these ledgers show\n"
        "  the complete 'resource access' story of the pipeline.\n"
    )
    for agent in all_agents:
        agent.mcp.print_call_summary()

    # ---- 6f: Pipeline summary -------------------------------------------
    total_duration = time.time() - run_start
    _print_run_summary(topic, content_type, bus, registry, total_duration)

    return publisher


# =============================================================================
# DISPLAY HELPERS
# =============================================================================


def _print_pipeline_header(topic: str, content_type: str) -> None:
    """
    Print the ContentForge pipeline header banner.

    Parameters
    ----------
    topic : str
        The article topic for this pipeline run.
    content_type : str
        The content format (blog / technical / news).
    """
    width = 68
    border = "═" * width

    print(f"\n\n  ╔{border}╗")
    print(f"  ║{'CONTENTFORGE — Multi-Agent Content Creation Pipeline':^{width}}║")
    print(
        f"  ║{'ACP (Agent Communication Protocol) + MCP (Model Context Protocol)':^{width}}║"
    )
    print(f"  ╠{border}╣")
    topic_line = f"Topic       : {topic}"
    type_line = f"Format      : {content_type}"
    time_line = f"Started     : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    print(f"  ║  {topic_line:<{width - 2}}║")
    print(f"  ║  {type_line:<{width - 2}}║")
    print(f"  ║  {time_line:<{width - 2}}║")
    print(f"  ╚{border}╝")

    print(
        f"\n  PIPELINE: ResearcherAgent → WriterAgent → EditorAgent "
        f"→ SEOAgent → PublisherAgent"
    )
    print(f"  PROTOCOLS: ACP (inter-agent messages) + MCP (tool access)")
    print()


def _print_section(title: str) -> None:
    """
    Print a clearly visible section separator for the console output.

    Parameters
    ----------
    title : str
        The section title to display.
    """
    width = 68
    print(f"\n\n{'=' * (width + 4)}")
    print(f"  {title}")
    print(f"{'=' * (width + 4)}")


def _print_run_summary(
    topic: str,
    content_type: str,
    bus: ACPMessageBus,
    registry: ACPAgentRegistry,
    duration: float,
) -> None:
    """
    Print a concise run summary after the pipeline completes.

    Shows the key metrics of the run: how many messages were exchanged,
    whether all agents completed successfully, and the total runtime.

    Parameters
    ----------
    topic : str
    content_type : str
    bus : ACPMessageBus
    registry : ACPAgentRegistry
    duration : float  Total run time in seconds.
    """
    stats = bus.get_stats()
    is_complete = registry.is_pipeline_complete()
    has_errors = registry.has_errors()

    print(f"\n\n{'─' * 72}")
    print(f"  RUN SUMMARY  |  Topic: '{topic}'  |  Format: {content_type}")
    print(f"{'─' * 72}")
    print(f"  ACP Messages Exchanged : {stats['total_messages']}")
    print(f"    ↳ Requests           : {stats['request_count']}")
    print(f"    ↳ Responses          : {stats['response_count']}")
    print(f"    ↳ Broadcasts         : {stats['broadcast_messages']}")
    print(f"    ↳ Errors             : {stats['error_count']}")
    print(f"  Active Agents          : {stats['subscriber_count']}")
    print(
        f"  Pipeline Status        : {'✓ COMPLETE' if is_complete else '✗ ERROR' if has_errors else '⚠ INCOMPLETE'}"
    )
    print(f"  Total Duration         : {duration:.2f}s")
    print(f"{'─' * 72}")


def _print_acp_education_block() -> None:
    """
    Print an educational overview of ACP and MCP concepts before the pipeline runs.

    This block is printed once at program startup to orient the reader to the
    key protocol concepts they are about to see demonstrated.
    """
    width = 68
    border = "─" * width

    print(f"\n\n  ┌{border}┐")
    print(f"  │{'EDUCATIONAL OVERVIEW: ACP + MCP':^{width}}│")
    print(f"  ├{border}┤")

    concepts = [
        (
            "ACP Message Bus",
            "Agents communicate by publishing messages to a central bus, never directly.",
        ),
        (
            "ACP Message Envelope",
            "Every message has: sender_id, receiver_id, type, payload, correlation_id.",
        ),
        (
            "ACP Agent Registry",
            "Agents self-register capabilities; the registry is service discovery.",
        ),
        (
            "ACP Correlation Chain",
            "Each RESPONSE carries the REQUEST's message_id so chains are traceable.",
        ),
        (
            "ACP Broadcast",
            "receiver_id=None → delivered to ALL agents. Used for pipeline events.",
        ),
        (
            "MCP Tool Server",
            "The MCPServer hosts named tools: search, style guides, SEO, publish.",
        ),
        (
            "MCP Tool Client",
            "Agents call mcp.call_tool('name', **params). Never touch files directly.",
        ),
        (
            "MCP Tool Discovery",
            "mcp.list_tools() returns the server's full capability catalogue.",
        ),
    ]

    for name, desc in concepts:
        name_part = f"  {name:<26}"
        # Wrap description to fit in the box
        desc_part = desc if len(desc) <= 38 else desc[:35] + "..."
        print(f"  │  {name:<26}│ {desc_part:<{width - 30}}│")

    print(f"  ├{border}┤")
    print(
        f"  │  {'ACP vs A2A:':<26}│ {'ACP: pub/sub bus. A2A: direct client-server HTTP calls.':<{width - 30}}│"
    )
    print(
        f"  │  {'ACP vs direct calls:':<26}│ {'ACP agents are loosely coupled. Direct = tightly coupled.':<{width - 30}}│"
    )
    print(f"  └{border}┘")


def _print_architecture_diagram() -> None:
    """
    Print the ContentForge ASCII architecture diagram.

    Shows the relationship between ACP components (bus, agents, registry)
    and MCP components (server, client, tools) in a single visual.
    """
    print("""
  ┌─────────────────────────────────────────────────────────────────────┐
  │               CONTENTFORGE ARCHITECTURE                              │
  ├─────────────────────────────────────────────────────────────────────┤
  │                                                                       │
  │                      ACP MESSAGE BUS                                  │
  │                      (Central Hub)                                    │
  │                           │                                           │
  │         ┌─────────────────┼─────────────────────┐                    │
  │         │                 │                     │                     │
  │  [ResearcherAgent]  [WriterAgent]        [EditorAgent]               │
  │  Stage 1: Research  Stage 2: Draft       Stage 3: Edit               │
  │         │                 │                     │                     │
  │         └─────────────────┼─────────────────────┘                    │
  │                           │                                           │
  │                  [SEOAgent]   [PublisherAgent]                        │
  │                 Stage 4: SEO  Stage 5: Publish                        │
  │                                                                       │
  │  All agents also connect to:                                          │
  │                                                                       │
  │                      MCP CLIENT                                       │
  │                          │                                            │
  │                      MCP SERVER (Tools)                               │
  │                      ├── search_topic                                 │
  │                      ├── get_style_guide                              │
  │                      ├── get_seo_keywords                             │
  │                      ├── save_draft                                   │
  │                      ├── publish_article                              │
  │                      ├── check_readability                            │
  │                      └── grammar_check                                │
  │                                                                       │
  │  ACP Registry (Service Discovery):                                    │
  │    ResearcherAgent | WriterAgent | EditorAgent | SEOAgent | Publisher │
  └─────────────────────────────────────────────────────────────────────┘
  """)


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================


def main() -> None:
    """
    ContentForge main entry point.

    Runs the complete multi-agent content creation pipeline for two topics:
      1. "artificial intelligence"  (demonstrates the pipeline end-to-end)
      2. "space exploration"        (demonstrates re-usability for any topic)

    The MCP server is created ONCE and reused for both runs.
    The ACP bus and all agents are created fresh for each run to ensure
    clean message histories.

    Educational design: every step of both runs is accompanied by detailed
    console output explaining WHAT the protocol is doing and WHY.
    This makes ContentForge suitable as a learning resource for ACP and MCP.
    """
    # ---- Print startup banners -----------------------------------------
    print("\n" + "█" * 72)
    print("█" + " " * 70 + "█")
    print(
        "█"
        + "  CONTENTFORGE — Multi-Agent Content Creation Pipeline  ".center(70)
        + "█"
    )
    print(
        "█"
        + "  Powered by ACP (Agent Communication Protocol)         ".center(70)
        + "█"
    )
    print(
        "█"
        + "  and MCP (Model Context Protocol)                      ".center(70)
        + "█"
    )
    print("█" + " " * 70 + "█")
    print("█" * 72)

    _print_acp_education_block()
    _print_architecture_diagram()

    # ---- Create the shared MCP server (loaded once, reused per run) ----
    # ACP CONCEPT: Infrastructure vs. Pipeline
    # -----------------------------------------
    # The MCPServer is INFRASTRUCTURE — it exists independently of any
    # specific pipeline run.  The ACPMessageBus and agents are PIPELINE
    # RUNTIME — created fresh for each run to ensure isolation.
    #
    # This mirrors real architectures:
    #   Infrastructure: Kubernetes cluster, RabbitMQ, a database
    #   Pipeline runtime: individual task instances, worker processes
    print("\n\n" + "=" * 72)
    print("  CREATING SHARED MCP SERVER (used for all pipeline runs)")
    print("=" * 72)
    print(
        "\n  MCP CONCEPT: The MCPServer is created once and shared across\n"
        "  all pipeline runs.  It loads data files once at startup and\n"
        "  serves tool requests from any agent in any pipeline run.\n"
        "  This is the 'stateless server' pattern — the server holds data\n"
        "  (knowledge base, style guides, keywords) but no run-specific state.\n"
    )
    shared_mcp_server = MCPServer()
    print(
        f"\n  Shared MCP Server ready with {len(shared_mcp_server.list_tools())} tools."
    )

    # ---- PIPELINE RUN 1: Artificial Intelligence -------------------------
    print("\n\n" + "█" * 72)
    print("█" + "  PIPELINE RUN 1 OF 2  —  Artificial Intelligence  ".center(70) + "█")
    print("█" * 72)

    publisher1 = run_pipeline(
        topic="artificial intelligence",
        content_type="blog",
        mcp_server=shared_mcp_server,
        verbose_mcp=True,
    )

    # Brief pause between runs for readability
    print("\n\n  [ContentForge] Run 1 complete. Pausing before Run 2...\n")
    time.sleep(0.5)

    # ---- PIPELINE RUN 2: Space Exploration --------------------------------
    # ACP CONCEPT: Pipeline Reusability
    # -----------------------------------
    # The SAME five agents (with fresh instances), the SAME MCP server,
    # the SAME protocols — but a completely different topic.
    # This demonstrates that ContentForge is a GENERAL-PURPOSE pipeline,
    # not hard-coded to any specific subject matter.
    print("\n\n" + "█" * 72)
    print("█" + "  PIPELINE RUN 2 OF 2  —  Space Exploration  ".center(70) + "█")
    print("█" * 72)
    print(
        "\n  ACP CONCEPT: We create a completely fresh ACP bus and new agent\n"
        "  instances for Run 2.  This isolation ensures Run 1's message\n"
        "  history doesn't contaminate Run 2's observable state.\n"
        "  The MCP server is reused (it's stateless — safe to share).\n"
        "  This mirrors production patterns: new task instances per job,\n"
        "  shared infrastructure (message broker, tool server) across jobs.\n"
    )

    publisher2 = run_pipeline(
        topic="space exploration",
        content_type="blog",
        mcp_server=shared_mcp_server,
        verbose_mcp=True,
    )

    # ---- FINAL SESSION SUMMARY ------------------------------------------
    print("\n\n" + "█" * 72)
    print("█" + "  CONTENTFORGE SESSION COMPLETE  ".center(70) + "█")
    print("█" * 72)

    # The PublisherAgent tracks all publications in its session
    publisher1.print_session_summary()

    # publisher2 only published one article (its own run)
    # Let's show a combined summary
    print("\n  ── MCP Server: Aggregate Tool Usage (Both Runs) ────────────────")
    print(
        "  MCP CONCEPT: Because both runs shared the same MCPServer, we can\n"
        "  see the TOTAL tool usage across the entire session.\n"
        "  This is the 'server-side observability' that MCP enables:\n"
        "  one place to see ALL tool calls from ALL agents in ALL runs.\n"
    )
    shared_mcp_server.print_usage_stats()

    # ---- Final educational recap ----------------------------------------
    print("\n\n" + "=" * 72)
    print("  KEY TAKEAWAYS: What ContentForge Demonstrates")
    print("=" * 72)

    takeaways = [
        (
            "ACP Message Bus",
            "All 5 agents communicate ONLY through the bus. Zero direct agent-to-agent calls. "
            "Adding a 6th agent (e.g. a SocialMediaAgent) requires zero changes to existing agents.",
        ),
        (
            "ACP Message Envelopes",
            "Every message carries: sender_id, receiver_id, message_type, correlation_id, timestamp. "
            "The bus can log, route, and audit without ever reading the payload.",
        ),
        (
            "ACP Correlation Chains",
            "Every RESPONSE carries the REQUEST's message_id. The full pipeline can be traced "
            "from the PublisherAgent's broadcast back to the original research REQUEST.",
        ),
        (
            "ACP Agent Registry",
            "Agents self-document their input/output schemas. The orchestrator discovers capabilities "
            "at runtime without importing agent classes — true service discovery.",
        ),
        (
            "ACP Broadcast Pattern",
            "The PublisherAgent's final BROADCAST reaches all agents simultaneously. "
            "Future consumers (social media, analytics) subscribe without publisher changes.",
        ),
        (
            "MCP Tool Abstraction",
            "Agents call tools BY NAME. They don't know whether data comes from a JSON file, "
            "a database, or a live API. Changing the data source = change only MCPServer.",
        ),
        (
            "MCP Tool Discovery",
            "Agents can call mcp.list_tools() to discover capabilities at runtime. "
            "This enables dynamic, self-adapting agent behaviour.",
        ),
        (
            "ACP vs A2A",
            "ACP uses a message BUS (pub/sub, decoupled). "
            "A2A uses direct client-server HTTP calls (coupled). "
            "ACP trades some performance for much higher flexibility and observability.",
        ),
    ]

    for i, (concept, explanation) in enumerate(takeaways, 1):
        print(f"\n  [{i}] {concept}")
        # Word-wrap the explanation at 65 chars
        words = explanation.split()
        line = "      "
        for word in words:
            if len(line) + len(word) + 1 > 70:
                print(line)
                line = "      " + word + " "
            else:
                line += word + " "
        if line.strip():
            print(line)

    print("\n\n" + "=" * 72)
    print("  Published articles are in: Agent/project4_contentforge/data/published/")
    print("  Each article has a .md file (with YAML front matter) and a")
    print("  _meta.json file (complete provenance: research, edits, SEO, pipeline).")
    print("=" * 72)
    print("\n  ContentForge session ended.\n")


# =============================================================================
# SCRIPT ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    main()
