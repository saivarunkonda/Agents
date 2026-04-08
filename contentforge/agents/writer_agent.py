# =============================================================================
# agents/writer_agent.py  —  WriterAgent
# =============================================================================
#
# ROLE IN THE PIPELINE
# --------------------
# The WriterAgent is Stage 2 of the ContentForge pipeline.  It receives the
# structured research brief assembled by the ResearcherAgent and transforms
# it into a complete, well-structured article draft.
#
# This is the "creative" stage of the pipeline — taking raw facts and turning
# them into readable, engaging prose that follows the requested style guide.
#
# WHAT IT DOES
# ------------
#   1. Receives a RESPONSE message from ResearcherAgent with payload:
#      { topic, facts, key_people, statistics, recent_developments,
#        seo_hints, writing_guidance, content_type }
#
#   2. Uses MCP tool "get_style_guide" to fetch writing guidelines:
#        - Tone (conversational, technical, news)
#        - Structure (which sections to include)
#        - Target word count and paragraph length
#        - Writing tips and anti-patterns to avoid
#
#   3. Composes a full article draft:
#        - Title (keyword-optimised, engaging)
#        - Introduction / hook paragraph
#        - 3+ body sections with subheadings
#        - Statistics and expert references woven in naturally
#        - Conclusion with call-to-action
#
#   4. Uses MCP tool "save_draft" to persist the work-in-progress
#
#   5. Sends the draft to EditorAgent via the ACP bus
#
# ACP FLOW
# --------
#   [ResearcherAgent]
#         │  RESPONSE  { topic, facts, key_people, statistics, seo_hints, ... }
#         ▼
#   [WriterAgent]  ──MCP──▶  get_style_guide("blog")
#                  ──MCP──▶  save_draft(title, content)
#         │  RESPONSE  { title, draft_content, style_used, word_count,
#         │              research_brief (passed through) }
#         ▼
#   [EditorAgent]
#
# MCP TOOLS USED
# --------------
#   get_style_guide(content_type)   → writing style guidelines
#   save_draft(title, content)      → persists the draft to temp storage
#
# ACP MESSAGE TYPES
# -----------------
#   Receives : ACPMessageType.RESPONSE  (from "researcher")
#              ACPMessageType.BROADCAST (ignored gracefully)
#   Sends    : ACPMessageType.RESPONSE  (to "editor")
#              ACPMessageType.ERROR     (to sender, on failure)
# =============================================================================

import textwrap
from typing import Any, Dict, List, Optional

from acp.agent_registry import ACPAgentInfo, ACPAgentRegistry
from acp.message import ACPContentType, ACPMessage, ACPMessageType
from acp.message_bus import ACPMessageBus
from mcp.mcp_client import MCPClient

from agents.base_agent import BaseACPAgent


