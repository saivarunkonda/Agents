# =============================================================================
# agents/seo_agent.py  —  SEOAgent
# =============================================================================
#
# ROLE IN THE PIPELINE
# --------------------
# The SEOAgent is Stage 4 of the ContentForge pipeline.  It receives the
# editorially-reviewed article from the EditorAgent and applies Search Engine
# Optimisation (SEO) techniques: keyword analysis, density optimisation, and
# the generation of all metadata required for effective search engine indexing.
#
# Great content that no one can find is wasted content.  The SEOAgent bridges
# the gap between "well-written" and "discoverable."
#
# WHAT IT DOES
# ------------
#   1. Receives a RESPONSE message from EditorAgent with payload:
#      { title, revised_content, edits_made, readability_score,
#        research_brief, topic, word_count, ... }
#
#   2. Uses MCP tool "get_seo_keywords" to fetch:
#        - Primary keywords (high search volume targets)
#        - Secondary keywords (supporting terms)
#        - Long-tail phrases (lower competition, high intent)
#        - Search volume data
#        - Related topics for internal linking suggestions
#
#   3. Performs keyword analysis on the article:
#        - Counts occurrences of each target keyword
#        - Calculates keyword density (%)
#        - Flags keywords that are absent (need insertion)
#        - Flags keywords that are over-used (keyword stuffing risk)
#        - Identifies the best-performing keyword (to designate as primary)
#
#   4. Applies keyword optimisations to the content:
#        - Natural insertion of missing high-priority keywords
#        - Meta title crafting (50-60 chars, primary keyword near front)
#        - Meta description writing (150-160 chars, includes CTA)
#        - Tag/category selection from keyword data
#        - Canonical URL slug generation
#        - Open Graph and Twitter Card metadata
#
#   5. Sends the fully SEO-optimised package to PublisherAgent via ACP bus
#
# ACP FLOW
# --------
#   [EditorAgent]
#         │  RESPONSE  { title, revised_content, edits_made, readability_score,
#         │              research_brief, topic, style_used }
#         ▼
#   [SEOAgent]  ──MCP──▶  get_seo_keywords(topic)
#         │  RESPONSE  { title, final_content, seo_metadata,
#         │              keyword_analysis, word_count, ... }
#         ▼
#   [PublisherAgent]
#
# MCP TOOLS USED
# --------------
#   get_seo_keywords(topic)   → keyword targets, search volumes, related topics
#
# ACP MESSAGE TYPES
# -----------------
#   Receives : ACPMessageType.RESPONSE  (from "editor")
#              ACPMessageType.BROADCAST (ignored gracefully)
#   Sends    : ACPMessageType.RESPONSE  (to "publisher")
#              ACPMessageType.ERROR     (to sender, on failure)
# =============================================================================

import re
from typing import Any, Dict, List, Optional, Tuple

from acp.agent_registry import ACPAgentInfo, ACPAgentRegistry
from acp.message import ACPContentType, ACPMessage, ACPMessageType
from acp.message_bus import ACPMessageBus
from mcp.mcp_client import MCPClient

from agents.base_agent import BaseACPAgent


