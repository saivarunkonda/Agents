# =============================================================================
# agents/researcher_agent.py  —  ResearcherAgent
# =============================================================================
#
# ROLE IN THE PIPELINE
# --------------------
# The ResearcherAgent is Stage 1 of the ContentForge pipeline.  It is the
# first agent to act when a content request arrives, and its job is to
# transform a bare topic string ("artificial intelligence") into a rich,
# structured research package that the WriterAgent can use to draft a
# compelling, fact-filled article.
#
# WHAT IT DOES
# ------------
#   1. Receives a REQUEST message from main.py (or an orchestrator) with
#      payload: { "intent": "research_topic", "topic": "...", "content_type": "..." }
#
#   2. Uses MCP tool "search_topic" to fetch:
#        - Key facts about the topic
#        - Notable people / experts in the field
#        - Recent developments / news
#        - Statistics and market data
#        - Related subtopics
#
#   3. Uses MCP tool "get_seo_keywords" to fetch:
#        - Primary keywords (high search volume)
#        - Secondary keywords (supporting terms)
#        - Long-tail keyword phrases
#        - Search volume data
#
#   4. Synthesises these into a "research brief" and sends it via the ACP
#      bus as a RESPONSE to the WriterAgent.
#
# ACP FLOW
# --------
#   [main.py / Orchestrator]
#         │  REQUEST  { intent: "research_topic", topic: "AI", ... }
#         ▼
#   [ResearcherAgent]  ──MCP──▶  search_topic("AI")
#                      ──MCP──▶  get_seo_keywords("AI")
#         │  RESPONSE { topic, facts, key_people, statistics,
#         │             recent_developments, seo_hints }
#         ▼
#   [WriterAgent]
#
# MCP TOOLS USED
# --------------
#   search_topic(topic)        → knowledge base lookup
#   get_seo_keywords(topic)    → keyword database lookup
#
# ACP MESSAGE TYPES
# -----------------
#   Receives : ACPMessageType.REQUEST
#   Sends    : ACPMessageType.RESPONSE  (to "writer")
#              ACPMessageType.ERROR     (to sender, on failure)
# =============================================================================

from typing import Any, Dict, List, Optional

from acp.agent_registry import ACPAgentInfo, ACPAgentRegistry
from acp.message import ACPContentType, ACPMessage, ACPMessageType
from acp.message_bus import ACPMessageBus
from mcp.mcp_client import MCPClient

from agents.base_agent import BaseACPAgent


