# =============================================================================
# agents/publisher_agent.py  —  PublisherAgent
# =============================================================================
#
# ROLE IN THE PIPELINE
# --------------------
# The PublisherAgent is Stage 5 (final stage) of the ContentForge pipeline.
# It receives the fully-researched, written, edited, and SEO-optimised article
# from the SEOAgent and performs the last critical step: publishing.
#
# "Publishing" in the ContentForge pipeline means:
#   1. Writing the final article as a Markdown file with YAML front matter
#      to the data/published/ directory (simulating a CMS or static site push)
#   2. Writing a comprehensive JSON metadata sidecar file alongside the article
#   3. Printing a human-readable publication summary to the console
#   4. Broadcasting a pipeline-wide completion announcement via the ACP bus
#      so every agent knows the article is live
#
# The PublisherAgent is special in the pipeline because it is the ONLY agent
# that:
#   a) Writes output to the file system (via MCP publish_article tool)
#   b) Sends a BROADCAST message (all others send unicast RESPONSE messages)
#   c) Marks the entire pipeline as complete
#
# WHAT IT DOES
# ------------
#   1. Receives a RESPONSE message from SEOAgent with payload:
#      { title, final_content, seo_metadata, keyword_analysis,
#        edits_made, readability_score, research_brief, word_count, ... }
#
#   2. Validates and assembles the final article content + metadata
#
#   3. Uses MCP tool "publish_article" to write:
#        - <slug>.md      : Full Markdown article with YAML front matter
#        - <slug>_meta.json: Complete JSON metadata (SEO, pipeline, quality)
#
#   4. Computes a publication summary with quality metrics
#
#   5. Broadcasts "Article Published: {title}" to ALL agents via ACP bus
#
#   6. Prints a formatted publication report to the console
#
# ACP FLOW
# --------
#   [SEOAgent]
#         │  RESPONSE  { title, final_content, seo_metadata,
#         │              keyword_analysis, edits_made, readability_score, ... }
#         ▼
#   [PublisherAgent]  ──MCP──▶  publish_article(title, content, metadata)
#         │  BROADCAST  { announcement: "Article Published: <title>",
#         │               url: "/articles/<slug>",
#         │               word_count, seo_score, pipeline_stages_completed }
#         ▼
#   [ALL AGENTS]   (broadcast — everyone receives this)
#
# MCP TOOLS USED
# --------------
#   publish_article(title, content, metadata)  → writes .md + _meta.json files
#
# ACP MESSAGE TYPES
# -----------------
#   Receives : ACPMessageType.RESPONSE   (from "seo")
#              ACPMessageType.BROADCAST  (ignored gracefully — from other agents)
#   Sends    : ACPMessageType.BROADCAST  (to ALL agents — "article published!")
#              ACPMessageType.ERROR      (to sender, on critical failure)
#
# WHY A BROADCAST AT THE END?
# ----------------------------
# The PublisherAgent's final BROADCAST demonstrates a key ACP pattern:
# using broadcasts as "pipeline completion events."  In a real multi-agent
# system, other agents might subscribe to "article published" events to:
#   - Trigger social media promotion agents
#   - Notify a content analytics agent to start tracking performance
#   - Alert a human editor dashboard that a new piece is live
#   - Kick off a distribution agent to syndicate the content
#
# Because ALL agents receive the broadcast via the ACP bus, any of these
# downstream consumers can be added to the pipeline WITHOUT modifying the
# PublisherAgent — they just subscribe to the bus and react to BROADCAST
# messages.  This is ACP's extensibility guarantee in action.
# =============================================================================

import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from acp.agent_registry import ACPAgentInfo, ACPAgentRegistry
from acp.message import ACPContentType, ACPMessage, ACPMessageType
from acp.message_bus import ACPMessageBus
from mcp.mcp_client import MCPClient

from agents.base_agent import BaseACPAgent