class SEOAgent(BaseACPAgent):
    """
    SEOAgent — Stage 4 of the ContentForge pipeline.

    Responsible for transforming a well-written, editorially-reviewed article
    into a search-engine-optimised piece of content.  The SEOAgent adds:
      - Keyword density analysis and natural keyword insertion
      - Meta title (50-60 chars, keyword-first)
      - Meta description (150-160 chars, includes a CTA)
      - Suggested tags / categories for the CMS
      - Canonical URL slug
      - Open Graph metadata (for social sharing previews)
      - Twitter Card metadata
      - Structured keyword report showing density for each target keyword

    ACP CONCEPT: Specialised Single-Purpose Agents
    ------------------------------------------------
    The SEOAgent is a perfect example of why the ACP pipeline model is
    valuable.  SEO is a highly specialised discipline with its own data
    sources (keyword databases), its own success metrics (search rankings,
    click-through rates), and its own best practices (keyword density,
    meta tag length limits).

    By isolating this expertise in a single agent, ContentForge achieves:
      - Independent upgradeability: swap in a better SEO agent as search
        algorithms evolve, without touching the Writer or Editor agents.
      - Testability: test SEO logic in isolation with mock content.
      - Replaceability: replace with an agent that calls SEMrush's API
        without any other agent needing to change.

    MCP CONCEPT: On-Demand Data Fetching
    --------------------------------------
    The SEOAgent fetches keyword data from the MCP server ON DEMAND —
    only when it has content to optimise.  It does not pre-fetch or cache
    keyword data at startup.  This is the right pattern because:
      - Keyword data is topic-specific (pointless to fetch before knowing the topic)
      - The MCP server is the single source of truth for keyword data
      - If keyword data changes between pipeline runs, each run gets fresh data

    Parameters
    ----------
    bus : ACPMessageBus
        The shared ACP message bus for this pipeline run.
    mcp_client : MCPClient
        Pre-configured MCP client for tool invocations.
    registry : ACPAgentRegistry, optional
        Shared agent registry for status updates.
    """

    AGENT_ID: str = "seo"

    def __init__(
        self,
        bus: ACPMessageBus,
        mcp_client: MCPClient,
        registry: Optional[ACPAgentRegistry] = None,
    ) -> None:
        super().__init__(
            agent_id=self.AGENT_ID,
            name="SEOAgent",
            bus=bus,
            mcp_client=mcp_client,
            registry=registry,
        )
        self._log(
            "SEOAgent ready.  Accepts RESPONSE messages from EditorAgent. "
            "Will analyse keyword density, generate SEO metadata, and forward "
            "optimised content to PublisherAgent."
        )

    # ==========================================================================
    # ACP MESSAGE DISPATCH
    # ==========================================================================

    def _dispatch(self, message: ACPMessage) -> None:
        """
        Route an incoming ACP message to the appropriate handler.

        ACP CONCEPT: Pipeline Position Awareness
        ------------------------------------------
        The SEOAgent sits near the end of the pipeline.  By the time a message
        reaches it, the content has been researched, written, and edited.
        The SEOAgent's job is to add the final layer of metadata and
        optimisation before publication.

        Like all ACP agents, the SEOAgent is subscribed to the ENTIRE bus,
        not just to messages explicitly addressed to it.  This means it
        receives broadcasts (e.g. "article published") even though it has
        nothing to do with those.  Graceful handling of unexpected messages
        is mandatory in well-designed ACP agents.

        Parameters
        ----------
        message : ACPMessage
            The incoming ACP message to evaluate and optionally handle.
        """
        if message.message_type == ACPMessageType.BROADCAST:
            broadcast_payload = message.payload or {}
            announcement = broadcast_payload.get("announcement", str(broadcast_payload))
            self._log(
                f"Received BROADCAST from '{message.sender_id}': "
                f"'{str(announcement)[:80]}' — SEOAgent has no action. Ignoring."
            )
            return

        if message.message_type == ACPMessageType.ACK:
            self._log(f"Received ACK from '{message.sender_id}' — acknowledged.")
            return

        if message.message_type == ACPMessageType.ERROR:
            self._log(
                f"Received ERROR from '{message.sender_id}': {message.payload}. "
                f"SEOAgent cannot optimise content without a valid edited draft."
            )
            self.mark_error(f"Upstream error from {message.sender_id}")
            return

        if message.message_type != ACPMessageType.RESPONSE:
            self._log(
                f"Unexpected message type '{message.message_type.value}' from "
                f"'{message.sender_id}'. SEOAgent only handles RESPONSE messages."
            )
            return

        # Process the edited draft from EditorAgent
        self._handle_seo_request(message)

    # ==========================================================================
    # SEO REQUEST HANDLER
    # ==========================================================================

    def _handle_seo_request(self, message: ACPMessage) -> None:
        """
        Handle the edited draft from EditorAgent and produce SEO-optimised output.

        The SEO optimisation process has five phases:
          1. FETCH      — retrieve keyword targets from the MCP server
          2. ANALYSE    — measure current keyword presence in the content
          3. OPTIMISE   — insert missing keywords naturally into the content
          4. METADATA   — generate all SEO metadata (title, description, tags, etc.)
          5. FORWARD    — send the complete SEO package to PublisherAgent

        ACP CONCEPT: Payload Enrichment Pattern
        -----------------------------------------
        Like every stage in the pipeline, the SEOAgent enriches the payload
        rather than replacing it.  The output contains ALL of the editor's
        output (title, revised_content, edits_made, readability metrics) PLUS
        the new SEO layer (keyword analysis, meta tags, final_content).

        This means the PublisherAgent — and anyone reading the ACP message
        history — has the COMPLETE picture of how the article was produced,
        from raw research facts to published SEO-optimised article.

        Parameters
        ----------
        message : ACPMessage
            The ACP message from EditorAgent containing the revised draft payload.
        """
        editor_payload: Dict[str, Any] = message.payload or {}

        # ---- Validate incoming content ------------------------------
        title: str = editor_payload.get("title", "").strip()
        revised_content: str = editor_payload.get("revised_content", "").strip()

        if not title or not revised_content:
            self._log(
                "ERROR: Payload is missing 'title' or 'revised_content'. "
                "Cannot perform SEO optimisation on empty content."
            )
            self.send(
                receiver_id=message.sender_id,
                payload={
                    "error": "SEO payload missing 'title' and/or 'revised_content'.",
                    "received_keys": list(editor_payload.keys()),
                },
                msg_type=ACPMessageType.ERROR,
                correlation_id=message.message_id,
            )
            self.mark_error("Missing content for SEO optimisation")
            return

        topic: str = editor_payload.get("topic", "").strip()
        word_count: int = editor_payload.get("word_count", len(revised_content.split()))
        edits_made: List[Dict] = editor_payload.get("edits_made", [])
        readability_full: Dict[str, Any] = editor_payload.get("readability_full", {})
        style_used: Dict[str, Any] = editor_payload.get("style_used", {})
        research_brief: Dict[str, Any] = editor_payload.get("research_brief", {})

        self._log_section(f"SEOAgent — Optimising: '{title}'")
        self._log(
            f"Received edited content from '{message.sender_id}' "
            f"(corr:{message.correlation_id}). "
            f"Topic: '{topic}', {word_count} words, "
            f"readability: {editor_payload.get('readability_score', '?')}/100."
        )
        self._log(
            "ACP CONCEPT: SEOAgent has the FULL pipeline context — it can see "
            "the readability score from EditorAgent, the original research facts "
            "from ResearcherAgent, and the writing style from WriterAgent. "
            "All of this context flows through ACP message payloads."
        )

        # ---- Phase 1: FETCH — Get keyword data from MCP -------------
        self._log(
            "Phase 1/5: FETCH — Calling MCP 'get_seo_keywords' for keyword targets"
        )
        self._log(
            "MCP CONCEPT: The SEOAgent doesn't maintain its own keyword database. "
            "It fetches keyword data from the MCP server on demand. "
            "In production, this MCP tool might call SEMrush, Ahrefs, or "
            "Google Keyword Planner APIs — the agent never needs to know which."
        )

        kw_response = self.mcp.call_tool("get_seo_keywords", topic=topic)

        if kw_response["success"] and kw_response.get("result"):
            kw_data: Dict[str, Any] = kw_response["result"]
            self._log(
                f"Keyword data fetched. "
                f"Primary: {kw_data.get('primary', [])}, "
                f"Secondary: {len(kw_data.get('secondary', []))} terms, "
                f"Long-tail: {len(kw_data.get('long_tail', []))} phrases."
            )
        else:
            self._log(
                f"MCP 'get_seo_keywords' failed: {kw_response.get('error')}. "
                f"Falling back to topic-derived keywords."
            )
            kw_data = self._derive_fallback_keywords(topic)

        # ---- Phase 2: ANALYSE — Measure keyword presence -----------
        self._log("Phase 2/5: ANALYSE — Measuring keyword density in current content")

        keyword_analysis = self._analyse_keyword_density(
            content=revised_content,
            kw_data=kw_data,
            word_count=word_count,
        )

        self._log(
            f"Keyword analysis complete. "
            f"Primary keyword '{kw_data.get('primary', ['?'])[0]}' density: "
            f"{keyword_analysis['primary_density']:.2f}% "
            f"(target: 1.0-2.5%). "
            f"Missing keywords: {keyword_analysis['missing_keywords'][:3]}. "
            f"Over-used keywords: {keyword_analysis['overused_keywords'][:3]}."
        )

        self._print_keyword_density_report(keyword_analysis, kw_data)

        # ---- Phase 3: OPTIMISE — Insert missing keywords naturally --
        self._log("Phase 3/5: OPTIMISE — Applying keyword optimisations to content")
        self._log(
            "MCP CONCEPT: The optimisation logic runs inside the agent (it is "
            "'intelligence'), but the keyword targets it uses came from the MCP "
            "tool ('resource access'). MCP separates WHAT to optimise for "
            "from HOW to access that information."
        )

        optimised_content, kw_optimisations = self._apply_keyword_optimisations(
            content=revised_content,
            title=title,
            topic=topic,
            kw_data=kw_data,
            keyword_analysis=keyword_analysis,
        )

        self._log(f"Keyword optimisations applied: {len(kw_optimisations)} change(s).")

        # ---- Phase 4: METADATA — Generate all SEO metadata ----------
        self._log("Phase 4/5: METADATA — Generating SEO metadata package")

        seo_metadata = self._generate_seo_metadata(
            title=title,
            content=optimised_content,
            topic=topic,
            kw_data=kw_data,
            keyword_analysis=keyword_analysis,
            word_count=len(optimised_content.split()),
            readability_full=readability_full,
            edits_made=edits_made,
            style_used=style_used,
        )

        self._print_seo_metadata_summary(seo_metadata)

        # ---- Phase 5: FORWARD — Send to PublisherAgent via ACP -----
        self._log(
            "Phase 5/5: FORWARD — Sending SEO-optimised package to PublisherAgent.\n"
            "  ACP CONCEPT: The 'final_content' in this payload is the article\n"
            "  as it will be published — researched, written, edited, and SEO'd.\n"
            "  The full pipeline history (research_brief, edits_made, keyword_analysis)\n"
            "  travels with it so the PublisherAgent can write a complete metadata file.\n"
            "  This end-to-end traceability is a core ACP guarantee."
        )

        output_payload: Dict[str, Any] = {
            # ── SEO-optimised final content ───────────────────────────────
            "title": title,
            "final_content": optimised_content,
            "seo_metadata": seo_metadata,
            "keyword_analysis": keyword_analysis,
            "kw_optimisations": kw_optimisations,
            "word_count": len(optimised_content.split()),
            # ── Pass-through from upstream agents ─────────────────────────
            "topic": topic,
            "edits_made": edits_made,
            "readability_score": editor_payload.get("readability_score", 0.0),
            "readability_level": editor_payload.get("readability_level", "Unknown"),
            "readability_full": readability_full,
            "grammar_quality": editor_payload.get("grammar_quality", "Unknown"),
            "style_used": style_used,
            "research_brief": research_brief,
            # ── Protocol metadata ─────────────────────────────────────────
            "seo_optimised_by": self.agent_id,
            "source_message_id": message.message_id,
        }

        sent_msg = self.send(
            receiver_id="publisher",
            payload=output_payload,
            msg_type=ACPMessageType.RESPONSE,
            content_type=ACPContentType.JSON,
            correlation_id=message.correlation_id,  # preserve the original chain
            metadata={
                "pipeline_stage": 4,
                "topic": topic,
                "primary_keyword": seo_metadata.get("primary_keyword", ""),
                "meta_title": seo_metadata.get("meta_title", ""),
            },
        )

        self._log(
            f"SEO-optimised content sent to PublisherAgent "
            f"(ACP msg id: {sent_msg.message_id}, "
            f"corr: {sent_msg.correlation_id}). "
            f"SEOAgent's work is done."
        )

        self._log_section_end()
        self.mark_done()

    # ==========================================================================
    # KEYWORD ANALYSIS
    # ==========================================================================

    def _analyse_keyword_density(
        self,
        content: str,
        kw_data: Dict[str, Any],
        word_count: int,
    ) -> Dict[str, Any]:
        """
        Measure the presence and density of all target keywords in the content.

        Keyword density = (keyword occurrences / total words) × 100

        SEO best practices:
          - Primary keyword:   1.0% – 2.5% density (clear signal without stuffing)
          - Secondary keywords: 0.5% – 1.5% density each
          - Long-tail phrases:  at least 1 occurrence each (for long-tail ranking)

        Density < 0.5% on primary keyword → likely won't rank for that term
        Density > 3.0%                    → keyword stuffing risk, may be penalised

        Parameters
        ----------
        content : str
            The article content to analyse.
        kw_data : dict
            Keyword data from the MCP get_seo_keywords tool.
        word_count : int
            Total word count of the content.

        Returns
        -------
        Dict[str, Any]
            Comprehensive keyword analysis report.
        """
        content_lower = content.lower()
        total_words = max(word_count, 1)

        # ---- Gather all keywords to check --------------------------------
        primary_keywords: List[str] = kw_data.get("primary", [])
        secondary_keywords: List[str] = kw_data.get("secondary", [])
        long_tail: List[str] = kw_data.get("long_tail", [])
        all_keywords = primary_keywords + secondary_keywords + long_tail

        # ---- Count occurrences for each keyword --------------------------
        keyword_counts: Dict[str, int] = {}
        keyword_densities: Dict[str, float] = {}

        for keyword in all_keywords:
            if not keyword:
                continue
            kw_lower = keyword.lower()
            # Count whole-word / whole-phrase occurrences
            # For multi-word phrases, use simple substring matching
            if " " in kw_lower:
                count = content_lower.count(kw_lower)
            else:
                # Single-word keyword: count word-boundary occurrences
                matches = re.findall(r"\b" + re.escape(kw_lower) + r"\b", content_lower)
                count = len(matches)

            keyword_counts[keyword] = count
            density = round((count / total_words) * 100, 3)
            keyword_densities[keyword] = density

        # ---- Classify keywords by density status -------------------------
        missing_keywords: List[str] = []
        underused_keywords: List[str] = []
        well_optimised: List[str] = []
        overused_keywords: List[str] = []

        for keyword in primary_keywords:
            density = keyword_densities.get(keyword, 0.0)
            if density == 0.0:
                missing_keywords.append(keyword)
            elif density < 0.8:
                underused_keywords.append(keyword)
            elif density <= 2.5:
                well_optimised.append(keyword)
            else:
                overused_keywords.append(keyword)

        for keyword in secondary_keywords:
            density = keyword_densities.get(keyword, 0.0)
            if density == 0.0:
                missing_keywords.append(keyword)
            elif density > 2.0:
                overused_keywords.append(keyword)

        # ---- Long-tail presence check ------------------------------------
        present_long_tail: List[str] = []
        absent_long_tail: List[str] = []
        for phrase in long_tail:
            if keyword_counts.get(phrase, 0) >= 1:
                present_long_tail.append(phrase)
            else:
                absent_long_tail.append(phrase)

        # ---- Identify the best-performing primary keyword ----------------
        # (highest count among primary keywords → designate as focus keyword)
        primary_density = 0.0
        focus_keyword = primary_keywords[0] if primary_keywords else ""

        if primary_keywords:
            best = max(primary_keywords, key=lambda k: keyword_counts.get(k, 0))
            focus_keyword = best
            primary_density = keyword_densities.get(best, 0.0)

        # ---- Compute overall SEO score -----------------------------------
        # Simple heuristic: are the important keywords present and well-dosed?
        seo_score = self._compute_seo_score(
            primary_keywords=primary_keywords,
            keyword_densities=keyword_densities,
            present_long_tail=present_long_tail,
            long_tail=long_tail,
        )

        return {
            "keyword_counts": keyword_counts,
            "keyword_densities": keyword_densities,
            "primary_density": primary_density,
            "focus_keyword": focus_keyword,
            "missing_keywords": missing_keywords,
            "underused_keywords": underused_keywords,
            "well_optimised": well_optimised,
            "overused_keywords": overused_keywords,
            "present_long_tail": present_long_tail,
            "absent_long_tail": absent_long_tail,
            "seo_score": seo_score,
            "seo_grade": self._score_to_grade(seo_score),
            "total_words_analysed": total_words,
        }

    @staticmethod
    def _compute_seo_score(
        primary_keywords: List[str],
        keyword_densities: Dict[str, float],
        present_long_tail: List[str],
        long_tail: List[str],
    ) -> int:
        """
        Compute a simple 0-100 SEO score based on keyword presence and density.

        Scoring breakdown:
          - 40 pts: primary keyword density in 1.0-2.5% range
          - 30 pts: at least 2 secondary/primary keywords present (density > 0)
          - 30 pts: at least 1 long-tail phrase present

        Parameters
        ----------
        primary_keywords : list[str]
        keyword_densities : dict[str, float]
        present_long_tail : list[str]
        long_tail : list[str]

        Returns
        -------
        int  Score from 0 to 100.
        """
        score = 0

        # ---- 40 pts: primary keyword in sweet spot ------------------
        if primary_keywords:
            primary_kw = primary_keywords[0]
            density = keyword_densities.get(primary_kw, 0.0)
            if 1.0 <= density <= 2.5:
                score += 40
            elif 0.5 <= density < 1.0:
                score += 25
            elif 0.1 <= density < 0.5:
                score += 10
            elif density > 2.5:
                score += 15  # present but over-stuffed

        # ---- 30 pts: multiple keywords present ----------------------
        present_count = sum(
            1 for kw, density in keyword_densities.items() if density > 0.0
        )
        if present_count >= 5:
            score += 30
        elif present_count >= 3:
            score += 20
        elif present_count >= 1:
            score += 10

        # ---- 30 pts: long-tail coverage -----------------------------
        if long_tail:
            coverage = len(present_long_tail) / len(long_tail)
            score += int(30 * coverage)
        else:
            score += 15  # no long-tail data, partial credit

        return min(score, 100)

    @staticmethod
    def _score_to_grade(score: int) -> str:
        """
        Convert a numeric SEO score to a letter grade with label.

        Parameters
        ----------
        score : int  0-100

        Returns
        -------
        str  e.g. "A (Excellent)"
        """
        if score >= 85:
            return "A (Excellent)"
        elif score >= 70:
            return "B (Good)"
        elif score >= 55:
            return "C (Average)"
        elif score >= 40:
            return "D (Needs Work)"
        else:
            return "F (Poor)"

    # ==========================================================================
    # KEYWORD OPTIMISATION
    # ==========================================================================

    def _apply_keyword_optimisations(
        self,
        content: str,
        title: str,
        topic: str,
        kw_data: Dict[str, Any],
        keyword_analysis: Dict[str, Any],
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """
        Apply keyword optimisations to the article content.

        Strategy:
          1. If the primary keyword is absent from the first paragraph,
             try to insert it naturally near the top of the article.
          2. For each missing secondary keyword that has a natural insertion
             point (a paragraph discussing a related concept), insert it.
          3. If any long-tail phrase is absent and matches a section heading,
             consider inserting it into the opening of that section.
          4. If the title doesn't contain the primary keyword, modify it.

        IMPORTANT: We NEVER keyword-stuff.  Every insertion must read naturally.
        If no natural insertion point exists, we skip the keyword rather than
        forcing it in.  Quality over quantity.

        Parameters
        ----------
        content : str
            The article content from EditorAgent.
        title : str
            The article title.
        topic : str
            The article topic.
        kw_data : dict
            Keyword data from MCP.
        keyword_analysis : dict
            Density analysis from _analyse_keyword_density().

        Returns
        -------
        Tuple[str, List[Dict]]
            (optimised_content, list_of_optimisations_applied)
        """
        optimised = content
        optimisations: List[Dict[str, Any]] = []

        primary_keywords: List[str] = kw_data.get("primary", [])
        focus_keyword: str = keyword_analysis.get("focus_keyword", "")
        missing_keywords: List[str] = keyword_analysis.get("missing_keywords", [])
        absent_long_tail: List[str] = keyword_analysis.get("absent_long_tail", [])

        # ---- Optimisation 1: Ensure primary keyword appears in intro ---
        if primary_keywords:
            primary_kw = primary_keywords[0]
            optimised, opt = self._ensure_keyword_in_intro(
                content=optimised,
                keyword=primary_kw,
                topic=topic,
            )
            if opt:
                optimisations.append(opt)

        # ---- Optimisation 2: Insert missing secondary keywords ---------
        # Only attempt for up to 2 missing secondary keywords to avoid
        # over-engineering the content
        secondary_missing = [
            kw for kw in missing_keywords if kw not in primary_keywords
        ][:2]

        for missing_kw in secondary_missing:
            optimised, opt = self._insert_keyword_naturally(
                content=optimised,
                keyword=missing_kw,
                topic=topic,
            )
            if opt:
                optimisations.append(opt)

        # ---- Optimisation 3: Insert absent long-tail in conclusion -----
        # Long-tail phrases often match "how to" or "why" question formats
        # that fit naturally in conclusion / FAQ sections
        if absent_long_tail:
            best_long_tail = absent_long_tail[0]
            optimised, opt = self._insert_long_tail_in_conclusion(
                content=optimised,
                phrase=best_long_tail,
                topic=topic,
            )
            if opt:
                optimisations.append(opt)

        # ---- Optimisation 4: Add SEO-enhancing alt text annotation -----
        # Real CMS systems use this to populate image alt text
        if focus_keyword:
            image_annotation = (
                f"\n\n<!-- SEO: If adding images, use alt text containing "
                f"'{focus_keyword}' to reinforce keyword relevance. "
                f"Suggested image search terms: {focus_keyword}, "
                f"{topic} diagram, {topic} illustration -->"
            )
            if "<!-- SEO:" not in optimised:
                optimised = optimised.rstrip() + image_annotation
                optimisations.append(
                    {
                        "type": "image_seo",
                        "keyword": focus_keyword,
                        "action": "Added image alt-text SEO annotation",
                        "rationale": (
                            "Images with keyword-rich alt text contribute to "
                            "image search rankings and reinforce topical relevance."
                        ),
                    }
                )

        # ---- Optimisation 5: Internal link suggestion -------------------
        related_topics: List[str] = kw_data.get("related_topics", [])
        if related_topics:
            internal_link_note = (
                f"\n\n<!-- SEO: Internal linking opportunities — consider linking "
                f"to articles about: "
                + ", ".join(f"'{rt}'" for rt in related_topics[:3])
                + ". Internal links improve crawlability and distribute page authority. -->"
            )
            if "Internal linking" not in optimised:
                optimised = optimised.rstrip() + internal_link_note
                optimisations.append(
                    {
                        "type": "internal_links",
                        "related_topics": related_topics[:3],
                        "action": "Added internal link opportunity annotations",
                        "rationale": (
                            "Internal links to related content improve site "
                            "crawlability, reduce bounce rate, and distribute "
                            "link equity across the content portfolio."
                        ),
                    }
                )

        return optimised, optimisations

    def _ensure_keyword_in_intro(
        self,
        content: str,
        keyword: str,
        topic: str,
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """
        Check if the primary keyword appears in the introduction section.
        If not, attempt to insert it naturally into the first prose paragraph.

        The introduction is the most important location for the primary keyword
        from an SEO perspective — search engine crawlers weight early occurrences
        of keywords more heavily.

        Parameters
        ----------
        content : str
        keyword : str
        topic : str

        Returns
        -------
        Tuple[str, Optional[Dict]]
            (revised_content, optimisation_record_or_None)
        """
        # Find the introduction section
        intro_match = re.search(
            r"(## Introduction|## Overview)\n\n(.+?)(?=\n\n##|\Z)",
            content,
            re.DOTALL,
        )

        if not intro_match:
            return content, None

        intro_text = intro_match.group(2)
        kw_lower = keyword.lower()

        # Check if keyword is already in the introduction
        if kw_lower in intro_text.lower():
            self._log(
                f"  ✓ Primary keyword '{keyword}' already present in introduction."
            )
            return content, None

        # Keyword is absent from the intro — try to insert it
        # Strategy: find the first sentence that mentions the topic, and
        # append "— a field known as [keyword]" or similar
        sentences = re.split(r"(?<=[.!?])\s+", intro_text)
        if not sentences:
            return content, None

        # Find the first sentence that could host the keyword naturally
        topic_lower = topic.lower()
        target_sentence = None
        target_idx = -1

        for i, sent in enumerate(sentences[:4]):  # check first 4 sentences
            if topic_lower in sent.lower() or any(
                word in sent.lower() for word in topic_lower.split()[:2]
            ):
                target_sentence = sent
                target_idx = i
                break

        if target_sentence is None and sentences:
            # Fall back to the second sentence (first is usually the hook)
            target_sentence = sentences[min(1, len(sentences) - 1)]
            target_idx = min(1, len(sentences) - 1)

        if target_sentence is None:
            return content, None

        # Construct a natural insertion
        # We append the keyword in parentheses if it's not already the topic name
        if keyword.lower() != topic_lower and topic_lower not in keyword.lower():
            # Insert as a clarifying phrase
            if target_sentence.endswith("."):
                insertion = target_sentence[:-1]
                insertion += (
                    f" — and the broader field of {keyword} — "
                    f"is at the centre of this transformation."
                )
            else:
                insertion = target_sentence + f" {keyword.title()} is central to this."
        else:
            return content, None  # keyword IS the topic, already present implicitly

        # Replace the target sentence in the intro text
        new_intro = intro_text.replace(target_sentence, insertion, 1)

        # Replace the intro text in the full content
        revised = content.replace(intro_text, new_intro, 1)

        if revised == content:
            return content, None

        self._log(
            f"  ✓ Inserted primary keyword '{keyword}' into introduction paragraph."
        )

        return revised, {
            "type": "keyword_insertion",
            "keyword": keyword,
            "location": "introduction",
            "action": f"Inserted primary keyword '{keyword}' into the introduction",
            "before": target_sentence[:80],
            "after": insertion[:80],
            "rationale": (
                f"Primary keyword '{keyword}' was absent from the introduction. "
                f"Search engines weight keyword occurrences in the first 100 words "
                f"more heavily than later occurrences. Natural insertion improves "
                f"topical relevance signals without affecting readability."
            ),
        }

    def _insert_keyword_naturally(
        self,
        content: str,
        keyword: str,
        topic: str,
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """
        Attempt to insert a missing secondary keyword at a natural location
        in the body of the article.

        Strategy: find a paragraph that discusses a concept closely related
        to the keyword (using word-overlap heuristics) and append the keyword
        as a parenthetical or synonym.

        Parameters
        ----------
        content : str
        keyword : str
        topic : str

        Returns
        -------
        Tuple[str, Optional[Dict]]
        """
        kw_words = set(keyword.lower().split())
        paragraphs = content.split("\n\n")
        best_paragraph = None
        best_paragraph_idx = -1
        best_overlap = 0

        # Find the paragraph with the highest word-overlap with the keyword
        for i, para in enumerate(paragraphs):
            if para.startswith("#") or para.startswith("---") or len(para) < 80:
                continue
            para_words = set(re.findall(r"\b\w+\b", para.lower()))
            overlap = len(kw_words & para_words)
            if overlap > best_overlap:
                best_overlap = overlap
                best_paragraph = para
                best_paragraph_idx = i

        if best_paragraph is None or best_overlap == 0:
            self._log(
                f"  ℹ  No natural insertion point found for keyword '{keyword}'. "
                f"Skipping — forcing keywords into unrelated contexts hurts quality."
            )
            return content, None

        # Build a natural insertion sentence
        kw_title = keyword.title()
        insertion_sentence = (
            f" This connects closely to the broader concept of {kw_title}, "
            f"which plays a significant role in shaping the {topic} landscape."
        )

        # Append to the end of the chosen paragraph (before the last sentence)
        sentences = re.split(r"(?<=[.!?])\s+", best_paragraph)
        if len(sentences) >= 2:
            # Insert before the last sentence
            new_para = (
                " ".join(sentences[:-1]) + insertion_sentence + " " + sentences[-1]
            )
        else:
            # Append to the only sentence
            new_para = best_paragraph.rstrip(".") + insertion_sentence

        # Replace in full content
        new_paragraphs = paragraphs[:]
        new_paragraphs[best_paragraph_idx] = new_para
        revised = "\n\n".join(new_paragraphs)

        if revised == content:
            return content, None

        self._log(
            f"  ✓ Inserted secondary keyword '{keyword}' into paragraph "
            f"{best_paragraph_idx + 1} (overlap score: {best_overlap})."
        )

        return revised, {
            "type": "keyword_insertion",
            "keyword": keyword,
            "location": f"paragraph_{best_paragraph_idx + 1}",
            "action": f"Naturally inserted missing secondary keyword '{keyword}'",
            "rationale": (
                f"Keyword '{keyword}' was absent from the content. "
                f"Found a contextually relevant paragraph (word-overlap: {best_overlap}) "
                f"and inserted a natural bridging sentence to include the keyword. "
                f"Secondary keywords support semantic richness and topic authority."
            ),
        }

    def _insert_long_tail_in_conclusion(
        self,
        content: str,
        phrase: str,
        topic: str,
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """
        Insert a long-tail keyword phrase into the conclusion section.

        Long-tail phrases are typically question-format keywords like
        "how does artificial intelligence work" or "AI impact on jobs".
        The conclusion/FAQ area is a natural home for these because readers
        who reach the conclusion are actively seeking clear answers.

        Parameters
        ----------
        content : str
        phrase : str
        topic : str

        Returns
        -------
        Tuple[str, Optional[Dict]]
        """
        phrase_lower = phrase.lower()

        # Check if phrase already exists
        if phrase_lower in content.lower():
            return content, None

        # Find the final section (Final Thoughts or Conclusion)
        conclusion_patterns = [
            r"(## Final Thoughts\n\n)",
            r"(## Conclusion\n\n)",
            r"(## Takeaway\n\n)",
        ]

        for pattern in conclusion_patterns:
            match = re.search(pattern, content)
            if match:
                # Build a natural sentence incorporating the long-tail phrase
                # For "how does X work" style: "Many readers ask [phrase]..."
                # For "X impact on Y" style: "The [phrase] is becoming clearer..."
                if phrase_lower.startswith("how "):
                    natural_sentence = (
                        f"A common question is: *{phrase}?* "
                        f"The answer becomes clearer as {topic} matures and "
                        f"real-world data accumulates.\n\n"
                    )
                elif phrase_lower.startswith("what "):
                    natural_sentence = (
                        f"*{phrase.capitalize()}* — this question is at the heart "
                        f"of the current conversation about {topic}.\n\n"
                    )
                else:
                    natural_sentence = (
                        f"Understanding {phrase} is increasingly important "
                        f"for anyone engaging with {topic} professionally.\n\n"
                    )

                # Insert after the conclusion heading
                insert_pos = match.end()
                revised = content[:insert_pos] + natural_sentence + content[insert_pos:]

                self._log(
                    f"  ✓ Inserted long-tail phrase '{phrase}' into conclusion section."
                )

                return revised, {
                    "type": "long_tail_insertion",
                    "phrase": phrase,
                    "location": "conclusion",
                    "action": f"Inserted long-tail phrase '{phrase}' in conclusion",
                    "rationale": (
                        f"Long-tail phrase '{phrase}' was absent from the content. "
                        f"Long-tail keywords typically have lower competition and higher "
                        f"search intent, meaning visitors who find the article via this "
                        f"phrase are more likely to engage deeply. "
                        f"The conclusion is a natural location for question-format phrases."
                    ),
                }

        return content, None

    # ==========================================================================
    # SEO METADATA GENERATION
    # ==========================================================================

    def _generate_seo_metadata(
        self,
        title: str,
        content: str,
        topic: str,
        kw_data: Dict[str, Any],
        keyword_analysis: Dict[str, Any],
        word_count: int,
        readability_full: Dict[str, Any],
        edits_made: List[Dict],
        style_used: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Generate the complete SEO metadata package for the article.

        This package contains everything a CMS or static site generator needs
        to optimise the article's search engine presence:

          meta_title        : 50-60 char title for <title> tag and SERP display
          meta_description  : 150-160 char description for SERP snippet
          primary_keyword   : the single most important keyword to rank for
          keywords          : list of all target keywords (for <meta keywords>)
          tags              : CMS tags / categories
          slug              : URL-safe article identifier
          canonical_url     : suggested canonical URL path
          open_graph        : OG metadata for Facebook / LinkedIn sharing
          twitter_card      : Twitter Card metadata
          schema_type       : JSON-LD schema type suggestion
          seo_score         : numeric SEO quality score (0-100)
          seo_grade         : letter grade corresponding to the score
          readability_brief : key readability metrics for the metadata file

        Parameters
        ----------
        title, content, topic, kw_data, keyword_analysis, word_count,
        readability_full, edits_made, style_used : (see type hints above)

        Returns
        -------
        Dict[str, Any]
            Complete SEO metadata package.
        """
        primary_keywords: List[str] = kw_data.get("primary", [])
        secondary_keywords: List[str] = kw_data.get("secondary", [])
        long_tail: List[str] = kw_data.get("long_tail", [])
        focus_keyword: str = keyword_analysis.get("focus_keyword", "")
        seo_score: int = keyword_analysis.get("seo_score", 0)

        # ---- Meta Title (50-60 chars, keyword near front) ----------------
        meta_title = self._craft_meta_title(title, focus_keyword, topic)
        self._log(f"  Meta title ({len(meta_title)} chars): '{meta_title}'")

        # ---- Meta Description (150-160 chars) ----------------------------
        meta_description = self._craft_meta_description(
            content=content,
            topic=topic,
            focus_keyword=focus_keyword,
            long_tail=long_tail,
        )
        self._log(
            f"  Meta description ({len(meta_description)} chars): '{meta_description[:60]}...'"
        )

        # ---- Tags / Categories -------------------------------------------
        tags = self._select_tags(
            topic=topic,
            primary_keywords=primary_keywords,
            secondary_keywords=secondary_keywords,
            kw_data=kw_data,
        )
        self._log(f"  Tags: {tags}")

        # ---- URL Slug ----------------------------------------------------
        slug = self._generate_slug(title, focus_keyword)
        canonical_url = f"/articles/{slug}"
        self._log(f"  Slug: {slug}")

        # ---- Open Graph Metadata -----------------------------------------
        og_metadata = {
            "og:title": meta_title,
            "og:description": meta_description,
            "og:type": "article",
            "og:url": canonical_url,
            "og:image:alt": f"{focus_keyword} — {topic} article illustration",
        }

        # ---- Twitter Card Metadata ----------------------------------------
        twitter_metadata = {
            "twitter:card": "summary_large_image",
            "twitter:title": meta_title,
            "twitter:description": (
                meta_description[:120] + "..."
                if len(meta_description) > 120
                else meta_description
            ),
            "twitter:image:alt": og_metadata["og:image:alt"],
        }

        # ---- Schema.org Type Suggestion ----------------------------------
        content_type = style_used.get("content_type", "blog")
        if content_type == "technical":
            schema_type = "ScholarlyArticle"
        elif content_type == "news":
            schema_type = "NewsArticle"
        else:
            schema_type = "BlogPosting"

        # ---- JSON-LD Structured Data Snippet ----------------------------
        json_ld = {
            "@context": "https://schema.org",
            "@type": schema_type,
            "headline": title,
            "description": meta_description,
            "keywords": ", ".join(primary_keywords[:3]),
            "wordCount": word_count,
            "publisher": {"@type": "Organization", "name": "ContentForge"},
            "inLanguage": "en-US",
        }

        # ---- Reading time (minutes) for article metadata ----------------
        reading_time = readability_full.get(
            "reading_time_minutes", round(word_count / 238, 1)
        )

        # ---- Readability brief for metadata file ------------------------
        readability_brief = {
            "flesch_score": readability_full.get("flesch_score", 0),
            "reading_level": readability_full.get("reading_level", "Unknown"),
            "avg_sentence_length": readability_full.get("avg_sentence_length", 0),
            "word_count": word_count,
            "reading_time_minutes": reading_time,
            "paragraph_count": readability_full.get("paragraph_count", 0),
        }

        # ---- Competitor keyword targets ----------------------------------
        competitor_keywords: List[str] = kw_data.get("competitor_keywords", [])

        return {
            # Core SEO metadata
            "meta_title": meta_title,
            "meta_description": meta_description,
            "primary_keyword": focus_keyword,
            "keywords": (primary_keywords + secondary_keywords)[:10],
            "tags": tags,
            "slug": slug,
            "canonical_url": canonical_url,
            # Social sharing
            "open_graph": og_metadata,
            "twitter_card": twitter_metadata,
            # Structured data
            "schema_type": schema_type,
            "json_ld": json_ld,
            # Quality metrics
            "seo_score": seo_score,
            "seo_grade": keyword_analysis.get("seo_grade", "C"),
            "readability": readability_brief,
            # Content metadata
            "word_count": word_count,
            "reading_time_minutes": reading_time,
            "content_type": content_type,
            # Pipeline provenance
            "edits_made": edits_made,
            "competitor_opportunities": competitor_keywords,
            "long_tail_targeted": long_tail[:3],
        }

    def _craft_meta_title(
        self,
        title: str,
        focus_keyword: str,
        topic: str,
    ) -> str:
        """
        Craft an optimised meta title tag (50-60 characters).

        Rules:
          - Must contain the primary/focus keyword
          - Ideally starts with or near the keyword (Google truncates after ~60 chars)
          - Should differ slightly from the H1 title to avoid duplication
          - Brand name can be appended with " | " separator if space allows

        Parameters
        ----------
        title : str
        focus_keyword : str
        topic : str

        Returns
        -------
        str  50-60 character meta title.
        """
        topic_title = topic.title()
        kw_lower = focus_keyword.lower() if focus_keyword else topic.lower()

        # Try to build a 50-60 char title that starts with the keyword
        candidate = f"{focus_keyword.title()}: {topic_title} Guide 2024"
        if 50 <= len(candidate) <= 65:
            return candidate

        # Shorter version
        candidate = f"{topic_title}: The Complete Guide | ContentForge"
        if 50 <= len(candidate) <= 65:
            return candidate

        # Trim the original title to fit
        if len(title) <= 60:
            return title

        # Truncate at the last word boundary before char 57
        truncated = title[:57].rsplit(" ", 1)[0]
        if len(truncated) < 40:
            # Too short — use topic-based fallback
            return f"{topic_title}: Key Facts, Trends & Insights 2024"[:60]

        return truncated + "..."

    def _craft_meta_description(
        self,
        content: str,
        topic: str,
        focus_keyword: str,
        long_tail: List[str],
    ) -> str:
        """
        Craft an optimised meta description (150-160 characters).

        Rules:
          - Should contain the primary keyword
          - Should include a value proposition (what will the reader learn?)
          - Should end with an implicit or explicit CTA
          - Must be 150-160 characters (Google truncates longer descriptions)
          - Should NOT be a direct copy of any sentence in the article
            (Google may show a different excerpt anyway; unique descriptions
             signal intent and improve click-through rates)

        Parameters
        ----------
        content : str
        topic : str
        focus_keyword : str
        long_tail : list[str]

        Returns
        -------
        str  150-160 character meta description.
        """
        topic_title = topic.title()
        kw = focus_keyword if focus_keyword else topic

        # Template-based meta descriptions tuned to 150-160 chars
        templates = [
            (
                f"Discover everything about {kw}: key facts, latest trends, expert "
                f"insights, and what it means for your future. "
                f"Read the complete {topic_title} guide."
            ),
            (
                f"Explore the world of {kw} — from groundbreaking statistics to "
                f"future predictions. Your essential {topic_title} resource, "
                f"updated for 2024."
            ),
            (
                f"What is {kw} and why does it matter? "
                f"Get the facts, understand the trends, and learn how "
                f"{topic_title} is shaping our world. "
                f"Start reading now."
            ),
        ]

        # Incorporate a long-tail phrase if available and it fits
        if long_tail:
            phrase = long_tail[0]
            lt_template = (
                f"Wondering about {phrase}? "
                f"This guide covers {kw}, key statistics, expert voices, "
                f"and the future of {topic_title}."
            )
            templates.insert(0, lt_template)

        # Pick the template closest to 155 chars (sweet spot)
        best = min(templates, key=lambda t: abs(len(t) - 155))

        # Ensure it's within limits
        if len(best) > 160:
            best = best[:157] + "..."
        elif len(best) < 120:
            best = best + f" Learn more about {topic_title} today."
            if len(best) > 160:
                best = best[:157] + "..."

        return best

    def _select_tags(
        self,
        topic: str,
        primary_keywords: List[str],
        secondary_keywords: List[str],
        kw_data: Dict[str, Any],
    ) -> List[str]:
        """
        Select the best tags/categories for the article from keyword data.

        Tags in a CMS serve several purposes:
          - Topical organisation (helps readers navigate related content)
          - Internal linking opportunities (tag archive pages)
          - Semantic signals to search engines about topical clusters

        Selection strategy:
          - Always include the topic itself as a tag
          - Include all primary keywords as tags (these ARE the main topics)
          - Include up to 3 secondary keywords as sub-topic tags
          - Include related topics from the keyword database
          - Limit total tags to 8 (more than this dilutes topical focus)

        Parameters
        ----------
        topic, primary_keywords, secondary_keywords, kw_data : (see above)

        Returns
        -------
        List[str]  Up to 8 tags, deduplicated, title-cased.
        """
        seen: set = set()
        tags: List[str] = []

        def add_tag(tag: str) -> None:
            """Add a tag if it hasn't been added yet (case-insensitive dedup)."""
            normalised = tag.strip().lower()
            if normalised and normalised not in seen and len(tags) < 8:
                seen.add(normalised)
                tags.append(tag.strip().title())

        # Topic itself is always the first tag
        add_tag(topic)

        # Primary keywords
        for kw in primary_keywords:
            add_tag(kw)

        # Top secondary keywords (most likely to be good tags)
        for kw in secondary_keywords[:3]:
            add_tag(kw)

        # Related topics from the keyword database
        for related in kw_data.get("related_topics", [])[:3]:
            add_tag(related)

        # Add "2024" as a year tag if space allows (improves freshness signals)
        if len(tags) < 8:
            add_tag("2024")

        return tags

    @staticmethod
    def _generate_slug(title: str, focus_keyword: str) -> str:
        """
        Generate a URL-safe slug from the title.

        Rules:
          - Lowercase only
          - Replace spaces and special chars with hyphens
          - Remove stop words that add no SEO value
          - Maximum 70 characters
          - Should contain the focus keyword if possible

        Parameters
        ----------
        title : str
        focus_keyword : str

        Returns
        -------
        str  URL slug, e.g. "complete-guide-to-artificial-intelligence"
        """
        # Start with the focus keyword if it gives a better slug than the title
        base = title.lower()

        # Remove common stop words that bloat the slug
        stop_words = {
            "the",
            "a",
            "an",
            "and",
            "or",
            "but",
            "in",
            "on",
            "at",
            "to",
            "for",
            "of",
            "with",
            "by",
            "from",
            "is",
            "are",
            "was",
            "were",
            "be",
            "been",
            "being",
            "have",
            "has",
            "had",
            "do",
            "does",
            "did",
            "will",
            "would",
            "could",
            "should",
            "may",
            "might",
            "shall",
            "right",
            "now",
            "need",
            "know",
            "you",
            "your",
            "it",
            "its",
            "this",
            "that",
            "these",
            "those",
            "what",
            "everything",
        }

        # Replace special chars with spaces
        base = re.sub(r"[^a-z0-9\s-]", " ", base)
        # Collapse whitespace
        base = re.sub(r"\s+", " ", base).strip()

        # Split and filter stop words
        words = [w for w in base.split() if w not in stop_words and len(w) > 1]

        # Build slug
        slug = "-".join(words)

        # Trim to max length at word boundary
        if len(slug) > 70:
            slug = slug[:70].rsplit("-", 1)[0]

        # Ensure focus keyword words appear somewhere in the slug
        if focus_keyword:
            kw_slug_part = re.sub(r"[^a-z0-9]", "-", focus_keyword.lower())
            kw_slug_part = re.sub(r"-+", "-", kw_slug_part).strip("-")
            if kw_slug_part not in slug:
                # Prepend a shortened version of the slug with the keyword
                slug = (kw_slug_part + "-" + slug)[:70].rsplit("-", 1)[0]

        return slug.strip("-") or re.sub(r"\s+", "-", title.lower())[:70]

    # ==========================================================================
    # FALLBACK HELPERS
    # ==========================================================================

    @staticmethod
    def _derive_fallback_keywords(topic: str) -> Dict[str, Any]:
        """
        Derive minimal keyword data from the topic string when the MCP tool fails.

        Ensures the SEOAgent can always produce SOME metadata even when the
        keyword database is unavailable.  The quality will be lower (no real
        search volume data) but the pipeline can still complete successfully.

        ACP/MCP CONCEPT: Graceful Degradation
        ----------------------------------------
        Well-designed agents handle tool failures gracefully.  The pipeline
        should produce a publishable article even when optional data sources
        are unavailable.  Degraded quality is preferable to a complete failure.

        Parameters
        ----------
        topic : str

        Returns
        -------
        Dict[str, Any]  Minimal keyword structure matching the MCP tool's output.
        """
        words = topic.lower().split()
        return {
            "topic": topic,
            "found": False,
            "primary": [topic, f"{topic} guide"],
            "secondary": words if len(words) > 1 else [topic],
            "long_tail": [
                f"what is {topic}",
                f"how does {topic} work",
                f"benefits of {topic}",
                f"future of {topic}",
            ],
            "search_volume": {topic: 5000},
            "related_topics": [],
            "competitor_keywords": [],
        }

    # ==========================================================================
    # OUTPUT HELPERS
    # ==========================================================================

    def _print_keyword_density_report(
        self,
        keyword_analysis: Dict[str, Any],
        kw_data: Dict[str, Any],
    ) -> None:
        """
        Print a formatted keyword density report to the console.

        This report shows the SEO status of every target keyword at a glance,
        using visual indicators to communicate density status:
          ✓  Green zone: 1.0-2.5% (well-optimised)
          ↑  Below target: < 0.8% (could add more)
          ✗  Absent: 0% (not present at all)
          ⚠  Over-used: > 2.5% (stuffing risk)

        Parameters
        ----------
        keyword_analysis : dict
        kw_data : dict
        """
        counts = keyword_analysis.get("keyword_counts", {})
        densities = keyword_analysis.get("keyword_densities", {})
        primary_keywords = kw_data.get("primary", [])
        secondary_keywords = kw_data.get("secondary", [])

        print("\n  ╔═ SEO KEYWORD DENSITY REPORT ════════════════════════════════╗")
        print(
            f"  ║  SEO Score : {keyword_analysis.get('seo_score', '?')}/100  "
            f"Grade: {keyword_analysis.get('seo_grade', '?')}"
        )
        print(
            f"  ║  Focus KW  : '{keyword_analysis.get('focus_keyword', '?')}'  "
            f"(density: {keyword_analysis.get('primary_density', 0):.2f}%)"
        )
        print("  ╠══════════════════════════════════════════════════════════════╣")
        print("  ║  Keyword                      │ Count │ Density │ Status")
        print("  ║  ─────────────────────────────┼───────┼─────────┼────────")

        all_kws = [(kw, "PRIMARY") for kw in primary_keywords] + [
            (kw, "secondary") for kw in secondary_keywords[:4]
        ]

        for kw, tier in all_kws:
            count = counts.get(kw, 0)
            density = densities.get(kw, 0.0)
            if density == 0.0:
                status = "✗ ABSENT"
            elif density < 0.8:
                status = "↑ low"
            elif density <= 2.5:
                status = "✓ good"
            else:
                status = "⚠ high"

            kw_display = kw[:30].ljust(30)
            print(f"  ║  {kw_display} │ {count:>5} │ {density:>6.2f}% │ {status}")

        print("  ╠══════════════════════════════════════════════════════════════╣")
        missing = keyword_analysis.get("missing_keywords", [])
        overused = keyword_analysis.get("overused_keywords", [])
        if missing:
            print(f"  ║  Missing  : {', '.join(missing[:4])}")
        if overused:
            print(f"  ║  Overused : {', '.join(overused[:3])}")
        print("  ╚══════════════════════════════════════════════════════════════╝")

    def _print_seo_metadata_summary(self, seo_metadata: Dict[str, Any]) -> None:
        """
        Print a formatted summary of the generated SEO metadata.

        Parameters
        ----------
        seo_metadata : dict
        """
        print("\n  ╔═ GENERATED SEO METADATA ═════════════════════════════════════╗")
        meta_title = seo_metadata.get("meta_title", "")
        meta_desc = seo_metadata.get("meta_description", "")
        slug = seo_metadata.get("slug", "")
        tags = seo_metadata.get("tags", [])
        schema = seo_metadata.get("schema_type", "")
        seo_score = seo_metadata.get("seo_score", 0)
        seo_grade = seo_metadata.get("seo_grade", "?")

        print(f"  ║  Meta Title    ({len(meta_title):>3} chars): {meta_title}")
        print(f"  ║  Meta Desc     ({len(meta_desc):>3} chars): {meta_desc[:65]}...")
        print(f"  ║  Slug          : /{slug}")
        print(f"  ║  Tags          : {', '.join(tags)}")
        print(f"  ║  Schema type   : {schema}")
        print(f"  ║  SEO Score     : {seo_score}/100 — {seo_grade}")
        print(
            f"  ║  Read time     : {seo_metadata.get('reading_time_minutes', '?')} min"
        )
        print("  ╚══════════════════════════════════════════════════════════════╝")

    # ==========================================================================
    # REGISTRY METADATA
    # ==========================================================================

    def get_agent_info(self) -> ACPAgentInfo:
        """
        Return ACPAgentInfo describing this agent for the ACP Agent Registry.

        Returns
        -------
        ACPAgentInfo
        """
        return ACPAgentInfo(
            agent_id=self.AGENT_ID,
            name="SEOAgent",
            description=(
                "Stage 4: Receives the editorially-reviewed article from EditorAgent "
                "and applies SEO optimisation. Uses the MCP keyword tool to fetch "
                "keyword targets, analyses keyword density in the current content, "
                "naturally inserts missing keywords, and generates a complete SEO "
                "metadata package (meta title, meta description, tags, slug, Open Graph, "
                "Twitter Card, JSON-LD). Forwards the fully-optimised content and "
                "metadata to PublisherAgent."
            ),
            input_schema={
                "title": "str — article title from EditorAgent",
                "revised_content": "str — editorially improved Markdown content",
                "edits_made": "list[dict] — editorial history (pass-through)",
                "readability_score": "float — Flesch score (pass-through)",
                "readability_full": "dict — full readability metrics (pass-through)",
                "grammar_quality": "str — overall grammar quality (pass-through)",
                "word_count": "int — word count of revised content",
                "topic": "str — article topic",
                "style_used": "dict — style guide metadata (pass-through)",
                "research_brief": "dict — original research data (pass-through)",
            },
            output_schema={
                "title": "str — article title (unchanged)",
                "final_content": "str — SEO-optimised Markdown content",
                "seo_metadata": "dict — complete SEO package (meta_title, meta_description, tags, slug, og, twitter, json_ld)",
                "keyword_analysis": "dict — keyword density report for all target keywords",
                "kw_optimisations": "list[dict] — record of keyword insertions applied",
                "word_count": "int — final word count",
                "topic": "str — passed through",
                "edits_made": "list[dict] — passed through from EditorAgent",
                "readability_score": "float — passed through",
                "readability_full": "dict — passed through",
                "grammar_quality": "str — passed through",
                "style_used": "dict — passed through",
                "research_brief": "dict — passed through",
                "seo_optimised_by": "str — agent_id of this agent ('seo')",
            },
            status=self._status,
            pipeline_stage=4,
            tags=["seo", "keywords", "metadata", "optimisation", "stage-4"],
            metadata={
                "mcp_tools_used": ["get_seo_keywords"],
                "sends_to": ["publisher"],
                "receives_from": ["editor"],
                "output_format": "markdown + seo_metadata dict",
            },
        )