class ResearcherAgent(BaseACPAgent):
    """
    ResearcherAgent — Stage 1 of the ContentForge pipeline.

    Responsible for gathering all the raw material (facts, statistics,
    expert names, recent news, SEO keywords) that the downstream agents
    need to produce a high-quality, well-researched article.

    ACP CONCEPT: Single Responsibility Agents
    ------------------------------------------
    The ResearcherAgent does ONE thing: research.  It does not write,
    edit, or publish.  This strict single-responsibility design is
    fundamental to ACP's composability promise — you can swap in a
    different researcher (e.g. one that calls a live web API instead of
    a local knowledge base) without touching any other agent.

    MCP CONCEPT: Research as a Tool-Driven Process
    ------------------------------------------------
    The ResearcherAgent does not hard-code any knowledge.  Every fact
    it produces comes from MCP tool calls.  This means:
      - The knowledge base can be updated independently of the agent code
      - The same agent code works for any topic, any knowledge source
      - Tool calls are logged and auditable (who requested what data)

    Parameters
    ----------
    bus : ACPMessageBus
        The shared ACP message bus for this pipeline run.
    mcp_client : MCPClient
        Pre-configured MCP client for tool invocations.
    registry : ACPAgentRegistry, optional
        Shared agent registry for status updates.
    """

    # The agent_id used to address this agent on the ACP Message Bus.
    # Any agent that wants to send work to the ResearcherAgent sets
    # receiver_id = ResearcherAgent.AGENT_ID in their ACPMessage.
    AGENT_ID: str = "researcher"

    def __init__(
        self,
        bus: ACPMessageBus,
        mcp_client: MCPClient,
        registry: Optional[ACPAgentRegistry] = None,
    ) -> None:
        super().__init__(
            agent_id=self.AGENT_ID,
            name="ResearcherAgent",
            bus=bus,
            mcp_client=mcp_client,
            registry=registry,
        )
        self._log(
            "ResearcherAgent ready.  Accepts REQUEST messages with "
            "intent='research_topic'.  Will forward research brief to WriterAgent."
        )

    # ==========================================================================
    # ACP MESSAGE DISPATCH
    # ==========================================================================

    def _dispatch(self, message: ACPMessage) -> None:
        """
        Route an incoming ACP message to the appropriate handler.

        ACP CONCEPT: Intent-Based Dispatch
        ------------------------------------
        A single agent may receive different types of requests over its
        lifetime.  Rather than one monolithic handler, we inspect the
        message's payload["intent"] field and dispatch to a specific
        private method.  This keeps each handler focused and testable.

        Understood intents:
          "research_topic" → _handle_research_request()

        Unknown intents are logged as warnings but do NOT crash the agent.
        In a production system, unknown intents might be forwarded to a
        "catch-all" handler or logged as metrics for protocol drift detection.

        Parameters
        ----------
        message : ACPMessage
            The incoming message to dispatch.
        """
        # ---- Guard: we only process REQUEST messages ----------------
        # A ResearcherAgent might receive BROADCAST messages (e.g. the
        # PublisherAgent broadcasting "article published!").  We gracefully
        # ignore anything that isn't a REQUEST directed at us.
        if message.message_type == ACPMessageType.BROADCAST:
            self._log(
                f"Received BROADCAST from '{message.sender_id}' — "
                f"ResearcherAgent has no action for broadcasts. Ignoring."
            )
            return  # no status change needed — stay idle

        if message.message_type == ACPMessageType.ACK:
            self._log(f"Received ACK from '{message.sender_id}' — acknowledged.")
            return

        if message.message_type != ACPMessageType.REQUEST:
            self._log(
                f"Unexpected message type '{message.message_type.value}' "
                f"from '{message.sender_id}'. ResearcherAgent only handles REQUESTs."
            )
            return

        # ---- Extract intent from payload ----------------------------
        payload: Dict[str, Any] = message.payload or {}
        intent: str = payload.get("intent", "").lower().strip()

        self._log(
            f"Dispatching REQUEST with intent='{intent}' from '{message.sender_id}'"
        )

        if intent == "research_topic":
            self._handle_research_request(message, payload)
        else:
            self._log(
                f"Unknown intent '{intent}'. "
                f"Supported intents: ['research_topic']. "
                f"Sending ERROR back to '{message.sender_id}'."
            )
            self.send(
                receiver_id=message.sender_id,
                payload={
                    "error": f"Unknown intent '{intent}'",
                    "supported_intents": ["research_topic"],
                    "agent_id": self.agent_id,
                },
                msg_type=ACPMessageType.ERROR,
                correlation_id=message.message_id,
            )

    # ==========================================================================
    # RESEARCH REQUEST HANDLER
    # ==========================================================================

    def _handle_research_request(
        self,
        message: ACPMessage,
        payload: Dict[str, Any],
    ) -> None:
        """
        Handle a "research_topic" REQUEST.

        This is the main work method of the ResearcherAgent.  It:
          1. Extracts the topic and content_type from the payload
          2. Calls MCP search_topic to get knowledge-base data
          3. Calls MCP get_seo_keywords to get keyword intelligence
          4. Synthesises a research brief
          5. Sends the brief to the WriterAgent via the ACP bus

        ACP CONCEPT: Request/Response Correlation
        -------------------------------------------
        Notice that the RESPONSE message we send to the WriterAgent carries
        correlation_id = message.message_id (the incoming REQUEST's ID).
        This creates a traceable chain in the ACP message history:

            REQUEST  id=abc123    (orchestrator → researcher)
               ↳  RESPONSE id=def456, corr=abc123  (researcher → writer)

        Anyone reading the message log can instantly see: "the writer
        received this data BECAUSE of the abc123 research request."

        Parameters
        ----------
        message : ACPMessage
            The original REQUEST message (used for correlation_id).
        payload : dict
            The message payload containing 'topic' and 'content_type'.
        """
        # ---- Extract inputs -----------------------------------------
        topic: str = payload.get("topic", "").strip()
        content_type: str = payload.get("content_type", "blog").strip()

        if not topic:
            self._log("ERROR: 'topic' field is missing or empty in payload.")
            self.send(
                receiver_id=message.sender_id,
                payload={
                    "error": "Missing required field 'topic' in request payload.",
                    "received_payload": payload,
                },
                msg_type=ACPMessageType.ERROR,
                correlation_id=message.message_id,
            )
            self.mark_error("Missing 'topic' in payload")
            return

        self._log_section(f"ResearcherAgent — Researching: '{topic}'")
        self._log(
            f"Starting research pipeline for topic='{topic}', "
            f"content_type='{content_type}'"
        )
        self._log(
            "ACP CONCEPT: This agent uses MCP tools to gather data. "
            "All tool calls flow through MCPClient → MCPServer. "
            "The agent never touches files or APIs directly."
        )

        # ---- Step 1: Knowledge Base Search --------------------------
        self._log(f"Step 1/3: Calling MCP tool 'search_topic' for '{topic}'")
        self._log(
            "MCP CONCEPT: 'search_topic' is a registered tool on the MCP server. "
            "The agent calls it by NAME — it doesn't know or care whether the "
            "data comes from a JSON file, a vector DB, or a web API."
        )

        knowledge_response = self.mcp.call_tool("search_topic", topic=topic)

        if not knowledge_response["success"]:
            self._log(
                f"MCP tool 'search_topic' failed: {knowledge_response['error']}. "
                f"Proceeding with minimal research data."
            )
            knowledge_data: Dict[str, Any] = {
                "topic": topic,
                "found": False,
                "facts": [f"General topic: {topic}"],
                "key_people": [],
                "recent_developments": [],
                "statistics": {},
                "subtopics": [],
            }
        else:
            knowledge_data = knowledge_response["result"]
            found_status = (
                "✓ found in knowledge base"
                if knowledge_data.get("found")
                else "⚠ not found, using fallback"
            )
            self._log(
                f"Knowledge base search complete ({found_status}). "
                f"Retrieved {len(knowledge_data.get('facts', []))} facts, "
                f"{len(knowledge_data.get('key_people', []))} key people, "
                f"{len(knowledge_data.get('recent_developments', []))} developments."
            )

        # ---- Step 2: SEO Keyword Research ---------------------------
        self._log(f"Step 2/3: Calling MCP tool 'get_seo_keywords' for '{topic}'")
        self._log(
            "MCP CONCEPT: SEO data is a separate tool from the knowledge base. "
            "MCP's tool-per-concern design means each tool can be improved, "
            "replaced, or scaled independently."
        )

        seo_response = self.mcp.call_tool("get_seo_keywords", topic=topic)

        if not seo_response["success"]:
            self._log(
                f"MCP tool 'get_seo_keywords' failed: {seo_response['error']}. "
                f"Proceeding without SEO keyword hints."
            )
            seo_data: Dict[str, Any] = {
                "topic": topic,
                "found": False,
                "primary": [topic],
                "secondary": [],
                "long_tail": [],
                "search_volume": {},
                "related_topics": [],
            }
        else:
            seo_data = seo_response["result"]
            self._log(
                f"SEO keyword research complete. "
                f"Primary: {seo_data.get('primary', [])[:3]}, "
                f"Secondary: {len(seo_data.get('secondary', []))} terms, "
                f"Long-tail: {len(seo_data.get('long_tail', []))} phrases."
            )

        # ---- Step 3: Synthesise Research Brief ----------------------
        self._log("Step 3/3: Synthesising research brief from gathered data")

        research_brief = self._synthesise_brief(
            topic=topic,
            content_type=content_type,
            knowledge=knowledge_data,
            seo=seo_data,
            original_message_id=message.message_id,
        )

        self._log(
            f"Research brief assembled. "
            f"Facts: {len(research_brief['facts'])}, "
            f"Key people: {len(research_brief['key_people'])}, "
            f"Developments: {len(research_brief['recent_developments'])}, "
            f"SEO primary keywords: {research_brief['seo_hints']['primary_keywords'][:2]}"
        )

        # ---- Step 4: Send to WriterAgent via ACP Bus ----------------
        self._log(
            "Step 4/3: Sending research brief to WriterAgent via ACP bus.\n"
            "  ACP CONCEPT: The ResearcherAgent does NOT import WriterAgent.\n"
            "  It only knows the string 'writer' — the bus handles delivery.\n"
            "  This is loose coupling: ResearcherAgent could be reused in a\n"
            "  completely different pipeline with a different downstream agent."
        )

        sent_msg = self.send(
            receiver_id="writer",  # ACP address of the WriterAgent
            payload=research_brief,
            msg_type=ACPMessageType.RESPONSE,
            content_type=ACPContentType.JSON,
            correlation_id=message.message_id,  # link back to the original REQUEST
            metadata={
                "pipeline_stage": 1,
                "topic": topic,
                "content_type": content_type,
            },
        )

        self._log(
            f"Research brief sent to WriterAgent "
            f"(ACP msg id: {sent_msg.message_id}, "
            f"corr: {sent_msg.correlation_id}). "
            f"ResearcherAgent's work is done."
        )

        self._log_section_end()
        self.mark_done()

    # ==========================================================================
    # SYNTHESIS HELPERS
    # ==========================================================================

    def _synthesise_brief(
        self,
        topic: str,
        content_type: str,
        knowledge: Dict[str, Any],
        seo: Dict[str, Any],
        original_message_id: str,
    ) -> Dict[str, Any]:
        """
        Combine raw knowledge-base data and SEO keyword data into a
        structured research brief ready for the WriterAgent.

        The brief is the "output contract" of the ResearcherAgent.
        The WriterAgent depends on this exact structure, so any changes
        to the brief schema must be coordinated with the WriterAgent's
        input handling.

        In a real ACP system, this schema would be published in the
        ACPAgentRegistry's output_schema field so the WriterAgent can
        validate it at runtime using registry.get_output_schema("researcher").

        Parameters
        ----------
        topic : str
            The research topic.
        content_type : str
            Target content format (affects writing guidance included in brief).
        knowledge : dict
            Raw output from the search_topic MCP tool.
        seo : dict
            Raw output from the get_seo_keywords MCP tool.
        original_message_id : str
            The message_id of the originating REQUEST, included in the brief
            for full end-to-end traceability.

        Returns
        -------
        dict
            A structured research brief with all data the WriterAgent needs.
        """
        # Select the top facts (limit to avoid overwhelming the writer)
        facts: List[str] = knowledge.get("facts", [])
        # If we have many facts, prioritise the most impactful ones
        # In production this might use an LLM to rank facts by relevance
        top_facts = facts[:6] if len(facts) > 6 else facts

        # Extract key people — these can be quoted or referenced in the article
        key_people: List[str] = knowledge.get("key_people", [])

        # Recent developments — these give the article recency and newsworthiness
        recent_developments: List[str] = knowledge.get("recent_developments", [])

        # Statistics — concrete numbers make articles more credible
        statistics: Dict[str, str] = knowledge.get("statistics", {})

        # Subtopics — the writer can use these for section headings
        subtopics: List[str] = knowledge.get("subtopics", [])

        # Build the SEO hints section — used by WriterAgent and later by SEOAgent
        seo_hints: Dict[str, Any] = {
            "primary_keywords": seo.get("primary", []),
            "secondary_keywords": seo.get("secondary", []),
            "long_tail_phrases": seo.get("long_tail", []),
            "top_search_terms": self._get_top_search_terms(
                seo.get("search_volume", {})
            ),
            "related_topics": seo.get("related_topics", []),
        }

        # Writing guidance derived from research
        # (helps the WriterAgent even before it fetches the style guide)
        writing_guidance: Dict[str, Any] = {
            "suggested_angle": self._suggest_angle(topic, content_type, facts),
            "suggested_sections": self._suggest_sections(
                topic, subtopics, content_type
            ),
            "key_messages": self._extract_key_messages(facts, statistics),
            "content_type": content_type,
        }

        return {
            # ── Core research data (from MCP search_topic) ────────────────
            "topic": topic,
            "facts": top_facts,
            "key_people": key_people,
            "recent_developments": recent_developments,
            "statistics": statistics,
            "subtopics": subtopics,
            "all_facts": facts,  # full list for agents that want more depth
            # ── SEO intelligence (from MCP get_seo_keywords) ─────────────
            "seo_hints": seo_hints,
            # ── Writing guidance synthesised by the researcher ────────────
            "writing_guidance": writing_guidance,
            # ── Protocol metadata ─────────────────────────────────────────
            "content_type": content_type,
            "source_message_id": original_message_id,  # traceability
            "researched_by": self.agent_id,
        }

    @staticmethod
    def _get_top_search_terms(
        search_volume: Dict[str, int], top_n: int = 3
    ) -> List[str]:
        """
        Return the top-N highest-volume search terms from the keyword data.

        Parameters
        ----------
        search_volume : dict
            Maps keyword → monthly search volume integer.
        top_n : int
            How many top terms to return.

        Returns
        -------
        List[str]
            Keywords sorted by search volume, descending.
        """
        if not search_volume:
            return []
        sorted_terms = sorted(search_volume.items(), key=lambda x: x[1], reverse=True)
        return [term for term, _ in sorted_terms[:top_n]]

    @staticmethod
    def _suggest_angle(topic: str, content_type: str, facts: List[str]) -> str:
        """
        Suggest a writing angle / hook for the article based on the research.

        This simulates the kind of editorial judgement a real researcher
        would exercise: "Given these facts, what's the most interesting
        angle for a blog post vs a technical article?"

        In production this would use an LLM with the facts as context to
        generate a genuinely creative angle.  Here we use simple heuristics
        to illustrate the concept.

        Parameters
        ----------
        topic : str
            The research topic.
        content_type : str
            "blog", "technical", or "news".
        facts : list of str
            The researched facts.

        Returns
        -------
        str
            A suggested narrative angle for the article.
        """
        # Look for facts containing numbers/statistics — these make great hooks
        stat_facts = [f for f in facts if any(c.isdigit() for c in f)]

        if content_type == "blog":
            if stat_facts:
                # Use a striking statistic as the hook
                hook_fact = stat_facts[0]
                return (
                    f"Open with the surprising statistic: '{hook_fact}' — "
                    f"then explain what it means for the average reader. "
                    f"Frame {topic} as something that affects them personally."
                )
            else:
                return (
                    f"Frame {topic} through the lens of its real-world impact. "
                    f"Start with a relatable scenario, then zoom out to the bigger picture."
                )
        elif content_type == "technical":
            return (
                f"Lead with the technical problem that {topic} solves. "
                f"Present a structured breakdown of current capabilities, "
                f"methodologies, and future directions. Use data to support claims."
            )
        else:  # news
            if facts:
                latest = facts[0]
                return (
                    f"Lead with the most recent development: '{latest}'. "
                    f"Follow with context and expert perspectives."
                )
            return f"Cover the latest developments in {topic} with factual precision."

    @staticmethod
    def _suggest_sections(
        topic: str, subtopics: List[str], content_type: str
    ) -> List[str]:
        """
        Suggest article section headings based on the topic's subtopics.

        These become the WriterAgent's structural blueprint, helping it
        organise the article logically before it has fetched the style guide.

        Parameters
        ----------
        topic : str
            Main article topic.
        subtopics : list of str
            Related subtopics from the knowledge base.
        content_type : str
            Target content format.

        Returns
        -------
        List[str]
            Suggested section headings in order.
        """
        title_topic = topic.title()

        if content_type == "blog":
            base_sections = [
                f"What Is {title_topic} and Why It Matters",
                f"Key Developments Shaping {title_topic} Today",
                f"Real-World Impact: How {title_topic} Affects You",
                f"What Experts Are Saying",
                f"The Future of {title_topic}",
                "Final Thoughts and Next Steps",
            ]
        elif content_type == "technical":
            base_sections = [
                "Abstract",
                f"Introduction to {title_topic}",
                "Current State and Methodologies",
                "Key Findings and Data Analysis",
                "Applications and Use Cases",
                "Conclusion and Future Directions",
                "References",
            ]
        else:  # news
            base_sections = [
                f"{title_topic}: Breaking Developments",
                "Background and Context",
                "Expert Perspectives",
                "What This Means Going Forward",
            ]

        # Inject up to 2 subtopic-based sections if available
        # (placed in the middle of the article, after the intro)
        if subtopics:
            insert_pos = 2
            for subtopic in subtopics[:2]:
                section = f"Deep Dive: {subtopic.title()}"
                base_sections.insert(insert_pos, section)
                insert_pos += 1

        return base_sections

    @staticmethod
    def _extract_key_messages(
        facts: List[str], statistics: Dict[str, str]
    ) -> List[str]:
        """
        Distil the most impactful 'key messages' from the facts and statistics.

        Key messages are the 3-4 core points the article must convey.
        The writer uses these to ensure the article stays focused and doesn't
        drift into irrelevant territory.

        Parameters
        ----------
        facts : list of str
            Research facts.
        statistics : dict
            Numeric statistics from the knowledge base.

        Returns
        -------
        List[str]
            Top 4 key messages for the article.
        """
        messages: List[str] = []

        # Include facts that contain quantifiable claims (most persuasive)
        for fact in facts:
            if any(c.isdigit() for c in fact) and len(messages) < 2:
                messages.append(fact)

        # Include non-numeric facts if we need more
        for fact in facts:
            if fact not in messages and len(messages) < 3:
                messages.append(fact)

        # Add a key statistic as the final message if available
        if statistics and len(messages) < 4:
            key_stat = next(iter(statistics.items()))  # first entry
            messages.append(f"{key_stat[0].replace('_', ' ').title()}: {key_stat[1]}")

        # Always return at least one message
        if not messages and facts:
            messages = facts[:1]
        elif not messages:
            messages = ["This topic is emerging and rapidly evolving."]

        return messages[:4]

    # ==========================================================================
    # REGISTRY METADATA
    # ==========================================================================

    def get_agent_info(self) -> ACPAgentInfo:
        """
        Return ACPAgentInfo describing this agent for the registry.

        This is called by main.py to register the agent before the pipeline
        starts.  The input_schema documents what payload this agent expects
        to receive; the output_schema documents what it produces.

        ACP CONCEPT: Self-Describing Agents
        -------------------------------------
        By declaring these schemas in the registry, any pipeline component
        (orchestrator, monitoring tool, another agent) can inspect the
        ResearcherAgent's contract without importing it or reading its source.

        This is the ACP equivalent of OpenAPI's requestBody and responses
        schemas — a machine-readable description of the agent's interface.

        Returns
        -------
        ACPAgentInfo
            Full registry record for this agent.
        """
        return ACPAgentInfo(
            agent_id=self.AGENT_ID,
            name="ResearcherAgent",
            description=(
                "Stage 1: Researches the given topic using the MCP knowledge base "
                "and SEO keyword tools. Produces a structured research brief "
                "containing facts, key people, statistics, and keyword hints. "
                "Forwards the brief to the WriterAgent via the ACP bus."
            ),
            input_schema={
                "intent": "str — must be 'research_topic'",
                "topic": "str — the subject to research (e.g. 'artificial intelligence')",
                "content_type": "str — target format: 'blog' | 'technical' | 'news'",
            },
            output_schema={
                "topic": "str — the researched topic",
                "facts": "list[str] — top facts about the topic",
                "key_people": "list[str] — notable people / experts in the field",
                "recent_developments": "list[str] — latest news and developments",
                "statistics": "dict[str, str] — key statistics with labels",
                "subtopics": "list[str] — related subtopics for article structure",
                "seo_hints": "dict — primary/secondary/long-tail keywords + volumes",
                "writing_guidance": "dict — suggested angle, sections, key messages",
                "content_type": "str — target format, passed through to WriterAgent",
            },
            status=self._status,
            pipeline_stage=1,
            tags=["research", "fact-gathering", "seo", "stage-1"],
            metadata={
                "mcp_tools_used": ["search_topic", "get_seo_keywords"],
                "sends_to": ["writer"],
                "receives_from": ["orchestrator", "main"],
            },
        )