class PublisherAgent(BaseACPAgent):
    """
    PublisherAgent — Stage 5 (Final) of the ContentForge pipeline.

    Responsible for taking the fully-processed article (researched, written,
    edited, and SEO-optimised) and performing the final publication step:
    writing it to the file system via MCP and broadcasting completion to
    all pipeline agents via the ACP bus.

    ACP CONCEPT: Terminal Pipeline Agents
    ----------------------------------------
    Every pipeline needs a "terminal agent" — one that marks the END of
    the processing chain and signals completion to the rest of the system.
    The PublisherAgent fills this role.

    Its distinguishing characteristic is the BROADCAST at the end:
    instead of routing a RESPONSE to a specific next agent (there is no
    "stage 6"), it publishes a BROADCAST to ALL agents.  This converts a
    linear pipeline into an event-driven architecture — interested consumers
    can react to the "article published" event without the PublisherAgent
    needing to know they exist.

    ACP CONCEPT: The Bus Completes the Loop
    -----------------------------------------
    The sequence of messages in a ContentForge run forms a closed loop:

        [orchestrator] → REQUEST  → [researcher]
        [researcher]   → RESPONSE → [writer]
        [writer]       → RESPONSE → [editor]
        [editor]       → RESPONSE → [seo]
        [seo]          → RESPONSE → [publisher]
        [publisher]    → BROADCAST→ [ALL agents]  ← closes the loop

    The ACP message history captures this entire loop, giving complete
    end-to-end observability of every content creation run.

    MCP CONCEPT: File System as an External Resource
    --------------------------------------------------
    Even though writing to a local file system seems trivial, the MCP
    pattern treats it exactly like any other external resource (a CMS API,
    an S3 bucket, a database).  The PublisherAgent calls the MCP
    "publish_article" tool and receives back a confirmation — it never
    directly calls open() or os.path.join().

    This means that changing from "write to local files" to "POST to a
    WordPress API" requires only a change to the MCPServer's tool
    implementation — the PublisherAgent code stays identical.

    Parameters
    ----------
    bus : ACPMessageBus
        The shared ACP message bus for this pipeline run.
    mcp_client : MCPClient
        Pre-configured MCP client for tool invocations.
    registry : ACPAgentRegistry, optional
        Shared agent registry for status updates.
    """

    AGENT_ID: str = "publisher"

    def __init__(
        self,
        bus: ACPMessageBus,
        mcp_client: MCPClient,
        registry: Optional[ACPAgentRegistry] = None,
    ) -> None:
        super().__init__(
            agent_id=self.AGENT_ID,
            name="PublisherAgent",
            bus=bus,
            mcp_client=mcp_client,
            registry=registry,
        )

        # ----------------------------------------------------------------
        # Track publications made in this session.
        # In a production system this would be stored in a database.
        # ----------------------------------------------------------------
        self._publications: List[Dict[str, Any]] = []

        self._log(
            "PublisherAgent ready.  Accepts RESPONSE messages from SEOAgent. "
            "Will publish the final article via MCP and broadcast completion "
            "to all pipeline agents."
        )

    # ==========================================================================
    # ACP MESSAGE DISPATCH
    # ==========================================================================

    def _dispatch(self, message: ACPMessage) -> None:
        """
        Route an incoming ACP message to the appropriate handler.

        ACP CONCEPT: Terminal Agent Message Handling
        ----------------------------------------------
        The PublisherAgent sits at the END of the pipeline.  This has an
        interesting consequence: it receives its own BROADCAST message back
        from the bus (since broadcasts go to all subscribers, including the
        sender — wait, actually the bus SKIPS the sender in broadcast mode,
        so the publisher won't receive its own broadcast).

        More importantly, it may receive BROADCAST messages from OTHER agents
        in future pipeline extensions.  Handling these gracefully (rather than
        crashing or processing them as SEO payloads) is essential for
        robustness.

        The PublisherAgent also handles ERROR messages from the SEOAgent —
        if SEO optimisation fails, the publisher can still attempt to publish
        the unoptimised content rather than abandoning the entire run.

        Parameters
        ----------
        message : ACPMessage
            The incoming ACP message to evaluate and optionally handle.
        """
        if message.message_type == ACPMessageType.BROADCAST:
            # In a future extended pipeline, another agent might broadcast
            # before the publisher receives the SEO output.  We log it and
            # continue without action.
            broadcast_payload = message.payload or {}
            announcement = broadcast_payload.get("announcement", str(broadcast_payload))
            self._log(
                f"Received BROADCAST from '{message.sender_id}': "
                f"'{str(announcement)[:80]}' — "
                f"PublisherAgent has no action for this broadcast. Ignoring."
            )
            return

        if message.message_type == ACPMessageType.ACK:
            self._log(f"Received ACK from '{message.sender_id}' — acknowledged.")
            return

        if message.message_type == ACPMessageType.ERROR:
            # ACP CONCEPT: Graceful Recovery from Upstream Errors
            # -------------------------------------------------------
            # If the SEOAgent (or any upstream agent) sends an ERROR message,
            # the PublisherAgent has two options:
            #   a) Halt and propagate the error → clean but article is lost
            #   b) Attempt to publish with degraded data → article is saved
            #
            # ContentForge opts for (a) to avoid publishing low-quality content.
            # In a production system you might implement a human-review queue
            # where failed pipeline runs are held for manual intervention.
            error_payload = message.payload or {}
            self._log(
                f"Received ERROR from '{message.sender_id}': "
                f"{error_payload.get('error', str(error_payload))}. "
                f"PublisherAgent cannot publish without a valid SEO-optimised payload. "
                f"Pipeline run aborted at publication stage."
            )
            self.mark_error(
                f"Upstream error from {message.sender_id} — publication aborted."
            )
            return

        if message.message_type != ACPMessageType.RESPONSE:
            self._log(
                f"Unexpected message type '{message.message_type.value}' from "
                f"'{message.sender_id}'. PublisherAgent only handles RESPONSE messages."
            )
            return

        # Process the SEO-optimised content from SEOAgent
        self._handle_publish_request(message)

    # ==========================================================================
    # PUBLISH REQUEST HANDLER
    # ==========================================================================

    def _handle_publish_request(self, message: ACPMessage) -> None:
        """
        Handle the SEO-optimised payload from SEOAgent and publish the article.

        This is the PublisherAgent's core work method.  The publication
        process has five phases:

          1. VALIDATE    — check the payload has all required fields
          2. ASSEMBLE    — prepare the final content and metadata for publication
          3. PUBLISH     — call the MCP publish_article tool to write files
          4. REPORT      — compute publication summary and print it
          5. BROADCAST   — announce completion to all pipeline agents

        ACP CONCEPT: Payload Completeness at the Terminal Stage
        ---------------------------------------------------------
        By the time the payload reaches the PublisherAgent, it has been
        enriched by EVERY preceding stage:

            ResearcherAgent → added: facts, key_people, statistics, seo_hints
            WriterAgent     → added: title, draft_content, word_count, style_used
            EditorAgent     → added: revised_content, edits_made, readability_score
            SEOAgent        → added: final_content, seo_metadata, keyword_analysis

        The PublisherAgent's payload is therefore the most information-rich
        message in the entire pipeline.  It contains a complete provenance
        record — from the first research fact to the last SEO optimisation —
        all carried forward through ACP message payloads.

        This design means the publication metadata file is self-contained:
        it documents exactly HOW the article was produced, with no need
        to consult the ACP message history to reconstruct the story.

        Parameters
        ----------
        message : ACPMessage
            The ACP message from SEOAgent containing the full article payload.
        """
        seo_payload: Dict[str, Any] = message.payload or {}

        # ---- Phase 1: VALIDATE — ensure we have what we need ---------
        title: str = seo_payload.get("title", "").strip()
        final_content: str = seo_payload.get("final_content", "").strip()

        if not title or not final_content:
            self._log(
                "ERROR: Payload is missing 'title' and/or 'final_content'. "
                "Cannot publish an empty article. This indicates a failure in "
                "the SEO stage or an upstream payload corruption."
            )
            self.send(
                receiver_id=message.sender_id,
                payload={
                    "error": (
                        "PublisherAgent cannot publish: missing 'title' "
                        "and/or 'final_content' in payload."
                    ),
                    "received_keys": list(seo_payload.keys()),
                    "pipeline_stage": 5,
                },
                msg_type=ACPMessageType.ERROR,
                correlation_id=message.message_id,
            )
            self.mark_error("Missing content — publication aborted.")
            return

        # ---- Extract all pipeline context from the payload -----------
        topic: str = seo_payload.get("topic", "unknown")
        word_count: int = seo_payload.get("word_count", len(final_content.split()))
        seo_metadata: Dict[str, Any] = seo_payload.get("seo_metadata", {})
        keyword_analysis: Dict[str, Any] = seo_payload.get("keyword_analysis", {})
        edits_made: List[Dict] = seo_payload.get("edits_made", [])
        readability_score: float = seo_payload.get("readability_score", 0.0)
        readability_level: str = seo_payload.get("readability_level", "Unknown")
        readability_full: Dict[str, Any] = seo_payload.get("readability_full", {})
        grammar_quality: str = seo_payload.get("grammar_quality", "Unknown")
        style_used: Dict[str, Any] = seo_payload.get("style_used", {})
        research_brief: Dict[str, Any] = seo_payload.get("research_brief", {})
        kw_optimisations: List[Dict] = seo_payload.get("kw_optimisations", [])

        self._log_section(f"PublisherAgent — Publishing: '{title}'")
        self._log(
            f"Received SEO-optimised content from '{message.sender_id}' "
            f"(corr:{message.correlation_id}). "
            f"Title: '{title}', {word_count} words, "
            f"SEO score: {seo_metadata.get('seo_score', '?')}/100, "
            f"readability: {readability_score}/100."
        )
        self._log(
            "ACP CONCEPT: This payload has traversed 4 pipeline stages. "
            "It contains research data, draft content, editorial history, "
            "and SEO metadata — all accumulated through ACP message payloads. "
            "PublisherAgent is the final consumer of this complete picture."
        )

        # ---- Phase 2: ASSEMBLE — prepare content and metadata --------
        self._log("Phase 1/5: ASSEMBLE — Preparing final content and metadata")

        # Clean the content of any editor/SEO annotations before publishing
        # (<!-- Editor note: ... --> and <!-- SEO: ... --> comments are internal
        # working notes, not intended for the published article)
        publishable_content = self._clean_content_for_publication(final_content)

        # Build the comprehensive metadata dict for the MCP publish_article tool.
        # This is everything that ends up in the _meta.json sidecar file.
        publication_metadata = self._assemble_publication_metadata(
            title=title,
            topic=topic,
            seo_metadata=seo_metadata,
            keyword_analysis=keyword_analysis,
            edits_made=edits_made,
            readability_score=readability_score,
            readability_level=readability_level,
            readability_full=readability_full,
            grammar_quality=grammar_quality,
            style_used=style_used,
            research_brief=research_brief,
            word_count=word_count,
            kw_optimisations=kw_optimisations,
            correlation_id=message.correlation_id or message.message_id,
        )

        self._log(
            f"Publication metadata assembled. "
            f"Meta title: '{seo_metadata.get('meta_title', title[:40])}', "
            f"Tags: {seo_metadata.get('tags', [])[:4]}, "
            f"Slug: '{seo_metadata.get('slug', '?')}'"
        )

        # ---- Phase 3: PUBLISH — call MCP publish_article tool --------
        self._log(
            "Phase 2/5: PUBLISH — Calling MCP 'publish_article' tool\n"
            "  MCP CONCEPT: The PublisherAgent never opens a file directly.\n"
            "  It delegates all file I/O to the MCP server via the 'publish_article'\n"
            "  tool.  The server handles path construction, YAML front matter,\n"
            "  JSON serialisation, and directory creation.\n"
            "  Swapping from 'write local files' to 'POST to a CMS API' requires\n"
            "  only updating the MCPServer's tool implementation — zero agent changes."
        )

        publish_response = self.mcp.call_tool(
            "publish_article",
            title=title,
            content=publishable_content,
            metadata=publication_metadata,
        )

        if not publish_response["success"] or not publish_response.get("result"):
            error_detail = publish_response.get("error", "Unknown MCP error")
            self._log(
                f"ERROR: MCP 'publish_article' tool failed: {error_detail}. "
                f"The article could not be saved to the file system. "
                f"Pipeline run will be marked as error."
            )
            # Still broadcast so other agents know what happened
            self.broadcast(
                payload={
                    "announcement": f"PUBLICATION FAILED: '{title}'",
                    "title": title,
                    "topic": topic,
                    "error": error_detail,
                    "pipeline_status": "error",
                },
                msg_type=ACPMessageType.BROADCAST,
            )
            self.mark_error(f"MCP publish_article failed: {error_detail}")
            return

        publish_result: Dict[str, Any] = publish_response["result"]
        md_filepath: str = publish_result.get("md_filepath", "")
        meta_filepath: str = publish_result.get("meta_filepath", "")
        slug: str = publish_result.get("slug", seo_metadata.get("slug", ""))
        published_at: str = publish_result.get(
            "published_at", datetime.now().isoformat()
        )
        canonical_url: str = publish_result.get("url_slug", f"/articles/{slug}")

        self._log(
            f"Article successfully published via MCP! "
            f"Files written:\n"
            f"    Article  : {os.path.basename(md_filepath)}\n"
            f"    Metadata : {os.path.basename(meta_filepath)}"
        )

        # ---- Phase 4: REPORT — print publication summary -------------
        self._log("Phase 3/5: REPORT — Printing publication summary")

        publication_record = {
            "title": title,
            "topic": topic,
            "slug": slug,
            "canonical_url": canonical_url,
            "published_at": published_at,
            "md_filepath": md_filepath,
            "meta_filepath": meta_filepath,
            "word_count": word_count,
            "reading_time_minutes": readability_full.get(
                "reading_time_minutes",
                round(word_count / 238, 1),
            ),
            "readability_score": readability_score,
            "readability_level": readability_level,
            "grammar_quality": grammar_quality,
            "seo_score": seo_metadata.get("seo_score", 0),
            "seo_grade": seo_metadata.get("seo_grade", "?"),
            "primary_keyword": seo_metadata.get("primary_keyword", ""),
            "tags": seo_metadata.get("tags", []),
            "edits_applied": len(
                [e for e in edits_made if e.get("category") != "annotation"]
            ),
            "kw_optimisations_applied": len(kw_optimisations),
            "pipeline_stages_completed": 5,
        }

        # Store for session summary
        self._publications.append(publication_record)

        self._print_publication_summary(publication_record, seo_metadata)

        # ---- Phase 5: BROADCAST — announce completion to all agents --
        self._log(
            "Phase 4/5: BROADCAST — Announcing publication to all pipeline agents\n"
            "  ACP CONCEPT: The PublisherAgent BROADCASTS (receiver_id = None),\n"
            "  not unicasts (receiver_id = specific agent).  This means EVERY\n"
            "  agent subscribed to the bus receives this announcement.\n"
            "  Future agents (social media poster, analytics tracker, email notifier)\n"
            "  can react to this broadcast WITHOUT the PublisherAgent knowing they exist.\n"
            "  This is ACP's event-driven extensibility model: publish events,\n"
            "  let interested parties subscribe."
        )

        broadcast_payload: Dict[str, Any] = {
            # ── Announcement (human-readable) ────────────────────────────
            "announcement": f"Article Published: '{title}'",
            # ── Publication details ──────────────────────────────────────
            "title": title,
            "topic": topic,
            "slug": slug,
            "url": canonical_url,
            "published_at": published_at,
            "md_file": os.path.basename(md_filepath),
            "meta_file": os.path.basename(meta_filepath),
            # ── Quality summary ──────────────────────────────────────────
            "word_count": word_count,
            "seo_score": seo_metadata.get("seo_score", 0),
            "seo_grade": seo_metadata.get("seo_grade", "?"),
            "readability_score": readability_score,
            "readability_level": readability_level,
            # ── Pipeline provenance ──────────────────────────────────────
            "pipeline_stages_completed": 5,
            "pipeline_agents": [
                "researcher",
                "writer",
                "editor",
                "seo",
                "publisher",
            ],
            "pipeline_status": "complete",
            # ── Correlation ID so anyone can trace back to origin ────────
            "origin_correlation_id": message.correlation_id,
        }

        broadcast_msg = self.broadcast(
            payload=broadcast_payload,
            msg_type=ACPMessageType.BROADCAST,
            content_type=ACPContentType.JSON,
            metadata={
                "pipeline_stage": 5,
                "event_type": "article_published",
                "topic": topic,
            },
        )

        self._log(
            f"Broadcast sent (msg id: {broadcast_msg.message_id}). "
            f"All {len(self.bus.list_subscribers())} pipeline agents have been notified "
            f"that the article is published."
        )

        self._log(
            "Phase 5/5: COMPLETE — ContentForge pipeline finished successfully!\n"
            f"  Article '{title}' is now published at {canonical_url}\n"
            f"  Total pipeline: Research → Write → Edit → SEO → Publish ✓"
        )

        self._log_section_end()
        self.mark_done()

    # ==========================================================================
    # CONTENT PREPARATION
    # ==========================================================================

    @staticmethod
    def _clean_content_for_publication(content: str) -> str:
        """
        Remove internal working annotations from the content before publication.

        During the pipeline, agents insert HTML comments as internal notes
        and annotations:
          - EditorAgent adds <!-- Editor note: ... --> comments
          - SEOAgent adds <!-- SEO: ... --> comments
          - WriterAgent adds <!-- ... --> draft markers

        These are useful for pipeline debugging and human editorial review,
        but they should NOT appear in the published article — they are
        invisible to browsers/CMS systems but they clutter the Markdown source
        and may be exposed by certain CMS renderers.

        This method removes all HTML comments and performs a final
        cleanup pass on whitespace.

        Parameters
        ----------
        content : str
            The raw final_content from the SEOAgent payload.

        Returns
        -------
        str
            Clean, publication-ready Markdown content.
        """
        # Remove all HTML comment blocks (<!-- ... -->)
        # The DOTALL flag allows matching comments that span multiple lines
        cleaned = re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL)

        # Remove the WriterAgent's draft footer line (starts with "---\n*Draft produced by")
        # The EditorAgent and SEOAgent have already added their own review markers
        # which are more current; the original draft marker is redundant
        cleaned = re.sub(
            r"\n\n---\n\*Draft produced by ContentForge WriterAgent[^\*]*\*",
            "",
            cleaned,
        )

        # Collapse runs of 3+ blank lines to exactly 2 blank lines
        # (which renders as a single paragraph break in Markdown)
        cleaned = re.sub(r"\n{4,}", "\n\n\n", cleaned)

        # Remove trailing whitespace from each line
        lines = [line.rstrip() for line in cleaned.split("\n")]
        cleaned = "\n".join(lines)

        # Ensure the article ends with exactly one newline
        cleaned = cleaned.strip() + "\n"

        return cleaned

    def _assemble_publication_metadata(
        self,
        title: str,
        topic: str,
        seo_metadata: Dict[str, Any],
        keyword_analysis: Dict[str, Any],
        edits_made: List[Dict],
        readability_score: float,
        readability_level: str,
        readability_full: Dict[str, Any],
        grammar_quality: str,
        style_used: Dict[str, Any],
        research_brief: Dict[str, Any],
        word_count: int,
        kw_optimisations: List[Dict],
        correlation_id: str,
    ) -> Dict[str, Any]:
        """
        Assemble the complete metadata dictionary for the MCP publish_article tool.

        This metadata is written to the JSON sidecar file (<slug>_meta.json)
        alongside the published article.  It is the permanent record of how
        this article was produced — a complete provenance document.

        The metadata covers:
          - SEO data    : keywords, meta tags, social sharing, schema
          - Quality     : readability, grammar, SEO score
          - Pipeline    : which agents ran, what each agent did
          - Editorial   : list of every edit applied with rationale
          - Research    : the original facts and statistics used
          - Timing      : when each stage completed (via correlation IDs)

        ACP CONCEPT: Message Payload as Provenance Record
        ---------------------------------------------------
        The fact that we can assemble such a rich provenance document here
        — knowing every fact researched, every edit made, every keyword
        inserted — is ONLY possible because each pipeline stage enriched
        the ACP message payload and passed it forward.

        If agents had communicated through shared state (a database, a global
        dict) rather than through ACP message payloads, we would need complex
        state management to reconstruct this provenance.  ACP's payload
        pass-through pattern makes it trivially available at the terminal stage.

        Parameters
        ----------
        (see individual parameter names — each corresponds to a field from
        the SEOAgent's output payload)

        Returns
        -------
        Dict[str, Any]
            Complete metadata dict ready for the MCP publish_article tool.
        """
        # ---- Summarise the editorial history -------------------------
        # Condense edits_made into a concise list of descriptions
        # (the full list can be very long)
        editorial_summary: List[str] = []
        substantive_edits = [
            e for e in edits_made if e.get("category") not in ("annotation",)
        ]
        for edit in substantive_edits[:10]:  # cap at 10 for readability
            category = edit.get("category", "unknown")
            action = edit.get("action", "unknown").replace("_", " ")
            impact = edit.get("impact", "") or ""
            if impact:
                first_line = impact.split("\n")[0][:80]
                editorial_summary.append(f"[{category}] {action}: {first_line}")
            else:
                editorial_summary.append(f"[{category}] {action}")

        # ---- Summarise keyword optimisations -------------------------
        kw_opt_summary: List[str] = []
        for opt in kw_optimisations:
            opt_type = opt.get("type", "unknown")
            kw = opt.get("keyword") or opt.get("phrase", "")
            action = opt.get("action", "")
            if kw:
                kw_opt_summary.append(f"[{opt_type}] '{kw}': {action[:60]}")

        # ---- Extract key research facts for the provenance record -----
        research_facts: List[str] = research_brief.get("facts", [])[:5]
        research_stats: Dict[str, str] = research_brief.get("statistics", {})
        research_people: List[str] = research_brief.get("key_people", [])[:5]

        # ---- Build the complete metadata record ----------------------
        return {
            # ── SEO metadata (from SEOAgent) ─────────────────────────────
            "meta_title": seo_metadata.get("meta_title", title),
            "meta_description": seo_metadata.get("meta_description", ""),
            "primary_keyword": seo_metadata.get("primary_keyword", ""),
            "keywords": seo_metadata.get("keywords", []),
            "tags": seo_metadata.get("tags", []),
            "slug": seo_metadata.get("slug", ""),
            "canonical_url": seo_metadata.get("canonical_url", ""),
            "open_graph": seo_metadata.get("open_graph", {}),
            "twitter_card": seo_metadata.get("twitter_card", {}),
            "schema_type": seo_metadata.get("schema_type", "BlogPosting"),
            "json_ld": seo_metadata.get("json_ld", {}),
            # ── Quality metrics ──────────────────────────────────────────
            "quality": {
                "seo_score": seo_metadata.get("seo_score", 0),
                "seo_grade": seo_metadata.get("seo_grade", "?"),
                "readability_score": readability_score,
                "readability_level": readability_level,
                "avg_sentence_length": readability_full.get("avg_sentence_length", 0),
                "paragraph_count": readability_full.get("paragraph_count", 0),
                "sentence_count": readability_full.get("sentence_count", 0),
                "grammar_quality": grammar_quality,
                "word_count": word_count,
                "reading_time_minutes": readability_full.get(
                    "reading_time_minutes", round(word_count / 238, 1)
                ),
            },
            # ── Pipeline provenance ──────────────────────────────────────
            "pipeline": {
                "system": "ContentForge",
                "version": "1.0.0",
                "protocols": [
                    "ACP (Agent Communication Protocol)",
                    "MCP (Model Context Protocol)",
                ],
                "stages": [
                    {
                        "stage": 1,
                        "agent": "ResearcherAgent",
                        "role": "Research topic using MCP knowledge base + SEO tools",
                        "mcp_tools": ["search_topic", "get_seo_keywords"],
                    },
                    {
                        "stage": 2,
                        "agent": "WriterAgent",
                        "role": "Draft full article using MCP style guide",
                        "mcp_tools": ["get_style_guide", "save_draft"],
                        "style": style_used.get("tone", ""),
                        "target_word_count": style_used.get("target_word_count", 1200),
                    },
                    {
                        "stage": 3,
                        "agent": "EditorAgent",
                        "role": "Review and improve draft using MCP analysis tools",
                        "mcp_tools": ["check_readability", "grammar_check"],
                        "edits_applied": len(substantive_edits),
                    },
                    {
                        "stage": 4,
                        "agent": "SEOAgent",
                        "role": "Optimise keywords and generate SEO metadata",
                        "mcp_tools": ["get_seo_keywords"],
                        "kw_optimisations": len(kw_optimisations),
                    },
                    {
                        "stage": 5,
                        "agent": "PublisherAgent",
                        "role": "Publish final article and broadcast completion",
                        "mcp_tools": ["publish_article"],
                    },
                ],
                "acp_message_chain": f"corr_id:{correlation_id}",
                "run_date": datetime.now().strftime("%Y-%m-%d"),
                "run_time": datetime.now().isoformat(),
            },
            # ── Editorial history ────────────────────────────────────────
            "editorial": {
                "total_edits": len(substantive_edits),
                "edit_categories": list(
                    {e.get("category", "") for e in substantive_edits}
                ),
                "summary": editorial_summary,
                "full_edits": edits_made,  # complete record for detailed audit
            },
            # ── Keyword optimisation history ─────────────────────────────
            "seo_optimisations": {
                "total": len(kw_optimisations),
                "summary": kw_opt_summary,
                "keyword_density_report": {
                    "focus_keyword": keyword_analysis.get("focus_keyword", ""),
                    "primary_density": keyword_analysis.get("primary_density", 0.0),
                    "well_optimised": keyword_analysis.get("well_optimised", []),
                    "missing_at_publish": keyword_analysis.get("missing_keywords", []),
                    "seo_score": keyword_analysis.get("seo_score", 0),
                },
            },
            # ── Research provenance ──────────────────────────────────────
            "research": {
                "topic": topic,
                "key_facts_used": research_facts,
                "statistics": research_stats,
                "key_people_referenced": research_people,
                "subtopics": research_brief.get("subtopics", []),
            },
            # ── Readability details ──────────────────────────────────────
            "readability": readability_full,
        }

    # ==========================================================================
    # OUTPUT / REPORTING
    # ==========================================================================

    def _print_publication_summary(
        self,
        record: Dict[str, Any],
        seo_metadata: Dict[str, Any],
    ) -> None:
        """
        Print a comprehensive, formatted publication summary to the console.

        This is the "money shot" of the ContentForge pipeline — the moment
        where all five stages' work is presented as a completed, published
        article with full quality metrics.

        The summary is designed to be immediately useful to a content
        manager or developer running the pipeline:
          - Shows exactly WHERE the article was saved
          - Shows quality metrics at a glance (readability, SEO score)
          - Shows the SEO metadata that will be used in search results
          - Shows how the pipeline performed (word count, edits, optimisations)

        Parameters
        ----------
        record : dict
            The publication record assembled during the publish phase.
        seo_metadata : dict
            The full SEO metadata package from SEOAgent.
        """
        separator = "═" * 66

        print(f"\n\n  ╔{separator}╗")
        print(f"  ║{'CONTENTFORGE — PUBLICATION COMPLETE':^66}║")
        print(f"  ╠{separator}╣")

        # ---- Article identity ----------------------------------------
        title_display = record["title"]
        if len(title_display) > 62:
            title_display = title_display[:59] + "..."
        print(f"  ║  {'Title':<18}: {title_display:<46}║")
        print(f"  ║  {'Topic':<18}: {record['topic']:<46}║")
        print(f"  ║  {'Published At':<18}: {record['published_at']:<46}║")

        print(f"  ╠{separator}╣")
        print(f"  ║  {'FILES WRITTEN':<64}  ║")
        print(f"  ╠{separator}╣")

        # ---- File locations ------------------------------------------
        md_file = os.path.basename(record.get("md_filepath", ""))
        meta_file = os.path.basename(record.get("meta_filepath", ""))
        print(f"  ║  {'Article (Markdown)':<18}: {md_file:<46}║")
        print(f"  ║  {'Metadata (JSON)':<18}: {meta_file:<46}║")
        print(f"  ║  {'Canonical URL':<18}: {record.get('canonical_url', '?'):<46}║")

        print(f"  ╠{separator}╣")
        print(f"  ║  {'CONTENT METRICS':<64}  ║")
        print(f"  ╠{separator}╣")

        # ---- Content quality metrics ---------------------------------
        print(f"  ║  {'Word Count':<18}: {record['word_count']:<46}║")
        reading_time = record.get("reading_time_minutes", 0)
        print(f"  ║  {'Reading Time':<18}: {f'{reading_time} min':<46}║")
        print(
            f"  ║  {'Readability':<18}: "
            f"{str(record['readability_score']) + '/100 (' + str(record['readability_level']) + ')':<46}║"
        )
        print(
            f"  ║  {'Grammar Quality':<18}: {record.get('grammar_quality', '?'):<46}║"
        )

        print(f"  ╠{separator}╣")
        print(f"  ║  {'SEO METADATA':<64}  ║")
        print(f"  ╠{separator}╣")

        # ---- SEO metadata summary ------------------------------------
        seo_score_str = (
            f"{record.get('seo_score', 0)}/100 — {seo_metadata.get('seo_grade', '?')}"
        )
        print(f"  ║  {'SEO Score':<18}: {seo_score_str:<46}║")
        print(
            f"  ║  {'Primary Keyword':<18}: {record.get('primary_keyword', '?'):<46}║"
        )

        meta_title = seo_metadata.get("meta_title", "")
        meta_title_display = (
            f"{meta_title[:40]}... ({len(meta_title)} chars)"
            if len(meta_title) > 40
            else f"{meta_title} ({len(meta_title)} chars)"
        )
        print(f"  ║  {'Meta Title':<18}: {meta_title_display:<46}║")

        meta_desc = seo_metadata.get("meta_description", "")
        meta_desc_display = (
            f"{meta_desc[:38]}... ({len(meta_desc)}c)"
            if len(meta_desc) > 38
            else f"{meta_desc} ({len(meta_desc)}c)"
        )
        print(f"  ║  {'Meta Description':<18}: {meta_desc_display:<46}║")

        tags_str = ", ".join(record.get("tags", [])[:5])
        if len(tags_str) > 46:
            tags_str = tags_str[:43] + "..."
        print(f"  ║  {'Tags':<18}: {tags_str:<46}║")

        print(f"  ╠{separator}╣")
        print(f"  ║  {'PIPELINE PERFORMANCE':<64}  ║")
        print(f"  ╠{separator}╣")

        # ---- Pipeline performance summary ----------------------------
        print(
            f"  ║  {'Stages Completed':<18}: "
            f"{'5 / 5  (Research→Write→Edit→SEO→Publish)':<46}║"
        )
        print(
            f"  ║  {'Edits Applied':<18}: "
            f"{str(record.get('edits_applied', 0)) + ' editorial improvements':<46}║"
        )
        print(
            f"  ║  {'KW Optimisations':<18}: "
            f"{str(record.get('kw_optimisations_applied', 0)) + ' keyword insertions/annotations':<46}║"
        )
        schema = seo_metadata.get("schema_type", "BlogPosting")
        print(f"  ║  {'Schema Type':<18}: {schema:<46}║")

        print(f"  ╠{separator}╣")
        print(f"  ║  {'STATUS':<64}  ║")
        print(f"  ╠{separator}╣")
        print(f"  ║  {'✓ PUBLISHED':<64}  ║")
        print(f"  ║  {'ACP pipeline complete. Article is live.':<64}  ║")
        print(f"  ╚{separator}╝")

        # ---- Print the article preview (first 400 chars) -------------
        print()
        print("  ── ARTICLE PREVIEW (first 400 characters) ─────────────────────────")
        # We don't have publishable_content here, but we can use the title
        # to hint at what's in the file
        print(
            f"  See data/published/{os.path.basename(record.get('md_filepath', ''))}"
            f" for the full article."
        )
        print(
            "  ──────────────────────────────────────────────────────────────────────"
        )

    def print_session_summary(self) -> None:
        """
        Print a summary of all articles published in this session.

        Called after running multiple pipeline topics to give an
        aggregate view of what ContentForge produced in the session.

        ACP CONCEPT: Session-Level Observability
        -----------------------------------------
        The PublisherAgent accumulates a publication record for every
        article it publishes in a session.  This is similar to how a
        message bus accumulates a message history.  Together, these two
        records give complete observability at both the message level
        (what did agents say to each other?) and the output level
        (what did the pipeline actually produce?).
        """
        if not self._publications:
            print("\n  [PublisherAgent] No articles published in this session.")
            return

        print("\n")
        print("=" * 70)
        print("  CONTENTFORGE SESSION SUMMARY")
        print("=" * 70)
        print(f"  Articles published this session: {len(self._publications)}\n")

        for i, pub in enumerate(self._publications, 1):
            print(f"  [{i}] {pub['title']}")
            print(f"      Topic   : {pub['topic']}")
            print(f"      URL     : {pub.get('canonical_url', '?')}")
            print(
                f"      Quality : "
                f"SEO {pub.get('seo_score', '?')}/100, "
                f"Readability {pub.get('readability_score', '?')}/100, "
                f"Grammar: {pub.get('grammar_quality', '?')}"
            )
            print(f"      Words   : {pub.get('word_count', '?')}")
            print(f"      File    : {os.path.basename(pub.get('md_filepath', '?'))}")
            print()

        print(
            f"  Total words published: "
            f"{sum(p.get('word_count', 0) for p in self._publications):,}"
        )
        print(
            f"  Average SEO score   : "
            f"{round(sum(p.get('seo_score', 0) for p in self._publications) / len(self._publications), 1)}"
        )
        print(
            f"  Average readability : "
            f"{round(sum(p.get('readability_score', 0) for p in self._publications) / len(self._publications), 1)}/100"
        )
        print("=" * 70)

    # ==========================================================================
    # REGISTRY METADATA
    # ==========================================================================

    def get_agent_info(self) -> ACPAgentInfo:
        """
        Return ACPAgentInfo describing this agent for the ACP Agent Registry.

        The PublisherAgent's output_schema is special: instead of describing
        an ACP message payload (there is no downstream agent), it describes
        the FILES written to the file system.

        Returns
        -------
        ACPAgentInfo
        """
        return ACPAgentInfo(
            agent_id=self.AGENT_ID,
            name="PublisherAgent",
            description=(
                "Stage 5 (Final): Receives the SEO-optimised article from SEOAgent, "
                "cleans internal annotations from the content, assembles a complete "
                "publication metadata record, and calls the MCP 'publish_article' tool "
                "to write the article to the published/ directory as both a Markdown "
                "file (with YAML front matter) and a JSON metadata sidecar. "
                "Finally, broadcasts an 'Article Published' event to ALL pipeline "
                "agents via the ACP bus, signalling pipeline completion and enabling "
                "future downstream consumers (social media, analytics, email) to react."
            ),
            input_schema={
                "title": "str — final article title",
                "final_content": "str — SEO-optimised Markdown content from SEOAgent",
                "seo_metadata": "dict — complete SEO package (meta_title, meta_description, tags, slug, og, twitter, json_ld)",
                "keyword_analysis": "dict — keyword density report (pass-through)",
                "kw_optimisations": "list[dict] — keyword insertions applied (pass-through)",
                "word_count": "int — final word count",
                "topic": "str — article topic",
                "edits_made": "list[dict] — full editorial history from EditorAgent (pass-through)",
                "readability_score": "float — Flesch score (pass-through)",
                "readability_full": "dict — full readability metrics (pass-through)",
                "grammar_quality": "str — grammar quality (pass-through)",
                "style_used": "dict — style guide metadata (pass-through)",
                "research_brief": "dict — original research data (pass-through)",
            },
            output_schema={
                "files_written": (
                    "Two files in data/published/: "
                    "<slug>.md (Markdown with YAML front matter) "
                    "and <slug>_meta.json (complete provenance record)"
                ),
                "broadcast_payload": (
                    "dict — sent to ALL agents: "
                    "{announcement, title, topic, slug, url, published_at, "
                    "word_count, seo_score, seo_grade, readability_score, "
                    "pipeline_stages_completed, pipeline_agents, pipeline_status, "
                    "origin_correlation_id}"
                ),
            },
            status=self._status,
            pipeline_stage=5,
            tags=["publishing", "output", "broadcast", "final-stage", "stage-5"],
            metadata={
                "mcp_tools_used": ["publish_article"],
                "sends_to": ["ALL (broadcast)"],
                "receives_from": ["seo"],
                "output_location": "data/published/",
                "output_formats": [
                    ".md (YAML front matter)",
                    ".json (metadata sidecar)",
                ],
                "publications_this_session": len(self._publications),
            },
        )