class WriterAgent(BaseACPAgent):
    """
    WriterAgent — Stage 2 of the ContentForge pipeline.

    Responsible for converting a structured research brief into a complete,
    publication-ready article draft.  The draft will be reviewed and improved
    by the EditorAgent in Stage 3.

    ACP CONCEPT: Data Transformation Agents
    -----------------------------------------
    The WriterAgent's role is to TRANSFORM data: it takes structured JSON
    (the research brief) and produces Markdown prose (the article draft).
    This transformation is the WriterAgent's sole responsibility — it does
    not research, edit, or publish.

    This strict separation means:
      - The WriterAgent can be tested independently with mock research data
      - The writing style can be changed by updating the MCP style guide
        without touching any agent code
      - A different writing agent (e.g. one using an LLM API) can be
        swapped in by simply replacing this class

    MCP CONCEPT: Style-Guide-Driven Writing
    -----------------------------------------
    Instead of hardcoding tone, structure, and word count targets, the
    WriterAgent fetches its "writing instructions" from the MCP server via
    the get_style_guide tool.  This means:
      - Content format can be changed at runtime (just pass content_type="technical")
      - The editorial team can update style guides in data/style_guides.json
        without touching any agent code
      - The same WriterAgent produces blogs, technical docs, or news articles
        depending on which style guide is loaded

    Parameters
    ----------
    bus : ACPMessageBus
        The shared ACP message bus for this pipeline run.
    mcp_client : MCPClient
        Pre-configured MCP client for tool invocations.
    registry : ACPAgentRegistry, optional
        Shared agent registry for status updates.
    """

    AGENT_ID: str = "writer"

    def __init__(
        self,
        bus: ACPMessageBus,
        mcp_client: MCPClient,
        registry: Optional[ACPAgentRegistry] = None,
    ) -> None:
        super().__init__(
            agent_id=self.AGENT_ID,
            name="WriterAgent",
            bus=bus,
            mcp_client=mcp_client,
            registry=registry,
        )
        self._log(
            "WriterAgent ready.  Accepts RESPONSE messages from ResearcherAgent. "
            "Will produce a full article draft and forward to EditorAgent."
        )

    # ==========================================================================
    # ACP MESSAGE DISPATCH
    # ==========================================================================

    def _dispatch(self, message: ACPMessage) -> None:
        """
        Route an incoming ACP message to the appropriate handler.

        ACP CONCEPT: Selective Processing
        -----------------------------------
        The WriterAgent is subscribed to the bus, which means it receives
        ALL messages published on the bus — including broadcasts from the
        PublisherAgent at the end of the pipeline.

        Well-behaved ACP agents gracefully ignore messages they have no
        role in processing, rather than throwing errors.  This is important
        for broadcast messages in particular — every agent will receive them,
        but only interested agents should act.

        Parameters
        ----------
        message : ACPMessage
            The incoming ACP message to evaluate and optionally handle.
        """
        # Silently ignore broadcasts — PublisherAgent broadcasts "article published"
        # to all agents, including us, but we have nothing to do with it.
        if message.message_type == ACPMessageType.BROADCAST:
            self._log(
                f"Received BROADCAST from '{message.sender_id}' — "
                f"WriterAgent has no action for broadcasts. Ignoring."
            )
            return

        if message.message_type == ACPMessageType.ACK:
            self._log(f"Received ACK from '{message.sender_id}' — acknowledged.")
            return

        if message.message_type == ACPMessageType.ERROR:
            self._log(
                f"Received ERROR message from '{message.sender_id}': "
                f"{message.payload}. WriterAgent cannot proceed without "
                f"research data."
            )
            self.mark_error(f"Upstream error from {message.sender_id}")
            return

        # The WriterAgent expects a RESPONSE from the ResearcherAgent.
        # In a stricter implementation we might also check message.sender_id == "researcher",
        # but checking message_type keeps the agent decoupled from specific sender identities.
        if message.message_type != ACPMessageType.RESPONSE:
            self._log(
                f"Unexpected message type '{message.message_type.value}' "
                f"from '{message.sender_id}'. WriterAgent only handles RESPONSE messages."
            )
            return

        # Process the research brief
        self._handle_write_request(message)

    # ==========================================================================
    # WRITE REQUEST HANDLER
    # ==========================================================================

    def _handle_write_request(self, message: ACPMessage) -> None:
        """
        Handle the research brief from ResearcherAgent and produce a draft.

        This is the main work method of the WriterAgent.  It orchestrates
        the full writing process:
          1. Validate the incoming research brief
          2. Fetch style guide via MCP
          3. Compose the article (title + all sections)
          4. Save the draft via MCP
          5. Send the draft to EditorAgent via ACP bus

        ACP CONCEPT: Pipeline Stage Handoff
        -------------------------------------
        Each stage in the pipeline receives a payload, enriches it, and
        passes it forward.  Notice that the WriterAgent's output payload
        includes both NEW data it created (title, draft_content) AND the
        original research brief it received.  This "pass-through" pattern
        means that downstream agents (Editor, SEO, Publisher) have access
        to the full context of what was researched, not just the latest
        transformation of it.

        Think of it as each stage annotating a shared document rather than
        discarding what came before.

        Parameters
        ----------
        message : ACPMessage
            The ACP message containing the ResearcherAgent's research brief
            in its payload.
        """
        research_brief: Dict[str, Any] = message.payload or {}

        # ---- Validate incoming research brief -----------------------
        topic = research_brief.get("topic", "").strip()
        if not topic:
            self._log(
                "ERROR: Research brief is missing 'topic'. "
                "Cannot write an article without a topic."
            )
            self.send(
                receiver_id=message.sender_id,
                payload={
                    "error": "Research brief missing required field 'topic'.",
                    "received_keys": list(research_brief.keys()),
                },
                msg_type=ACPMessageType.ERROR,
                correlation_id=message.message_id,
            )
            self.mark_error("Missing 'topic' in research brief")
            return

        content_type: str = research_brief.get("content_type", "blog")

        self._log_section(f"WriterAgent — Writing: '{topic}' ({content_type})")
        self._log(
            f"Received research brief from '{message.sender_id}' "
            f"(corr:{message.correlation_id}). "
            f"Topic: '{topic}', Content type: '{content_type}'. "
            f"Brief contains {len(research_brief.get('facts', []))} facts, "
            f"{len(research_brief.get('key_people', []))} key people."
        )
        self._log(
            "ACP CONCEPT: WriterAgent received the research brief through the "
            "ACP bus — it never called ResearcherAgent directly. "
            "The correlation_id links this work back to the original REQUEST."
        )

        # ---- Step 1: Fetch Style Guide via MCP ----------------------
        self._log(
            f"Step 1/4: Calling MCP tool 'get_style_guide' for content_type='{content_type}'"
        )
        self._log(
            "MCP CONCEPT: The WriterAgent doesn't know what 'blog style' means "
            "until it asks the MCP server. This separation means the editorial "
            "team can update style_guides.json and all future articles "
            "automatically use the new guidelines — no code changes needed."
        )

        style_response = self.mcp.call_tool(
            "get_style_guide", content_type=content_type
        )

        if not style_response["success"] or not style_response.get("result"):
            self._log(
                f"MCP 'get_style_guide' failed: {style_response.get('error')}. "
                f"Using built-in fallback style guidelines."
            )
            style_guide = self._fallback_style_guide(content_type)
        else:
            style_guide = style_response["result"]
            self._log(
                f"Style guide loaded: tone='{style_guide.get('tone')}', "
                f"target={style_guide.get('avg_word_count')} words, "
                f"structure={style_guide.get('structure', [])}"
            )

        # ---- Step 2: Compose the Article Draft ----------------------
        self._log(
            f"Step 2/4: Composing article draft. "
            f"Target: ~{style_guide.get('avg_word_count', 1200)} words, "
            f"tone: {style_guide.get('tone', 'neutral')}"
        )

        title = self._craft_title(
            topic, research_brief.get("seo_hints", {}), content_type
        )
        self._log(f"Crafted title: '{title}'")

        draft_content = self._compose_article(
            topic=topic,
            title=title,
            research_brief=research_brief,
            style_guide=style_guide,
            content_type=content_type,
        )

        word_count = len(draft_content.split())
        self._log(
            f"Article draft composed: {word_count} words, "
            f"target was {style_guide.get('avg_word_count', 1200)}."
        )

        # ---- Step 3: Save Draft via MCP -----------------------------
        self._log("Step 3/4: Saving draft to temporary storage via MCP 'save_draft'")
        self._log(
            "MCP CONCEPT: The WriterAgent delegates file I/O entirely to the "
            "MCP server. The agent doesn't know or care where the file is saved — "
            "only that the tool confirms success. This means you can change the "
            "storage backend (local file → S3 → CMS API) without touching agent code."
        )

        save_response = self.mcp.call_tool(
            "save_draft", title=title, content=draft_content
        )

        if save_response["success"] and save_response.get("result"):
            save_result = save_response["result"]
            self._log(
                f"Draft saved successfully. "
                f"Draft key: '{save_result.get('draft_key')}', "
                f"File: {save_result.get('filepath', 'unknown')}"
            )
            draft_filepath = save_result.get("filepath", "")
            draft_key = save_result.get("draft_key", "")
        else:
            self._log(
                f"Warning: Could not save draft to file system: "
                f"{save_response.get('error')}. "
                f"Continuing — the draft content is in the ACP message payload."
            )
            draft_filepath = ""
            draft_key = ""

        # ---- Step 4: Send Draft to EditorAgent via ACP Bus ----------
        self._log(
            "Step 4/4: Sending draft to EditorAgent via ACP bus.\n"
            "  ACP CONCEPT: WriterAgent sends a RESPONSE with the draft payload.\n"
            "  The correlation_id is the ResearcherAgent's message ID — this\n"
            "  creates a traceable chain: request → research → draft → edit.\n"
            "  EditorAgent receives this without WriterAgent needing to know\n"
            "  anything about the EditorAgent's implementation."
        )

        output_payload: Dict[str, Any] = {
            # ── New data produced by WriterAgent ─────────────────────────
            "title": title,
            "draft_content": draft_content,
            "word_count": word_count,
            "style_used": {
                "content_type": content_type,
                "tone": style_guide.get("tone", ""),
                "target_word_count": style_guide.get("avg_word_count", 1200),
                "reading_level": style_guide.get("reading_level", ""),
                "use_subheadings": style_guide.get("use_subheadings", True),
            },
            "draft_filepath": draft_filepath,
            "draft_key": draft_key,
            # ── Pass-through from ResearcherAgent ─────────────────────────
            # EditorAgent and downstream agents need the original research
            # context to verify facts and assess content quality.
            "topic": topic,
            "research_brief": research_brief,
            # ── Protocol metadata ─────────────────────────────────────────
            "written_by": self.agent_id,
            "source_message_id": message.message_id,
        }

        sent_msg = self.send(
            receiver_id="editor",
            payload=output_payload,
            msg_type=ACPMessageType.RESPONSE,
            content_type=ACPContentType.JSON,
            correlation_id=message.correlation_id,  # preserve the original chain
            metadata={
                "pipeline_stage": 2,
                "topic": topic,
                "word_count": word_count,
            },
        )

        self._log(
            f"Draft sent to EditorAgent "
            f"(ACP msg id: {sent_msg.message_id}, "
            f"corr: {sent_msg.correlation_id}). "
            f"WriterAgent's work is done — {word_count} words written."
        )

        self._log_section_end()
        self.mark_done()

    # ==========================================================================
    # ARTICLE COMPOSITION
    # ==========================================================================

    def _craft_title(
        self,
        topic: str,
        seo_hints: Dict[str, Any],
        content_type: str,
    ) -> str:
        """
        Craft an engaging, SEO-aware article title.

        A good title for a blog post should:
          - Include the primary keyword near the beginning
          - Convey a clear benefit or promise to the reader
          - Be 50-70 characters (optimal for search engine display)
          - Use numbers or power words when natural

        For technical articles the title should:
          - Be precise and descriptive
          - Use established terminology from the field

        Parameters
        ----------
        topic : str
            The article's main topic.
        seo_hints : dict
            SEO keyword data from the ResearcherAgent's brief.
        content_type : str
            Target format ('blog', 'technical', 'news').

        Returns
        -------
        str
            The crafted article title.
        """
        title_topic = topic.title()
        primary_keywords: List[str] = seo_hints.get("primary_keywords", [])
        long_tail: List[str] = seo_hints.get("long_tail_phrases", [])

        if content_type == "blog":
            # Blog titles use numbers, questions, or "how/why" frames
            title_templates = [
                f"The Complete Guide to {title_topic}: What You Need to Know in 2024",
                f"{title_topic} Explained: Key Trends, Facts, and Future Outlook",
                f"Everything You Need to Know About {title_topic} Right Now",
                f"How {title_topic} Is Changing the World — And What It Means for You",
            ]
            # Prefer a template that incorporates a long-tail keyword if available
            if long_tail:
                phrase = long_tail[0].title()
                title_templates.insert(0, f"{phrase}: The Definitive Guide for 2024")
            # Pick the first (best) template
            return title_templates[0]

        elif content_type == "technical":
            return (
                f"{title_topic}: Current State, Emerging Methodologies, "
                f"and Future Directions"
            )

        else:  # news
            primary_kw = (
                primary_keywords[0].title() if primary_keywords else title_topic
            )
            return f"Breaking: Major Developments in {primary_kw} Signal Pivotal Shift"

    def _compose_article(
        self,
        topic: str,
        title: str,
        research_brief: Dict[str, Any],
        style_guide: Dict[str, Any],
        content_type: str,
    ) -> str:
        """
        Compose the full article draft in Markdown format.

        This method orchestrates the composition of every section,
        delegating to specialised helper methods for each part.
        The final output is a complete, properly formatted Markdown document.

        Article structure (blog format):
          # Title
          ## Introduction
          ## [Section 1: What Is X and Why It Matters]
          ## [Section 2: Key Facts and Developments]
          ## [Section 3: Expert Voices and Key People]
          ## [Section 4: Statistics and Data]
          ## [Section 5: Future Outlook]
          ## Conclusion
          ---
          *editorial footer*

        Parameters
        ----------
        topic : str
            The article topic.
        title : str
            The crafted article title.
        research_brief : dict
            Full research package from the ResearcherAgent.
        style_guide : dict
            Writing guidelines from the MCP style guide tool.
        content_type : str
            Target format ('blog', 'technical', 'news').

        Returns
        -------
        str
            The complete article draft in Markdown format.
        """
        facts: List[str] = research_brief.get("facts", [])
        key_people: List[str] = research_brief.get("key_people", [])
        recent_developments: List[str] = research_brief.get("recent_developments", [])
        statistics: Dict[str, str] = research_brief.get("statistics", {})
        seo_hints: Dict[str, Any] = research_brief.get("seo_hints", {})
        writing_guidance: Dict[str, Any] = research_brief.get("writing_guidance", {})
        key_messages: List[str] = writing_guidance.get("key_messages", facts[:3])

        # ---- Build section by section --------------------------------
        sections: List[str] = []

        # 1. Introduction / Hook
        sections.append(
            self._write_introduction(
                topic, facts, key_messages, style_guide, content_type
            )
        )

        # 2. What Is / Background section
        sections.append(self._write_background_section(topic, facts, content_type))

        # 3. Key Developments / Current State
        sections.append(
            self._write_developments_section(
                topic, recent_developments, facts, content_type
            )
        )

        # 4. Expert Voices / Key People
        if key_people:
            sections.append(
                self._write_experts_section(topic, key_people, facts, content_type)
            )

        # 5. Statistics and Data
        if statistics:
            sections.append(
                self._write_statistics_section(topic, statistics, content_type)
            )

        # 6. Future Outlook
        sections.append(
            self._write_future_section(
                topic, recent_developments, seo_hints, content_type
            )
        )

        # 7. Conclusion + CTA
        sections.append(
            self._write_conclusion(topic, key_messages, style_guide, content_type)
        )

        # ---- Assemble the full document ------------------------------
        # Join sections with double newlines for proper Markdown spacing
        body = "\n\n".join(sections)

        # Add editorial footer with pipeline metadata
        footer = self._write_footer(topic, style_guide)

        full_article = f"# {title}\n\n{body}\n\n{footer}"

        return full_article

    # --------------------------------------------------------------------------
    # Section writers
    # --------------------------------------------------------------------------

    def _write_introduction(
        self,
        topic: str,
        facts: List[str],
        key_messages: List[str],
        style_guide: Dict[str, Any],
        content_type: str,
    ) -> str:
        """
        Write an engaging introductory section.

        Blog tip (from style guide): Open with a surprising fact or bold
        statement to grab the reader's attention.  Follow with context
        that explains why this topic matters right now.

        Parameters
        ----------
        topic : str
        facts : list[str]
        key_messages : list[str]
        style_guide : dict
        content_type : str

        Returns
        -------
        str  Markdown-formatted introduction section.
        """
        title_topic = topic.title()

        # Use the most striking (numeric) fact as the hook
        hook_facts = [f for f in facts if any(c.isdigit() for c in f)]
        hook = (
            hook_facts[0]
            if hook_facts
            else (facts[0] if facts else f"{title_topic} is reshaping our world.")
        )

        if content_type == "blog":
            intro = textwrap.dedent(f"""\
                ## Introduction

                Here is a striking reality: {hook}

                If that number surprises you, you are not alone. {title_topic} has moved \
                from the pages of science fiction into the centre of our daily lives at a \
                breathtaking pace. Whether you are a business leader wondering how to adapt, \
                a curious learner trying to make sense of the headlines, or a professional \
                navigating an industry in flux — understanding {topic} has never been more \
                important.

                In this article, we cut through the hype and give you the facts, the context, \
                and the forward-looking perspective you need. By the end, you will have a \
                clear picture of where {topic} stands today, why it matters, and what it \
                means for the future.\
            """)

        elif content_type == "technical":
            intro = textwrap.dedent(f"""\
                ## Introduction

                {title_topic} represents one of the most significant areas of research \
                and development in the contemporary technological landscape. Recent data \
                underscores this significance: {hook}

                This article provides a comprehensive examination of the current state of \
                {topic}, surveying recent methodological advances, analysing key empirical \
                findings, and projecting likely trajectories for the field. Our analysis \
                draws on the latest available data and synthesises perspectives from leading \
                practitioners and researchers.

                The remainder of this article is structured as follows: Section 2 provides \
                background and foundational context; Section 3 surveys current developments; \
                Section 4 analyses key data; Section 5 considers future directions; and \
                Section 6 concludes with a synthesis of findings.\
            """)

        else:  # news
            intro = textwrap.dedent(f"""\
                ## Overview

                {hook} This latest development underscores the accelerating pace of change \
                in the {topic} space.

                In a series of announcements and findings that have reverberated across \
                industry and government alike, {topic} continues to command attention \
                from experts, policymakers, and the public. Here is what you need to know.\
            """)

        return intro

    def _write_background_section(
        self,
        topic: str,
        facts: List[str],
        content_type: str,
    ) -> str:
        """
        Write the background / "What Is X" section.

        Uses the first 2-3 facts as foundational knowledge that
        establishes context for readers unfamiliar with the topic.

        Parameters
        ----------
        topic : str
        facts : list[str]
        content_type : str

        Returns
        -------
        str  Markdown-formatted background section.
        """
        title_topic = topic.title()
        background_facts = facts[:3] if len(facts) >= 3 else facts

        if content_type == "blog":
            heading = f"## What Is {title_topic} and Why Does It Matter?"

            # Build a paragraph from the first 2 background facts
            fact_sentences = ""
            if len(background_facts) >= 2:
                fact_sentences = (
                    f"To put it in perspective: {background_facts[0].lower().rstrip('.')}. "
                    f"Meanwhile, {background_facts[1].lower().rstrip('.')}. "
                )
            elif background_facts:
                fact_sentences = f"Consider this: {background_facts[0]} "

            body_para = (
                f"{title_topic} refers to the broad set of technologies, systems, and "
                f"processes that are fundamentally transforming how we work, communicate, "
                f"and solve problems. {fact_sentences}"
                f"These are not abstract projections — they are measurable realities "
                f"that are already reshaping industries from healthcare to finance, "
                f"from education to entertainment."
            )

            impact_para = (
                f"What makes {topic} particularly significant is the speed at which it "
                f"is developing. Unlike previous technological shifts that unfolded over "
                f"decades, the changes driven by {topic} are happening in years — "
                f"sometimes months. This compressed timeline means that the window for "
                f"preparation is shorter than ever, and the cost of inaction is higher."
            )

            return f"{heading}\n\n{body_para}\n\n{impact_para}"

        elif content_type == "technical":
            heading = f"## Background and Context"

            context_para = (
                f"The field of {topic} has undergone substantial evolution over the past "
                f"decade, driven by a convergence of increased computational power, "
                f"expanded data availability, and algorithmic innovation. "
            )
            if background_facts:
                context_para += (
                    f"Empirical indicators confirm the field's trajectory: "
                    + " ".join(f"{f.rstrip('.')}." for f in background_facts)
                )

            methodology_para = (
                f"Current approaches to {topic} span a range of methodologies, from "
                f"traditional rule-based systems to data-driven machine learning paradigms. "
                f"The increasing availability of large-scale datasets and high-performance "
                f"computing infrastructure has accelerated the shift toward end-to-end "
                f"learned systems, raising both the capabilities and the interpretability "
                f"challenges of deployed solutions."
            )

            return f"{heading}\n\n{context_para}\n\n{methodology_para}"

        else:  # news
            heading = f"## Background"
            para = (
                f"{title_topic} has been a focal point of global attention, with "
                f"developments accelerating rapidly. "
            )
            if background_facts:
                para += " ".join(f"{f.rstrip('.')}." for f in background_facts[:2])

            return f"{heading}\n\n{para}"

    def _write_developments_section(
        self,
        topic: str,
        recent_developments: List[str],
        facts: List[str],
        content_type: str,
    ) -> str:
        """
        Write the "Key Developments" section using recent_developments data.

        This section covers what is happening RIGHT NOW with the topic,
        giving the article timeliness and relevance.  Each development
        is presented as a bullet point with a brief explanatory sentence.

        Parameters
        ----------
        topic : str
        recent_developments : list[str]
        facts : list[str]
        content_type : str

        Returns
        -------
        str  Markdown-formatted developments section.
        """
        title_topic = topic.title()
        devs = recent_developments[:5] if recent_developments else []

        # If no developments in the research brief, fall back to using facts
        if not devs and facts:
            devs = [f"New data confirms: {f}" for f in facts[2:5]]

        if content_type == "blog":
            heading = f"## The Latest Developments in {title_topic}"
            intro_para = (
                f"The pace of change in {topic} is genuinely remarkable. "
                f"Staying current requires more than just following the headlines — "
                f"you need to understand the underlying shifts that are driving them. "
                f"Here are the most significant developments shaping the landscape today:"
            )

            # Format developments as an unordered list with explanations
            dev_bullets = self._format_development_bullets(devs, topic, content_type)

            transition = (
                f"Each of these developments represents not just a technical milestone "
                f"but a shift in what is possible — and, more importantly, what is expected. "
                f"Understanding them is the first step to navigating the {topic} landscape "
                f"with confidence."
            )

            return f"{heading}\n\n{intro_para}\n\n{dev_bullets}\n\n{transition}"

        elif content_type == "technical":
            heading = f"## Current State and Recent Advances"
            intro_para = (
                f"The contemporary {topic} landscape is characterised by rapid advances "
                f"across multiple fronts. Key developments include the following:"
            )
            dev_bullets = self._format_development_bullets(devs, topic, content_type)

            analysis_para = (
                f"These advances collectively indicate a field transitioning from "
                f"proof-of-concept demonstrations to production-scale deployments. "
                f"The convergence of improved algorithms, specialised hardware, and "
                f"mature tooling ecosystems has substantially lowered barriers to "
                f"entry while simultaneously raising the performance ceiling."
            )

            return f"{heading}\n\n{intro_para}\n\n{dev_bullets}\n\n{analysis_para}"

        else:  # news
            heading = f"## Key Developments"
            dev_bullets = self._format_development_bullets(devs, topic, content_type)
            return f"{heading}\n\n{dev_bullets}"

    def _write_experts_section(
        self,
        topic: str,
        key_people: List[str],
        facts: List[str],
        content_type: str,
    ) -> str:
        """
        Write the "Expert Voices" section referencing key people in the field.

        Quoting or referencing experts adds credibility and authority to the
        article.  Since we don't have real quotes in the knowledge base, we
        reference the people and associate them with related facts.

        Parameters
        ----------
        topic : str
        key_people : list[str]
        facts : list[str]
        content_type : str

        Returns
        -------
        str  Markdown-formatted experts section.
        """
        title_topic = topic.title()

        # Pair people with facts for contextual attribution
        people_to_mention = key_people[:4]
        supporting_fact = facts[3] if len(facts) > 3 else (facts[-1] if facts else "")

        if content_type == "blog":
            heading = "## What the Experts Are Saying"

            intro_para = (
                f"To truly understand {topic}, it helps to hear from the people "
                f"building and studying it. The field has attracted some of the world's "
                f"most brilliant minds, and their perspectives offer invaluable insight "
                f"into where things are headed."
            )

            # Format expert list with brief descriptions
            expert_paras = self._format_expert_list(people_to_mention, topic)

            if supporting_fact:
                closing = (
                    f"These experts and their peers are aligned on one key point: "
                    f"the trajectory of {topic} is clear. As the data shows — "
                    f"{supporting_fact.lower().rstrip('.')} — the question is no longer "
                    f"*whether* this technology will be transformative, but *how* and *when*."
                )
            else:
                closing = (
                    f"The consensus among these leading voices is unmistakable: "
                    f"{topic} is not a passing trend. It is a fundamental shift in "
                    f"how technology and society interact."
                )

            return f"{heading}\n\n{intro_para}\n\n{expert_paras}\n\n{closing}"

        elif content_type == "technical":
            heading = "## Key Contributors and Research Groups"

            contributors = ", ".join(people_to_mention[:-1])
            if len(people_to_mention) > 1:
                contributors += f", and {people_to_mention[-1]}"
            elif people_to_mention:
                contributors = people_to_mention[0]

            para = (
                f"Research in {topic} has been significantly shaped by contributions "
                f"from practitioners including {contributors}. "
                f"Their work spans theoretical foundations, empirical evaluation, "
                f"and practical system design, collectively advancing the field's "
                f"understanding of both capabilities and limitations."
            )

            return f"{heading}\n\n{para}"

        else:  # news
            heading = "## Expert Perspectives"
            people_str = ", ".join(people_to_mention)
            para = (
                f"Leading figures in the {topic} space, including {people_str}, "
                f"have weighed in on these latest developments, generally expressing "
                f"cautious optimism about the pace of progress while calling for "
                f"thoughtful governance frameworks."
            )
            return f"{heading}\n\n{para}"

    def _write_statistics_section(
        self,
        topic: str,
        statistics: Dict[str, str],
        content_type: str,
    ) -> str:
        """
        Write a data-driven statistics section.

        Numbers and concrete data points are among the most persuasive
        elements in any article.  This section presents the key statistics
        from the knowledge base in an accessible, contextualised format.

        Parameters
        ----------
        topic : str
        statistics : dict[str, str]
        content_type : str

        Returns
        -------
        str  Markdown-formatted statistics section.
        """
        title_topic = topic.title()

        if content_type == "blog":
            heading = f"## {title_topic} by the Numbers"
            intro = (
                f"Sometimes, the best way to grasp the scale of a phenomenon is to "
                f"look at the data. The numbers surrounding {topic} are not just big — "
                f"they are transformative:"
            )

            stat_bullets = []
            for key, value in list(statistics.items())[:6]:
                label = key.replace("_", " ").title()
                stat_bullets.append(f"- **{label}**: {value}")

            stats_block = "\n".join(stat_bullets)

            closing = (
                f"These figures are not just impressive data points — they represent "
                f"real investment, real jobs, and real societal change. "
                f"They tell the story of a world that is betting heavily on {topic}, "
                f"and winning."
            )

            return f"{heading}\n\n{intro}\n\n{stats_block}\n\n{closing}"

        elif content_type == "technical":
            heading = "## Quantitative Indicators and Market Data"

            stat_items = []
            for key, value in list(statistics.items())[:6]:
                label = key.replace("_", " ").title()
                stat_items.append(f"- **{label}**: {value}")

            stat_block = "\n".join(stat_items)

            para = (
                f"The following quantitative indicators characterise the current "
                f"state of the {topic} landscape:\n\n{stat_block}\n\n"
                f"These metrics collectively suggest a field experiencing "
                f"sustained, compounding growth across investment, adoption, and "
                f"technical capability dimensions."
            )

            return f"{heading}\n\n{para}"

        else:  # news
            heading = "## By the Numbers"
            stat_items = [
                f"- **{k.replace('_', ' ').title()}**: {v}"
                for k, v in list(statistics.items())[:4]
            ]
            return f"{heading}\n\n" + "\n".join(stat_items)

    def _write_future_section(
        self,
        topic: str,
        recent_developments: List[str],
        seo_hints: Dict[str, Any],
        content_type: str,
    ) -> str:
        """
        Write the future outlook section.

        This section gives the article forward momentum and is often the
        most-read section (readers want to know: "what does this mean for ME
        going forward?").  It uses recent developments and related topics
        as the basis for forward-looking statements.

        Parameters
        ----------
        topic : str
        recent_developments : list[str]
        seo_hints : dict
        content_type : str

        Returns
        -------
        str  Markdown-formatted future outlook section.
        """
        title_topic = topic.title()
        related: List[str] = seo_hints.get("related_topics", [])
        future_devs = (
            recent_developments[-3:]
            if len(recent_developments) >= 3
            else recent_developments
        )

        if content_type == "blog":
            heading = f"## The Future of {title_topic}: What to Expect Next"

            horizon_para = (
                f"If the current trajectory holds — and there is every reason to "
                f"believe it will — the next few years will bring changes in {topic} "
                f"that dwarf what we have seen so far. The seeds being planted today "
                f"will define entire industries by the end of the decade."
            )

            # Reference recent developments as signals of the future
            if future_devs:
                signals_intro = (
                    f"Several developments signal where {topic} is headed next:"
                )
                signal_bullets = "\n".join(f"- {dev}" for dev in future_devs)
                signals_block = f"{signals_intro}\n\n{signal_bullets}"
            else:
                signals_block = (
                    f"Emerging research and investment patterns point unmistakably "
                    f"toward continued acceleration in this space."
                )

            # Mention related topics as expanding ecosystem
            if related:
                related_str = ", ".join(related[:3])
                ecosystem_para = (
                    f"Furthermore, {topic} does not exist in isolation. Its growth "
                    f"is deeply intertwined with adjacent fields including {related_str}. "
                    f"As these areas converge, the combined effect will be far greater "
                    f"than any single technology could achieve alone."
                )
            else:
                ecosystem_para = (
                    f"The ripple effects of advances in {topic} will extend far beyond "
                    f"the field itself, reshaping adjacent industries and creating "
                    f"entirely new categories of products and services."
                )

            return f"{heading}\n\n{horizon_para}\n\n{signals_block}\n\n{ecosystem_para}"

        elif content_type == "technical":
            heading = "## Future Directions and Open Challenges"

            challenges_para = (
                f"Despite significant progress, several open challenges remain in {topic}. "
                f"Scalability, interpretability, robustness to distribution shift, and "
                f"ethical deployment frameworks represent active research fronts where "
                f"fundamental questions remain unanswered."
            )

            directions_para = (
                f"Promising future directions include "
                + (
                    ", ".join(future_devs)
                    if future_devs
                    else f"continued refinement of core {topic} methodologies"
                )
                + ". "
                f"Interdisciplinary collaboration — particularly between {topic} "
                f"researchers and domain experts in high-stakes application areas — "
                f"will be essential to realising the field's full potential responsibly."
            )

            return f"{heading}\n\n{challenges_para}\n\n{directions_para}"

        else:  # news
            heading = "## What Comes Next"
            para = (
                f"Industry analysts and researchers expect the pace of development in "
                f"{topic} to continue accelerating. "
                + (
                    " ".join(f"{d.rstrip('.')}." for d in future_devs[:2])
                    if future_devs
                    else ""
                )
            )
            return f"{heading}\n\n{para}"

    def _write_conclusion(
        self,
        topic: str,
        key_messages: List[str],
        style_guide: Dict[str, Any],
        content_type: str,
    ) -> str:
        """
        Write the conclusion and call-to-action.

        The conclusion should:
          - Briefly recap the 2-3 most important points
          - Leave the reader with a clear takeaway
          - Include a call-to-action (blog) or summary of findings (technical)

        Parameters
        ----------
        topic : str
        key_messages : list[str]
        style_guide : dict
        content_type : str

        Returns
        -------
        str  Markdown-formatted conclusion section.
        """
        title_topic = topic.title()

        if content_type == "blog":
            # Build a brief recap using key messages
            recap_points = (
                key_messages[:3]
                if key_messages
                else [f"{title_topic} is rapidly evolving."]
            )
            recap_bullets = "\n".join(f"- {msg}" for msg in recap_points)

            conclusion = textwrap.dedent(f"""\
                ## Final Thoughts

                We have covered a lot of ground. Let's bring it together with the \
                essentials worth remembering:

                {recap_bullets}

                {title_topic} is no longer on the horizon — it is here, and it is \
                accelerating. The organisations, professionals, and individuals who \
                take the time to understand it now will be the ones best positioned \
                to benefit from it tomorrow.

                **What you can do today:** Start by identifying one area of your work \
                or life where {topic} is already having — or could soon have — a \
                meaningful impact. Read deeper on that specific angle. Then take one \
                concrete step, however small, toward engaging with it proactively \
                rather than reactively.

                The future belongs to those who prepare for it.\
            """)

        elif content_type == "technical":
            recap_points = (
                key_messages[:2]
                if key_messages
                else [f"Progress in {topic} continues."]
            )
            recap_text = " ".join(f"{msg.rstrip('.')}." for msg in recap_points)

            conclusion = textwrap.dedent(f"""\
                ## Conclusion

                This article has surveyed the current state of {topic}, examining \
                recent developments, key empirical findings, and future research \
                directions. {recap_text}

                The evidence reviewed herein supports the conclusion that {topic} \
                has entered a phase of sustained, compound growth in both technical \
                capability and real-world deployment. Researchers and practitioners \
                are encouraged to engage with the open challenges identified, \
                particularly those at the intersection of capability advancement \
                and responsible deployment.

                Future work should prioritise longitudinal studies that track the \
                societal impact of {topic} deployments, as well as theoretical \
                investigations into the fundamental limits of current approaches.\
            """)

        else:  # news
            conclusion = textwrap.dedent(f"""\
                ## Takeaway

                The developments in {topic} described here represent a significant \
                moment in the field's evolution. Stakeholders across government, \
                industry, and civil society will be watching closely as events unfold \
                in the coming months.\
            """)

        return conclusion

    def _write_footer(self, topic: str, style_guide: Dict[str, Any]) -> str:
        """
        Write an editorial footer with pipeline and style metadata.

        This footer is stripped out by the PublisherAgent before final
        publication — it serves as an internal annotation that tracks
        how this draft was produced, making the pipeline auditable.

        Parameters
        ----------
        topic : str
        style_guide : dict

        Returns
        -------
        str  Markdown-formatted footer.
        """
        return (
            f"---\n"
            f"*Draft produced by ContentForge WriterAgent. "
            f"Topic: {topic}. "
            f"Style: {style_guide.get('tone', 'blog')}. "
            f"Pending editorial review by EditorAgent and SEO optimisation by SEOAgent.*"
        )

    # --------------------------------------------------------------------------
    # Formatting helpers
    # --------------------------------------------------------------------------

    @staticmethod
    def _format_development_bullets(
        developments: List[str],
        topic: str,
        content_type: str,
    ) -> str:
        """
        Format a list of developments as Markdown bullet points,
        each with a brief explanatory expansion sentence.

        Parameters
        ----------
        developments : list[str]
        topic : str
        content_type : str

        Returns
        -------
        str  Markdown unordered list.
        """
        if not developments:
            return f"- Ongoing advances in {topic} continue to reshape the landscape."

        bullets: List[str] = []
        expansion_phrases = [
            "This represents a significant leap forward in capability.",
            "Industry observers point to this as a defining shift in the field.",
            "The implications for practitioners and end-users are far-reaching.",
            "This development has attracted substantial attention and investment.",
            "Early adopters are already reporting measurable benefits.",
            "Analysts expect this trend to accelerate through the coming years.",
        ]

        for i, dev in enumerate(developments):
            expansion = expansion_phrases[i % len(expansion_phrases)]
            if content_type == "blog":
                bullets.append(f"- **{dev}** — {expansion}")
            else:
                bullets.append(f"- {dev}")

        return "\n".join(bullets)

    @staticmethod
    def _format_expert_list(people: List[str], topic: str) -> str:
        """
        Format a list of key people into prose paragraphs
        with brief contextual descriptions.

        Parameters
        ----------
        people : list[str]
        topic : str

        Returns
        -------
        str  Markdown prose or bullet list of experts.
        """
        if not people:
            return f"A growing community of researchers and practitioners are advancing {topic}."

        descriptors = [
            "a leading figure in the field",
            "one of the most cited researchers in this space",
            "a pioneer whose work has defined the current generation of thinking",
            "an influential voice on both the technical and policy dimensions",
            "a prominent researcher whose contributions span theory and application",
        ]

        paragraphs: List[str] = []
        for i, person in enumerate(people[:3]):
            descriptor = descriptors[i % len(descriptors)]
            paragraphs.append(
                f"**{person}** — {descriptor.capitalize()} — has consistently "
                f"argued that {topic} requires not just technical innovation but "
                f"thoughtful consideration of societal impact."
            )

        return "\n\n".join(paragraphs)

    # ==========================================================================
    # FALLBACK STYLE GUIDE
    # ==========================================================================

    @staticmethod
    def _fallback_style_guide(content_type: str) -> Dict[str, Any]:
        """
        Return a minimal built-in style guide when the MCP server is unavailable.

        This ensures the WriterAgent can always produce a draft, even if
        the MCP server is down or the requested style guide does not exist.

        In production, you might cache the last-known-good style guide
        rather than using hardcoded defaults.

        Parameters
        ----------
        content_type : str

        Returns
        -------
        dict  Minimal style guide.
        """
        guides = {
            "blog": {
                "tone": "conversational yet authoritative",
                "structure": [
                    "engaging_hook",
                    "context",
                    "main_points",
                    "conclusion",
                    "cta",
                ],
                "avg_word_count": 1200,
                "paragraph_length": "3-4 sentences",
                "use_subheadings": True,
                "reading_level": "8th grade",
                "tips": ["Use active voice", "Include statistics", "End with a CTA"],
                "forbidden": ["Avoid jargon without explanation"],
            },
            "technical": {
                "tone": "precise and detailed",
                "structure": [
                    "abstract",
                    "introduction",
                    "methodology",
                    "findings",
                    "conclusion",
                ],
                "avg_word_count": 2500,
                "paragraph_length": "4-6 sentences",
                "use_subheadings": True,
                "reading_level": "college",
                "tips": ["Define all technical terms", "Support claims with data"],
                "forbidden": ["No colloquialisms"],
            },
            "news": {
                "tone": "objective and factual",
                "structure": ["headline", "lead", "body", "background"],
                "avg_word_count": 600,
                "paragraph_length": "1-2 sentences",
                "use_subheadings": False,
                "reading_level": "6th grade",
                "tips": ["Lead with key facts", "Be objective"],
                "forbidden": ["No opinions"],
            },
        }
        return guides.get(content_type, guides["blog"])

    # ==========================================================================
    # REGISTRY METADATA
    # ==========================================================================

    def get_agent_info(self) -> ACPAgentInfo:
        """
        Return ACPAgentInfo describing this agent for the ACP Agent Registry.

        The schemas here document the exact structure of messages this agent
        accepts and produces, enabling other pipeline components to interact
        with the WriterAgent without reading its source code.

        Returns
        -------
        ACPAgentInfo
        """
        return ACPAgentInfo(
            agent_id=self.AGENT_ID,
            name="WriterAgent",
            description=(
                "Stage 2: Receives the research brief from ResearcherAgent and "
                "composes a full article draft (introduction, body sections, conclusion) "
                "using the MCP style guide. Saves the draft and forwards it to the "
                "EditorAgent for review and improvement."
            ),
            input_schema={
                "topic": "str — the article topic",
                "facts": "list[str] — researched facts",
                "key_people": "list[str] — notable people in the field",
                "recent_developments": "list[str] — recent news/advances",
                "statistics": "dict[str, str] — key statistics",
                "seo_hints": "dict — keyword intelligence from ResearcherAgent",
                "writing_guidance": "dict — suggested angle, sections, key messages",
                "content_type": "str — 'blog' | 'technical' | 'news'",
            },
            output_schema={
                "title": "str — crafted article title",
                "draft_content": "str — full article draft in Markdown",
                "word_count": "int — word count of the draft",
                "style_used": "dict — style guide used (tone, structure, reading_level)",
                "draft_filepath": "str — path to the saved draft file",
                "draft_key": "str — key for draft retrieval from MCP server",
                "topic": "str — passed through from input",
                "research_brief": "dict — full original research brief (pass-through)",
                "written_by": "str — agent_id of this agent ('writer')",
            },
            status=self._status,
            pipeline_stage=2,
            tags=["writing", "drafting", "content-creation", "stage-2"],
            metadata={
                "mcp_tools_used": ["get_style_guide", "save_draft"],
                "sends_to": ["editor"],
                "receives_from": ["researcher"],
                "output_format": "markdown",
            },
        )
