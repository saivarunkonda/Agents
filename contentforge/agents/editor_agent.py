# =============================================================================
# agents/editor_agent.py  —  EditorAgent
# =============================================================================
#
# ROLE IN THE PIPELINE
# --------------------
# The EditorAgent is Stage 3 of the ContentForge pipeline.  It receives the
# raw article draft produced by the WriterAgent and applies a professional
# editorial pass: checking readability, identifying grammar issues, and making
# targeted improvements to the prose quality, clarity, and structure.
#
# A good editor doesn't rewrite the article — they elevate it.  The EditorAgent
# preserves the WriterAgent's voice and intent while correcting weaknesses,
# tightening sentences, and ensuring the draft meets the style guide's standards.
#
# WHAT IT DOES
# ------------
#   1. Receives a RESPONSE message from WriterAgent with payload:
#      { title, draft_content, word_count, style_used, research_brief, ... }
#
#   2. Uses MCP tool "check_readability" to analyse the draft:
#        - Word count, sentence count, paragraph count
#        - Average sentence length (flag if > 25 words)
#        - Flesch-Kincaid readability score
#        - Reading time estimate
#
#   3. Uses MCP tool "grammar_check" to scan for issues:
#        - Passive voice overuse
#        - Wordy / weak phrases
#        - Spelling and grammar errors
#        - Structural problems (overly long paragraphs)
#
#   4. Makes targeted editorial improvements:
#        - Tightens wordy phrases
#        - Improves section transitions
#        - Strengthens the introduction hook
#        - Adds or refines the call-to-action
#        - Inserts editorial annotations showing WHAT changed and WHY
#
#   5. Sends the revised content to SEOAgent via the ACP bus
#
# ACP FLOW
# --------
#   [WriterAgent]
#         │  RESPONSE  { title, draft_content, word_count, style_used, ... }
#         ▼
#   [EditorAgent]  ──MCP──▶  check_readability(draft_content)
#                  ──MCP──▶  grammar_check(draft_content)
#         │  RESPONSE  { title, revised_content, edits_made,
#         │              readability_score, word_count, research_brief }
#         ▼
#   [SEOAgent]
#
# MCP TOOLS USED
# --------------
#   check_readability(text)   → readability metrics (Flesch score, sentence length, etc.)
#   grammar_check(text)       → list of grammar/style issues with suggestions
#
# ACP MESSAGE TYPES
# -----------------
#   Receives : ACPMessageType.RESPONSE  (from "writer")
#              ACPMessageType.BROADCAST (ignored gracefully)
#   Sends    : ACPMessageType.RESPONSE  (to "seo")
#              ACPMessageType.ERROR     (to sender, on failure)
# =============================================================================

import re
import textwrap
from typing import Any, Dict, List, Optional, Tuple

from acp.agent_registry import ACPAgentInfo, ACPAgentRegistry
from acp.message import ACPContentType, ACPMessage, ACPMessageType
from acp.message_bus import ACPMessageBus
from mcp.mcp_client import MCPClient

from agents.base_agent import BaseACPAgent


class EditorAgent(BaseACPAgent):
    """
    EditorAgent — Stage 3 of the ContentForge pipeline.

    Responsible for reviewing the article draft produced by the WriterAgent,
    identifying readability and grammar issues via MCP tools, and applying
    targeted editorial improvements before passing the content to the SEOAgent.

    ACP CONCEPT: Quality Gate Agents
    ----------------------------------
    The EditorAgent is a "quality gate" — a pipeline stage whose sole purpose
    is to verify and improve the output of the previous stage.  Quality gate
    agents are a fundamental pattern in multi-agent pipelines because:

      - They decouple quality assurance from content production
      - They can be upgraded independently (better grammar model → swap EditorAgent)
      - They create a natural checkpoint where the pipeline can halt on quality failures
      - Their edits are logged as ACP messages, creating a full audit trail of
        WHAT was changed and WHY in every article

    In a production system, the EditorAgent might use a fine-tuned LLM to make
    higher-quality improvements, but the ACP/MCP contract stays identical.

    MCP CONCEPT: Analysis Before Action
    -------------------------------------
    The EditorAgent demonstrates a key MCP pattern: call analytical tools FIRST
    to gather objective data about the content, then use that data to guide
    targeted improvements.  This is more principled than editing blindly:

      check_readability → "Sentence length averages 28 words — must shorten"
      grammar_check     → "Found 4 passive voice instances — convert to active"

    The MCP tools provide the EVIDENCE; the agent provides the JUDGEMENT about
    what to do with it.  This separation keeps the tool implementations reusable
    across different contexts.

    Parameters
    ----------
    bus : ACPMessageBus
        The shared ACP message bus for this pipeline run.
    mcp_client : MCPClient
        Pre-configured MCP client for tool invocations.
    registry : ACPAgentRegistry, optional
        Shared agent registry for status updates.
    """

    AGENT_ID: str = "editor"

    def __init__(
        self,
        bus: ACPMessageBus,
        mcp_client: MCPClient,
        registry: Optional[ACPAgentRegistry] = None,
    ) -> None:
        super().__init__(
            agent_id=self.AGENT_ID,
            name="EditorAgent",
            bus=bus,
            mcp_client=mcp_client,
            registry=registry,
        )
        self._log(
            "EditorAgent ready.  Accepts RESPONSE messages from WriterAgent. "
            "Will review, improve, and forward revised content to SEOAgent."
        )

    # ==========================================================================
    # ACP MESSAGE DISPATCH
    # ==========================================================================

    def _dispatch(self, message: ACPMessage) -> None:
        """
        Route an incoming ACP message to the appropriate handler.

        ACP CONCEPT: Broadcast Awareness
        ----------------------------------
        Every agent that subscribes to the ACP Message Bus will receive
        BROADCAST messages — including the PublisherAgent's final announcement
        that the article has been published.  Well-behaved agents acknowledge
        broadcasts they receive but have no action for, rather than silently
        ignoring them.  This makes the agent's behaviour predictable and
        easier to debug (you can see in the logs that it received the broadcast).

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
                f"'{announcement}' — EditorAgent has no action. Ignoring."
            )
            return

        if message.message_type == ACPMessageType.ACK:
            self._log(f"Received ACK from '{message.sender_id}' — acknowledged.")
            return

        if message.message_type == ACPMessageType.ERROR:
            self._log(
                f"Received ERROR from '{message.sender_id}': {message.payload}. "
                f"EditorAgent cannot edit without a valid draft."
            )
            self.mark_error(f"Upstream error from {message.sender_id}")
            return

        if message.message_type != ACPMessageType.RESPONSE:
            self._log(
                f"Unexpected message type '{message.message_type.value}' from "
                f"'{message.sender_id}'. EditorAgent only handles RESPONSE messages."
            )
            return

        # Process the draft from WriterAgent
        self._handle_edit_request(message)

    # ==========================================================================
    # EDIT REQUEST HANDLER
    # ==========================================================================

    def _handle_edit_request(self, message: ACPMessage) -> None:
        """
        Handle the draft from WriterAgent and produce an improved revision.

        The editorial process has four clear phases:
          1. ASSESS    — run MCP analytical tools on the draft
          2. PLAN      — decide what edits to make based on the tool results
          3. EXECUTE   — apply the edits and record what changed
          4. FORWARD   — send the revised content to SEOAgent via ACP bus

        ACP CONCEPT: Enriching the Payload
        ------------------------------------
        Notice that the output payload of the EditorAgent includes not just
        the revised content, but also a structured list of edits_made.  This
        is an important ACP pattern: agents should annotate their changes
        so downstream agents (and human operators reviewing the message history)
        can understand exactly what happened at each pipeline stage.

        The edits_made list becomes part of the article's provenance record —
        eventually stored in the JSON metadata file by the PublisherAgent.

        Parameters
        ----------
        message : ACPMessage
            The ACP message from WriterAgent containing the draft payload.
        """
        writer_payload: Dict[str, Any] = message.payload or {}

        # ---- Validate incoming draft --------------------------------
        title: str = writer_payload.get("title", "").strip()
        draft_content: str = writer_payload.get("draft_content", "").strip()

        if not title or not draft_content:
            self._log(
                "ERROR: Draft is missing 'title' or 'draft_content'. "
                "Cannot perform editorial review on an empty draft."
            )
            self.send(
                receiver_id=message.sender_id,
                payload={
                    "error": "Draft missing required fields: 'title' and/or 'draft_content'.",
                    "received_keys": list(writer_payload.keys()),
                },
                msg_type=ACPMessageType.ERROR,
                correlation_id=message.message_id,
            )
            self.mark_error("Missing draft content")
            return

        topic: str = writer_payload.get("topic", "unknown topic")
        original_word_count: int = writer_payload.get(
            "word_count", len(draft_content.split())
        )
        style_used: Dict[str, Any] = writer_payload.get("style_used", {})
        research_brief: Dict[str, Any] = writer_payload.get("research_brief", {})

        self._log_section(f"EditorAgent — Editing: '{title}'")
        self._log(
            f"Received draft from '{message.sender_id}' "
            f"(corr:{message.correlation_id}). "
            f"Title: '{title}', {original_word_count} words, "
            f"style: {style_used.get('tone', 'unknown')}."
        )
        self._log(
            "ACP CONCEPT: EditorAgent received the draft through the ACP bus. "
            "It now uses MCP tools to objectively measure content quality "
            "before making any editorial decisions."
        )

        # ---- Phase 1: ASSESS — Run MCP analytical tools -------------
        self._log("Phase 1/4: ASSESS — Running MCP analytical tools on the draft")

        readability_data, grammar_data = self._run_assessment(draft_content)

        # ---- Phase 2: PLAN — Decide what needs editing ---------------
        self._log("Phase 2/4: PLAN — Analysing assessment results and planning edits")

        edit_plan = self._plan_edits(readability_data, grammar_data, style_used)

        self._log(
            f"Edit plan: {len(edit_plan)} categories of changes identified. "
            f"Readability score: {readability_data.get('flesch_score', 'N/A')}, "
            f"Grammar issues: {grammar_data.get('issues_found', 0)}, "
            f"Overall quality: {grammar_data.get('overall_quality', 'N/A')}."
        )

        # ---- Phase 3: EXECUTE — Apply edits -------------------------
        self._log("Phase 3/4: EXECUTE — Applying editorial improvements to the draft")
        self._log(
            "MCP CONCEPT: The edit decisions are made by the agent (the 'brain'), "
            "but the data driving those decisions came entirely from MCP tools "
            "(check_readability + grammar_check). This separates intelligence "
            "from resource access — a core MCP design principle."
        )

        revised_content, edits_made = self._apply_edits(
            content=draft_content,
            title=title,
            topic=topic,
            edit_plan=edit_plan,
            readability_data=readability_data,
            grammar_data=grammar_data,
            style_used=style_used,
        )

        revised_word_count = len(revised_content.split())
        word_count_delta = revised_word_count - original_word_count

        self._log(
            f"Editorial pass complete. "
            f"{len(edits_made)} edit(s) applied. "
            f"Word count: {original_word_count} → {revised_word_count} "
            f"({'+' if word_count_delta >= 0 else ''}{word_count_delta} words)."
        )

        # ---- Print a readable diff summary --------------------------
        self._print_edit_summary(edits_made, readability_data, grammar_data)

        # ---- Phase 4: FORWARD — Send to SEOAgent via ACP bus ---------
        self._log(
            "Phase 4/4: FORWARD — Sending revised content to SEOAgent via ACP bus.\n"
            "  ACP CONCEPT: The revised_content travels forward in the pipeline.\n"
            "  The edits_made list is included so the PublisherAgent can include\n"
            "  a full editorial history in the article's metadata.\n"
            "  The correlation_id chain is preserved: every stage can be traced\n"
            "  back to the original research REQUEST that started the pipeline."
        )

        output_payload: Dict[str, Any] = {
            # ── New data produced by EditorAgent ──────────────────────────
            "title": title,
            "revised_content": revised_content,
            "edits_made": edits_made,
            "readability_score": readability_data.get("flesch_score", 0.0),
            "readability_level": readability_data.get("reading_level", "Unknown"),
            "grammar_quality": grammar_data.get("overall_quality", "Unknown"),
            "word_count": revised_word_count,
            "word_count_delta": word_count_delta,
            "reading_time_minutes": readability_data.get("reading_time_minutes", 0),
            "readability_full": readability_data,  # full metrics for metadata
            # ── Pass-through data from upstream agents ─────────────────────
            "topic": topic,
            "style_used": style_used,
            "research_brief": research_brief,
            # ── Protocol metadata ─────────────────────────────────────────
            "edited_by": self.agent_id,
            "source_message_id": message.message_id,
        }

        sent_msg = self.send(
            receiver_id="seo",
            payload=output_payload,
            msg_type=ACPMessageType.RESPONSE,
            content_type=ACPContentType.JSON,
            correlation_id=message.correlation_id,  # preserve chain from origin
            metadata={
                "pipeline_stage": 3,
                "topic": topic,
                "edits_count": len(edits_made),
                "readability_score": readability_data.get("flesch_score", 0),
            },
        )

        self._log(
            f"Revised content sent to SEOAgent "
            f"(ACP msg id: {sent_msg.message_id}, "
            f"corr: {sent_msg.correlation_id}). "
            f"EditorAgent's work is done — {len(edits_made)} edit(s) made."
        )

        self._log_section_end()
        self.mark_done()

    # ==========================================================================
    # ASSESSMENT PHASE
    # ==========================================================================

    def _run_assessment(
        self, draft_content: str
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        Run both MCP assessment tools on the draft and return their results.

        MCP CONCEPT: Parallel Assessment Tools
        ----------------------------------------
        The EditorAgent calls two different MCP tools for different
        dimensions of quality:

          check_readability → QUANTITATIVE analysis
            (How long are sentences?  What is the Flesch score?  How many words?)

          grammar_check     → QUALITATIVE analysis
            (What specific writing issues exist?  What should be fixed?)

        Both tools return structured data that the EditorAgent uses in its
        edit planning phase.  The agent never needs to know HOW these tools
        work internally — just what they return.  This is MCP's tool
        abstraction in action.

        In production, you might call these tools in parallel (asyncio.gather)
        since they are independent analyses of the same text.

        Parameters
        ----------
        draft_content : str
            The full article draft text to assess.

        Returns
        -------
        Tuple[Dict, Dict]
            (readability_data, grammar_data) — the raw MCP tool results.
        """
        # ---- Readability check ------------------------------------------
        self._log(
            "  [Assessment 1/2] Calling MCP 'check_readability' — "
            "measures Flesch score, sentence length, reading time, etc."
        )
        readability_response = self.mcp.call_tool(
            "check_readability", text=draft_content
        )

        if readability_response["success"] and readability_response.get("result"):
            readability_data = readability_response["result"]
            self._log(
                f"  Readability results: "
                f"words={readability_data.get('word_count', '?')}, "
                f"sentences={readability_data.get('sentence_count', '?')}, "
                f"avg_sentence_len={readability_data.get('avg_sentence_length', '?')} words, "
                f"flesch={readability_data.get('flesch_score', '?')}, "
                f"level='{readability_data.get('reading_level', '?')}', "
                f"read_time={readability_data.get('reading_time_minutes', '?')}min"
            )

            # Surface any readability issues flagged by the tool
            issues = readability_data.get("issues", [])
            if issues and issues != ["No major readability issues detected."]:
                self._log(f"  Readability issues flagged ({len(issues)}):")
                for issue in issues:
                    self._log(f"    ⚠  {issue}")
        else:
            self._log(
                f"  ⚠  MCP 'check_readability' failed: "
                f"{readability_response.get('error')}. Using fallback metrics."
            )
            # Compute minimal metrics locally as fallback
            words = draft_content.split()
            readability_data = {
                "word_count": len(words),
                "sentence_count": max(draft_content.count("."), 1),
                "paragraph_count": len(
                    [p for p in draft_content.split("\n\n") if p.strip()]
                ),
                "avg_sentence_length": round(
                    len(words) / max(draft_content.count("."), 1), 1
                ),
                "flesch_score": 60.0,  # assume "Standard" as fallback
                "reading_level": "Standard (estimated)",
                "reading_time_minutes": round(len(words) / 238, 1),
                "issues": ["Readability tool unavailable — using estimates."],
            }

        # ---- Grammar check -----------------------------------------------
        self._log(
            "  [Assessment 2/2] Calling MCP 'grammar_check' — "
            "scans for passive voice, wordy phrases, spelling errors, etc."
        )
        grammar_response = self.mcp.call_tool("grammar_check", text=draft_content)

        if grammar_response["success"] and grammar_response.get("result"):
            grammar_data = grammar_response["result"]
            self._log(
                f"  Grammar results: "
                f"{grammar_data.get('issues_found', 0)} issue(s) found — "
                f"{grammar_data.get('error_count', 0)} error(s), "
                f"{grammar_data.get('warning_count', 0)} warning(s), "
                f"{grammar_data.get('suggestion_count', 0)} suggestion(s). "
                f"Overall: {grammar_data.get('overall_quality', '?')}"
            )

            # Surface the specific issues found
            issues = grammar_data.get("issues", [])
            if issues:
                self._log(f"  Grammar issues found ({len(issues)}):")
                for issue in issues[:5]:  # show first 5 to avoid excessive output
                    sev = issue.get("severity", "?")
                    text = issue.get("text", "?")
                    suggestion = issue.get("suggestion", "?")[:60]
                    self._log(f"    [{sev.upper()}] {text} → {suggestion}")
                if len(issues) > 5:
                    self._log(f"    ... and {len(issues) - 5} more issue(s)")
        else:
            self._log(
                f"  ⚠  MCP 'grammar_check' failed: "
                f"{grammar_response.get('error')}. Using empty grammar results."
            )
            grammar_data = {
                "issues_found": 0,
                "error_count": 0,
                "warning_count": 0,
                "suggestion_count": 0,
                "issues": [],
                "overall_quality": "Unknown (tool unavailable)",
                "summary": "Grammar check could not be performed.",
            }

        return readability_data, grammar_data

    # ==========================================================================
    # PLANNING PHASE
    # ==========================================================================

    def _plan_edits(
        self,
        readability_data: Dict[str, Any],
        grammar_data: Dict[str, Any],
        style_used: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """
        Analyse MCP assessment results and produce a prioritised edit plan.

        The edit plan is a list of dicts, each describing one category of
        improvement to make.  Each entry has:
          - category    : what type of edit (readability / grammar / structure / etc.)
          - priority    : "high", "medium", "low"
          - action      : what the editor will do
          - rationale   : WHY this change is being made (data-driven justification)
          - apply_fn    : string name of the method to call during EXECUTE phase

        ACP CONCEPT: Data-Driven Decisions
        ------------------------------------
        Every edit in the plan is justified by objective data from the MCP tools.
        This makes the EditorAgent's behaviour transparent and auditable:
        "The sentence length was 28 words on average (target: <20), therefore
        we shortened sentences."  This is much more valuable in the message
        history than "the editor made some changes."

        Parameters
        ----------
        readability_data : dict
            Output from check_readability MCP tool.
        grammar_data : dict
            Output from grammar_check MCP tool.
        style_used : dict
            Style guide metadata from WriterAgent (tone, word count target, etc.).

        Returns
        -------
        List[Dict[str, Any]]
            Prioritised list of edit categories to apply.
        """
        plan: List[Dict[str, Any]] = []

        flesch_score: float = readability_data.get("flesch_score", 65.0)
        avg_sentence_len: float = readability_data.get("avg_sentence_length", 18.0)
        word_count: int = readability_data.get("word_count", 0)
        grammar_quality: str = grammar_data.get("overall_quality", "Good")
        grammar_issues: List[Dict] = grammar_data.get("issues", [])
        target_word_count: int = style_used.get("target_word_count", 1200)
        content_type: str = style_used.get("content_type", "blog")

        # ---- Plan Item 1: Sentence length --------------------------------
        if avg_sentence_len > 22 and content_type == "blog":
            plan.append(
                {
                    "category": "readability",
                    "priority": "high",
                    "action": "break_long_sentences",
                    "rationale": (
                        f"Average sentence length is {avg_sentence_len} words "
                        f"(target: under 20 for blog content). Long sentences reduce "
                        f"readability and increase bounce rate on web content."
                    ),
                }
            )

        # ---- Plan Item 2: Readability score ------------------------------
        if flesch_score < 50:
            plan.append(
                {
                    "category": "readability",
                    "priority": "high",
                    "action": "simplify_vocabulary",
                    "rationale": (
                        f"Flesch Reading Ease score of {flesch_score} is 'Difficult' "
                        f"(target for blog: 60–70 / Standard). Simplifying vocabulary "
                        f"will make the article accessible to the intended audience."
                    ),
                }
            )

        # ---- Plan Item 3: Wordy phrases ----------------------------------
        wordy_issues = [i for i in grammar_issues if i.get("type") == "wordiness"]
        if wordy_issues:
            plan.append(
                {
                    "category": "concision",
                    "priority": "medium",
                    "action": "replace_wordy_phrases",
                    "rationale": (
                        f"Found {len(wordy_issues)} wordy phrase(s) that can be tightened. "
                        f"Concise writing is more engaging and easier to skim — especially "
                        f"important for web audiences who scan before reading."
                    ),
                    "issues": wordy_issues,
                }
            )

        # ---- Plan Item 4: Weak intensifiers ------------------------------
        style_issues = [
            i
            for i in grammar_issues
            if i.get("type") == "style"
            and "intensifier" not in i.get("text", "").lower()
            and any(
                w in i.get("text", "").lower()
                for w in ["very", "really", "quite", "rather", "fairly"]
            )
        ]
        if style_issues:
            plan.append(
                {
                    "category": "style",
                    "priority": "low",
                    "action": "strengthen_vocabulary",
                    "rationale": (
                        f"Found {len(style_issues)} weak intensifier(s) (very, really, quite). "
                        f"Replacing them with more precise vocabulary strengthens the "
                        f"article's authoritative tone."
                    ),
                    "issues": style_issues,
                }
            )

        # ---- Plan Item 5: Passive voice ----------------------------------
        passive_issues = [
            i
            for i in grammar_issues
            if i.get("type") == "style" and "passive" in i.get("suggestion", "").lower()
        ]
        if passive_issues:
            plan.append(
                {
                    "category": "voice",
                    "priority": "medium",
                    "action": "convert_passive_to_active",
                    "rationale": (
                        f"Excessive passive voice detected. "
                        f"Active voice is more direct, engaging, and authoritative — "
                        f"key qualities for '{style_used.get('tone', 'blog')}' content."
                    ),
                    "issues": passive_issues,
                }
            )

        # ---- Plan Item 6: Structural improvements ------------------------
        structural_issues = [i for i in grammar_issues if i.get("type") == "structure"]
        if structural_issues:
            plan.append(
                {
                    "category": "structure",
                    "priority": "medium",
                    "action": "improve_structure",
                    "rationale": (
                        f"Found {len(structural_issues)} structural issue(s): "
                        f"{structural_issues[0].get('text', '')}. "
                        f"Better structure improves scanability and user engagement."
                    ),
                    "issues": structural_issues,
                }
            )

        # ---- Plan Item 7: Transition sentences ---------------------------
        # Always add transition improvements for blog content — this is
        # a standard editorial enhancement regardless of tool output
        if content_type == "blog":
            plan.append(
                {
                    "category": "flow",
                    "priority": "low",
                    "action": "improve_transitions",
                    "rationale": (
                        "Adding/strengthening transition sentences between sections "
                        "improves article flow and keeps readers engaged. "
                        "This is a standard blog editorial improvement."
                    ),
                }
            )

        # ---- Plan Item 8: Introduction hook strengthening ---------------
        plan.append(
            {
                "category": "hook",
                "priority": "high",
                "action": "strengthen_opening",
                "rationale": (
                    "The opening paragraph is the most important part of any article — "
                    "it determines whether readers continue. Ensuring the hook is "
                    "compelling and the value proposition is clear in the first 50 words."
                ),
            }
        )

        # ---- Plan Item 9: Conclusion / CTA enhancement ------------------
        if content_type == "blog":
            plan.append(
                {
                    "category": "cta",
                    "priority": "medium",
                    "action": "strengthen_cta",
                    "rationale": (
                        "A clear, actionable conclusion drives reader engagement and "
                        "achieves the content's business objective. Ensuring the "
                        "call-to-action is specific, motivating, and easy to act on."
                    ),
                }
            )

        # ---- Plan Item 10: Word count padding ----------------------------
        if word_count < target_word_count * 0.8:  # more than 20% below target
            shortfall = target_word_count - word_count
            plan.append(
                {
                    "category": "length",
                    "priority": "medium",
                    "action": "expand_thin_sections",
                    "rationale": (
                        f"Article is {word_count} words — "
                        f"{shortfall} words below the {target_word_count}-word target. "
                        f"Thin content ranks poorly in search engines and may fail to "
                        f"fully satisfy reader intent. Adding depth to underdeveloped sections."
                    ),
                }
            )

        # Sort plan by priority: high first, then medium, then low
        priority_order = {"high": 0, "medium": 1, "low": 2}
        plan.sort(key=lambda x: priority_order.get(x.get("priority", "low"), 2))

        # Log the plan
        for i, item in enumerate(plan, 1):
            self._log(
                f"  Edit [{i}] [{item['priority'].upper()}] "
                f"{item['category']} / {item['action']}: "
                f"{item['rationale'][:80]}..."
            )

        return plan

    # ==========================================================================
    # EXECUTION PHASE
    # ==========================================================================

    def _apply_edits(
        self,
        content: str,
        title: str,
        topic: str,
        edit_plan: List[Dict[str, Any]],
        readability_data: Dict[str, Any],
        grammar_data: Dict[str, Any],
        style_used: Dict[str, Any],
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """
        Apply all planned editorial edits to the draft and record what changed.

        Each edit is applied in order of priority (high → medium → low).
        After each edit, we record a structured entry in the edits_made list:
          {
              "edit_number"  : int — sequential edit number
              "category"     : str — what kind of edit
              "action"       : str — what was done
              "rationale"    : str — why it was done (from the edit plan)
              "before_sample": str — a short excerpt of original text
              "after_sample" : str — a short excerpt of edited text
              "impact"       : str — brief description of the improvement made
          }

        ACP CONCEPT: Transparent Edit History
        ----------------------------------------
        The edits_made list is included in the output payload sent to the
        SEOAgent and ultimately stored in the article's metadata JSON.
        This creates a permanent, auditable record of every editorial
        decision — who made it (the EditorAgent), why (data from MCP tools),
        and what changed.

        This kind of provenance tracking is essential in production content
        pipelines where compliance, brand consistency, and quality audits
        require knowing exactly how every published article was produced.

        Parameters
        ----------
        content : str
            The original draft content from WriterAgent.
        title : str
            The article title.
        topic : str
            The article topic.
        edit_plan : list
            Ordered list of edit categories from _plan_edits().
        readability_data : dict
            Output from the readability MCP tool.
        grammar_data : dict
            Output from the grammar check MCP tool.
        style_used : dict
            Style guide metadata.

        Returns
        -------
        Tuple[str, List[Dict]]
            (revised_content, edits_made)
        """
        revised = content
        edits_made: List[Dict[str, Any]] = []
        edit_num = 0

        # ---- Dispatch table: action name → handler method ---------------
        # This maps the string action names in the edit plan to the actual
        # methods that implement each edit type.  Using a dispatch table
        # keeps _apply_edits() clean and makes it trivial to add new
        # edit types in the future.
        action_dispatch = {
            "replace_wordy_phrases": self._edit_replace_wordy_phrases,
            "strengthen_vocabulary": self._edit_strengthen_vocabulary,
            "convert_passive_to_active": self._edit_passive_to_active,
            "improve_transitions": self._edit_improve_transitions,
            "strengthen_opening": self._edit_strengthen_opening,
            "strengthen_cta": self._edit_strengthen_cta,
            "improve_structure": self._edit_improve_structure,
            "break_long_sentences": self._edit_break_long_sentences,
            "simplify_vocabulary": self._edit_simplify_vocabulary,
            "expand_thin_sections": self._edit_expand_thin_sections,
        }

        for plan_item in edit_plan:
            action = plan_item.get("action", "")
            category = plan_item.get("category", "general")
            rationale = plan_item.get("rationale", "")

            handler = action_dispatch.get(action)

            if handler is None:
                self._log(
                    f"  No handler registered for action '{action}'. "
                    f"Skipping this edit item."
                )
                continue

            # Capture a sample of text before editing (for the audit record)
            before_sample = self._extract_sample(revised, chars=120)

            try:
                revised, change_description = handler(
                    revised, topic=topic, plan_item=plan_item, style_used=style_used
                )
                after_sample = self._extract_sample(revised, chars=120)

                # Check if the handler actually changed anything
                if revised != content or change_description:
                    edit_num += 1
                    edits_made.append(
                        {
                            "edit_number": edit_num,
                            "category": category,
                            "action": action,
                            "rationale": rationale,
                            "before_sample": before_sample,
                            "after_sample": after_sample,
                            "impact": change_description
                            or f"Applied {action} improvements.",
                        }
                    )
                    self._log(
                        f"  ✓ Edit {edit_num}: [{category}] {action} — "
                        f"{(change_description or '').split(chr(10))[0][:70]}"
                    )
                else:
                    self._log(
                        f"  ℹ  Action '{action}' produced no changes — "
                        f"content already meets the standard or no matching patterns found."
                    )

            except Exception as exc:
                self._log(
                    f"  ✗ Edit action '{action}' failed: {type(exc).__name__}: {exc}. "
                    f"Skipping this edit."
                )

        # ---- Final pass: clean up any double blank lines ----------------
        revised = re.sub(r"\n{3,}", "\n\n", revised)

        # ---- Add editorial revision marker at the end -------------------
        flesch = readability_data.get("flesch_score", "?")
        quality = grammar_data.get("overall_quality", "?")
        revised = revised.rstrip()
        revised += (
            f"\n\n---\n"
            f"*Reviewed by ContentForge EditorAgent. "
            f"Readability: {flesch}/100 ({readability_data.get('reading_level', '?')}). "
            f"Grammar quality: {quality}. "
            f"Edits applied: {edit_num}.*"
        )

        edit_record: Dict[str, Any] = {
            "edit_number": edit_num + 1,
            "category": "annotation",
            "action": "add_editorial_footer",
            "rationale": (
                "Adding editorial metadata footer for pipeline traceability. "
                "This footer records readability and quality metrics at the time "
                "of the editorial review, creating a permanent quality snapshot."
            ),
            "before_sample": "(no footer)",
            "after_sample": f"Flesch={flesch}, Quality={quality}",
            "impact": (
                f"Added editorial review footer. "
                f"Final readability: {flesch}/100. "
                f"Grammar quality: {quality}."
            ),
        }
        edits_made.append(edit_record)

        return revised, edits_made

    # ==========================================================================
    # INDIVIDUAL EDIT HANDLERS
    # ==========================================================================
    # Each handler has the same signature:
    #   handler(content, *, topic, plan_item, style_used) -> (revised_content, change_description)
    #
    # This uniform signature lets the dispatch table call any handler
    # without knowing its specific implementation.
    # ==========================================================================

    def _edit_replace_wordy_phrases(
        self,
        content: str,
        *,
        topic: str,
        plan_item: Dict[str, Any],
        style_used: Dict[str, Any],
    ) -> Tuple[str, str]:
        """
        Replace wordy phrases with concise alternatives.

        This implements the MCP grammar_check tool's "wordiness" suggestions
        directly in the content.  Each replacement is a direct substitution
        using the same phrase pairs the grammar checker identified.

        Before: "due to the fact that AI is advancing rapidly..."
        After:  "because AI is advancing rapidly..."

        Returns
        -------
        Tuple[str, str]
            (revised_content, change_description)
        """
        replacements = {
            "due to the fact that": "because",
            "in order to": "to",
            "at this point in time": "now",
            "in the event that": "if",
            "for the purpose of": "to",
            "it is important to note that": "note that",
            "a large number of": "many",
            "in spite of the fact that": "although",
            "with the exception of": "except",
            "prior to": "before",
            "subsequent to": "after",
            "in the near future": "soon",
            "on a daily basis": "daily",
            "at the present time": "currently",
            "in the process of": "while",
            "with regard to": "regarding",
            "in addition to the above": "additionally",
            "the majority of": "most",
        }

        revised = content
        changes: List[str] = []

        for wordy, concise in replacements.items():
            # Case-insensitive search; preserve original casing of surrounding text
            if wordy.lower() in revised.lower():
                # Use regex for case-insensitive replacement
                pattern = re.compile(re.escape(wordy), re.IGNORECASE)
                revised = pattern.sub(concise, revised)
                changes.append(f'"{wordy}" → "{concise}"')

        if not changes:
            return content, ""

        description = (
            f"Replaced {len(changes)} wordy phrase(s) with concise alternatives: "
            + "; ".join(changes[:4])
            + ("..." if len(changes) > 4 else ".")
        )
        return revised, description

    def _edit_strengthen_vocabulary(
        self,
        content: str,
        *,
        topic: str,
        plan_item: Dict[str, Any],
        style_used: Dict[str, Any],
    ) -> Tuple[str, str]:
        """
        Replace weak intensifiers with stronger, more precise vocabulary.

        Before: "This is a very important development"
        After:  "This is a pivotal development"

        The substitutions are context-aware where possible — "very good"
        becomes "excellent", not just removing "very".

        Returns
        -------
        Tuple[str, str]
        """
        # Context-aware two-word substitutions (checked first)
        two_word_subs = [
            (r"\bvery\s+important\b", "pivotal"),
            (r"\bvery\s+good\b", "excellent"),
            (r"\bvery\s+big\b", "substantial"),
            (r"\bvery\s+small\b", "minimal"),
            (r"\bvery\s+fast\b", "rapid"),
            (r"\bvery\s+slow\b", "gradual"),
            (r"\bvery\s+large\b", "enormous"),
            (r"\bvery\s+significant\b", "profound"),
            (r"\bvery\s+different\b", "markedly different"),
            (r"\bvery\s+new\b", "novel"),
            (r"\bvery\s+old\b", "long-established"),
            (r"\breally\s+important\b", "crucial"),
            (r"\breally\s+interesting\b", "compelling"),
            (r"\bquite\s+significant\b", "notable"),
            (r"\bextremely\s+important\b", "critical"),
            (r"\bincredibly\s+powerful\b", "formidable"),
        ]

        revised = content
        changes: List[str] = []

        for pattern, replacement in two_word_subs:
            if re.search(pattern, revised, re.IGNORECASE):
                before_match = re.search(pattern, revised, re.IGNORECASE)
                if before_match:
                    original_text = before_match.group(0)
                    revised = re.sub(pattern, replacement, revised, flags=re.IGNORECASE)
                    changes.append(f'"{original_text}" → "{replacement}"')

        if not changes:
            return content, ""

        description = (
            f"Replaced {len(changes)} weak intensifier phrase(s) with precise vocabulary: "
            + "; ".join(changes[:3])
            + ("..." if len(changes) > 3 else ".")
        )
        return revised, description

    def _edit_passive_to_active(
        self,
        content: str,
        *,
        topic: str,
        plan_item: Dict[str, Any],
        style_used: Dict[str, Any],
    ) -> Tuple[str, str]:
        """
        Convert selected passive voice constructions to active voice.

        Full passive-to-active conversion requires understanding sentence
        semantics (who is the actor?).  This implementation handles a set
        of common patterns that appear frequently in AI-generated blog content,
        where the actor can be inferred from context.

        Before: "has been widely adopted by enterprises"
        After:  "enterprises have widely adopted"

        Returns
        -------
        Tuple[str, str]
        """
        # Common passive patterns that can be safely converted
        # Format: (passive_pattern, active_replacement_or_None)
        passive_fixes = [
            (r"is being used by", "is used by"),  # simplify progressive passive
            (r"are being used by", "are used by"),
            (r"has been shown to", "shows"),
            (r"have been shown to", "show"),
            (r"is considered to be", "is"),
            (r"are considered to be", "are"),
            (r"it has been found that", "research shows that"),
            (r"it was discovered that", "researchers discovered that"),
            (r"it is expected that", "experts expect that"),
            (r"is believed to be", "experts believe it is"),
        ]

        revised = content
        changes_count = 0

        for passive_pattern, active_form in passive_fixes:
            if re.search(passive_pattern, revised, re.IGNORECASE):
                revised = re.sub(
                    passive_pattern, active_form, revised, flags=re.IGNORECASE
                )
                changes_count += 1

        if changes_count == 0:
            return content, ""

        description = (
            f"Converted {changes_count} passive voice construction(s) to active voice. "
            f"Active voice is more direct and engaging for '{style_used.get('tone', 'blog')}' content."
        )
        return revised, description

    def _edit_improve_transitions(
        self,
        content: str,
        *,
        topic: str,
        plan_item: Dict[str, Any],
        style_used: Dict[str, Any],
    ) -> Tuple[str, str]:
        """
        Strengthen section transitions by inserting bridging sentences.

        Good transitions guide the reader from one section to the next,
        maintaining the article's narrative momentum.  This edit adds
        a forward-looking sentence at the end of key sections where
        the section currently ends abruptly.

        Returns
        -------
        Tuple[str, str]
        """
        title_topic = topic.title()

        # Map of section ending markers to the transition sentence to insert
        # The marker is a partial match of the last sentence before a new section
        transition_additions = [
            (
                "now, let us",
                None,  # already has a transition
            ),
            (
                r"(## What Is [^\n]+\n\n[^\n]+\n\n[^\n]+\.)\n\n(## The Latest|## Key Dev)",
                r"\1\n\nWith this context established, let us now examine the developments "
                r"that are defining the current landscape.\n\n\2",
            ),
            (
                r"(## What Experts[^\n]+\n(?:.*\n)*?)\n(## "
                + re.escape(title_topic[:15]),
                None,  # complex context match — skip in this simplified implementation
            ),
        ]

        revised = content

        # A simpler, reliable approach: insert a transition phrase before each ## heading
        # if the preceding paragraph doesn't end with a transition signal word
        transition_signals = [
            "with this in mind",
            "building on this",
            "turning to",
            "next",
            "let us",
            "now consider",
            "finally",
            "in conclusion",
            "to summarise",
            "what follows",
        ]

        # Check if content already has sufficient transitions
        lower_content = content.lower()
        existing_transitions = sum(
            1 for sig in transition_signals if sig in lower_content
        )

        if existing_transitions >= 2:
            # Already has adequate transitions
            return content, ""

        # Add a meta-note about transitions rather than inserting potentially
        # jarring sentences without full sentence-context awareness
        # (a production LLM-backed agent would insert these contextually)
        note = (
            "\n\n<!-- Editor note: Additional transition sentences recommended "
            "between sections to improve narrative flow. -->"
        )
        if "<!-- Editor note:" not in revised:
            revised = revised + note

        description = (
            f"Noted that {max(0, 2 - existing_transitions)} additional transition "
            f"sentence(s) would improve section flow. "
            f"Current transition density: {existing_transitions} signal(s) found."
        )
        return revised, description

    def _edit_strengthen_opening(
        self,
        content: str,
        *,
        topic: str,
        plan_item: Dict[str, Any],
        style_used: Dict[str, Any],
    ) -> Tuple[str, str]:
        """
        Review and optionally strengthen the opening paragraph.

        The opening paragraph is the most important in the article.
        This edit ensures:
          1. The first sentence is punchy and direct (under 20 words)
          2. The topic is named explicitly in the first paragraph
          3. There is a clear benefit or tension in the first 100 words

        Returns
        -------
        Tuple[str, str]
        """
        title_topic = topic.title()

        # Find the introduction section
        intro_match = re.search(
            r"## Introduction\n\n(.+?)(?=\n\n## |\Z)", content, re.DOTALL
        )

        if not intro_match:
            return content, ""

        intro_text = intro_match.group(1)
        first_sentence_match = re.match(r"([^.!?]+[.!?])", intro_text)

        if not first_sentence_match:
            return content, ""

        first_sentence = first_sentence_match.group(1).strip()
        word_count = len(first_sentence.split())

        if word_count <= 20 and topic.lower() in first_sentence.lower():
            # Opening is already strong
            return content, ""

        # The opening can be improved — add an editorial annotation
        strength_note = (
            f"\n\n<!-- Editor note: Consider shortening the opening sentence "
            f"(currently {word_count} words) to under 20 words for maximum impact. "
            f"Ensure '{title_topic}' appears in the first sentence. -->"
        )

        if "<!-- Editor note: Consider shortening" not in content:
            # Insert annotation right after the introduction heading
            revised = content.replace(
                "## Introduction\n\n",
                "## Introduction\n\n",
                1,
            )
            revised = revised + strength_note
        else:
            return content, ""

        description = (
            f"Flagged opening sentence for refinement: "
            f"first sentence is {word_count} words "
            f"(target: ≤20 for maximum impact). "
            f"Added editorial annotation with specific guidance."
        )
        return revised, description

    def _edit_strengthen_cta(
        self,
        content: str,
        *,
        topic: str,
        plan_item: Dict[str, Any],
        style_used: Dict[str, Any],
    ) -> Tuple[str, str]:
        """
        Strengthen the call-to-action (CTA) in the conclusion.

        A strong CTA should:
          - Be specific (what exactly should the reader DO?)
          - Be actionable (can they do it right now?)
          - Relate to the article's topic
          - Use imperative mood ("Start", "Explore", "Learn")

        This edit checks if the existing CTA is sufficiently specific
        and improves it if needed.

        Returns
        -------
        Tuple[str, str]
        """
        title_topic = topic.title()

        # Look for the final thoughts / conclusion section
        conclusion_match = re.search(
            r"(## Final Thoughts|## Conclusion|## Takeaway)(.*?)(\Z|---)",
            content,
            re.DOTALL,
        )

        if not conclusion_match:
            return content, ""

        conclusion_text = conclusion_match.group(2)

        # Check for CTA markers in the conclusion
        cta_markers = [
            "what you can do",
            "next step",
            "start by",
            "begin with",
            "take action",
            "sign up",
            "learn more",
            "explore",
            "discover",
            "try",
        ]

        has_cta = any(marker in conclusion_text.lower() for marker in cta_markers)

        if has_cta:
            # CTA already exists — enhance it
            enhanced_cta = (
                f"\n\n**Ready to take the next step?** "
                f"Bookmark this guide and share it with a colleague who needs to "
                f"understand {topic}. "
                f"The more people engage with {title_topic} today, the better prepared "
                f"we all are for tomorrow's opportunities."
            )

            # Only add if we haven't already added this enhancement
            if "Ready to take the next step?" not in content:
                # Insert before the editorial footer
                if "---\n*Draft produced by" in content:
                    revised = content.replace(
                        "\n\n---\n*Draft produced by",
                        enhanced_cta + "\n\n---\n*Draft produced by",
                    )
                elif "---\n*Reviewed by" in content:
                    revised = content.replace(
                        "\n\n---\n*Reviewed by",
                        enhanced_cta + "\n\n---\n*Reviewed by",
                    )
                else:
                    revised = content + enhanced_cta
            else:
                return content, ""

            description = (
                "Enhanced the existing call-to-action with a shareable prompt "
                "and a forward-looking motivation statement. "
                "Strong CTAs improve reader engagement metrics and social sharing."
            )
        else:
            # No CTA found — insert a default one
            new_cta = (
                f"\n\n**Your next step:** Identify one practical way to engage with "
                f"{topic} in your professional or personal context this week. "
                f"Even small, deliberate steps compound into significant advantage over time."
            )

            if "Your next step:" not in content:
                if "## Final Thoughts\n" in content:
                    # Find the last paragraph of the conclusion and append
                    revised = (
                        content.replace(
                            "\n\n---\n*Draft produced by",
                            new_cta + "\n\n---\n*Draft produced by",
                        )
                        if "---\n*Draft produced by" in content
                        else content + new_cta
                    )
                else:
                    revised = content + new_cta
            else:
                return content, ""

            description = (
                "Added a clear call-to-action to the conclusion. "
                "Articles with specific CTAs generate significantly higher "
                "engagement than those with passive endings."
            )

        return revised, description

    def _edit_improve_structure(
        self,
        content: str,
        *,
        topic: str,
        plan_item: Dict[str, Any],
        style_used: Dict[str, Any],
    ) -> Tuple[str, str]:
        """
        Address structural issues flagged by the grammar_check tool.

        Currently handles:
          - Very long paragraphs (> 150 words): adds a split annotation
          - Missing blank lines between sections

        Returns
        -------
        Tuple[str, str]
        """
        revised = content
        changes: List[str] = []

        # Ensure double blank lines before ## headings
        revised = re.sub(r"([^\n])\n(## )", r"\1\n\n\2", revised)

        # Check for overly long paragraphs and add annotations
        paragraphs = revised.split("\n\n")
        new_paragraphs: List[str] = []
        long_para_count = 0

        for para in paragraphs:
            word_count = len(para.split())
            if (
                word_count > 150
                and not para.startswith("#")
                and not para.startswith("---")
            ):
                # Split at the midpoint sentence
                sentences = re.split(r"(?<=[.!?])\s+", para)
                mid = len(sentences) // 2
                if mid > 0:
                    first_half = " ".join(sentences[:mid])
                    second_half = " ".join(sentences[mid:])
                    new_paragraphs.extend([first_half, second_half])
                    long_para_count += 1
                    changes.append(
                        f"Split {word_count}-word paragraph into two paragraphs"
                    )
                else:
                    new_paragraphs.append(para)
            else:
                new_paragraphs.append(para)

        revised = "\n\n".join(new_paragraphs)

        if not changes:
            return content, ""

        description = (
            f"Improved structural formatting: {'; '.join(changes[:3])}. "
            f"Shorter paragraphs (target: 60-100 words) improve web readability "
            f"and reduce reader fatigue."
        )
        return revised, description

    def _edit_break_long_sentences(
        self,
        content: str,
        *,
        topic: str,
        plan_item: Dict[str, Any],
        style_used: Dict[str, Any],
    ) -> Tuple[str, str]:
        """
        Attempt to break overly long sentences at natural conjunction points.

        This is a heuristic approach: we look for sentences over 40 words
        that contain coordinating conjunctions (and, but, because, which)
        and split them there.

        Before: "AI is transforming industries at an unprecedented pace, and
                 the companies that understand this early will gain competitive
                 advantage while those that ignore it risk being left behind."
        After:  "AI is transforming industries at an unprecedented pace.
                 Companies that understand this early will gain competitive
                 advantage. Those that ignore it risk being left behind."

        Returns
        -------
        Tuple[str, str]
        """
        # Split content into sentences
        sentence_pattern = re.compile(r'(?<=[.!?])\s+(?=[A-Z"\'(])')
        paragraphs = content.split("\n\n")
        new_paragraphs: List[str] = []
        splits_made = 0

        for para in paragraphs:
            # Don't process headings, code blocks, or bullet lists
            if para.startswith("#") or para.startswith("-") or para.startswith("*"):
                new_paragraphs.append(para)
                continue

            sentences = sentence_pattern.split(para)
            new_sentences: List[str] = []

            for sentence in sentences:
                words = sentence.split()
                if len(words) > 38:
                    # Try to split at ", and", ", but", ", because", " — "
                    split_patterns = [
                        r",\s+and\s+",
                        r",\s+but\s+",
                        r";\s+",
                        r"\s+—\s+(?=[A-Z])",
                        r",\s+which\s+means\s+",
                    ]
                    split_done = False
                    for sp in split_patterns:
                        parts = re.split(sp, sentence, maxsplit=1, flags=re.IGNORECASE)
                        if len(parts) == 2 and len(parts[0].split()) >= 10:
                            # Capitalise the second part and add period to first
                            first = parts[0].rstrip(",;").rstrip() + "."
                            second = parts[1][0].upper() + parts[1][1:]
                            new_sentences.extend([first, second])
                            splits_made += 1
                            split_done = True
                            break
                    if not split_done:
                        new_sentences.append(sentence)
                else:
                    new_sentences.append(sentence)

            new_paragraphs.append(" ".join(new_sentences))

        if splits_made == 0:
            return content, ""

        revised = "\n\n".join(new_paragraphs)
        description = (
            f"Broke {splits_made} long sentence(s) (>38 words) into shorter, "
            f"more digestible units at natural conjunction points. "
            f"Target average sentence length for blog content: under 20 words."
        )
        return revised, description

    def _edit_simplify_vocabulary(
        self,
        content: str,
        *,
        topic: str,
        plan_item: Dict[str, Any],
        style_used: Dict[str, Any],
    ) -> Tuple[str, str]:
        """
        Replace complex/formal vocabulary with simpler equivalents.

        Targeted at improving the Flesch Reading Ease score for blog content.
        Only applies to clearly interchangeable terms where the simpler word
        carries the same meaning.

        Returns
        -------
        Tuple[str, str]
        """
        # Maps formal/complex → plain equivalent
        # Only include pairs where the swap is safe and unambiguous
        simplifications = {
            r"\butilise\b": "use",
            r"\butilization\b": "use",
            r"\bsubsequently\b": "then",
            r"\bfacilitate\b": "help",
            r"\bcommence\b": "start",
            r"\bterminate\b": "end",
            r"\bdemonstrate\b": "show",
            r"\binvestigate\b": "study",
            r"\bascertain\b": "find out",
            r"\bpurchase\b": "buy",
            r"\bacquire\b": "get",
            r"\bapproximately\b": "about",
            r"\bnumerous\b": "many",
            r"\badditionally\b": "also",
            r"\bnevertheless\b": "however",
            r"\bconsequently\b": "so",
            r"\bimplemented\b": "used",
        }

        revised = content
        changes: List[str] = []

        for pattern, replacement in simplifications.items():
            if re.search(pattern, revised, re.IGNORECASE):
                revised = re.sub(pattern, replacement, revised, flags=re.IGNORECASE)
                # Extract what was matched for the change log
                original_word = pattern.strip(r"\b")
                changes.append(f"{original_word} → {replacement}")

        if not changes:
            return content, ""

        description = (
            f"Simplified {len(changes)} complex word(s) to plain equivalents: "
            + ", ".join(changes[:5])
            + ("..." if len(changes) > 5 else ".")
            + " This improves the Flesch Reading Ease score and audience accessibility."
        )
        return revised, description

    def _edit_expand_thin_sections(
        self,
        content: str,
        *,
        topic: str,
        plan_item: Dict[str, Any],
        style_used: Dict[str, Any],
    ) -> Tuple[str, str]:
        """
        Flag and annotate sections that are too thin (under-developed).

        When the article's word count is significantly below the target,
        this edit identifies which sections are shortest and adds editorial
        annotations requesting expansion.  A production LLM agent would
        actually write the expanded content; here we add structured annotations
        that a human editor (or a future LLM pass) can act on.

        Returns
        -------
        Tuple[str, str]
        """
        target_word_count: int = style_used.get("target_word_count", 1200)
        current_word_count = len(content.split())

        if current_word_count >= target_word_count * 0.85:
            return content, ""

        # Find sections and their word counts
        sections = re.split(r"(## [^\n]+)", content)
        section_words: List[Tuple[str, int]] = []

        for i in range(1, len(sections) - 1, 2):
            heading = sections[i].strip()
            body = sections[i + 1] if i + 1 < len(sections) else ""
            wc = len(body.split())
            section_words.append((heading, wc))

        # Find the thinnest non-footer sections
        thin_sections = [
            (heading, wc)
            for heading, wc in section_words
            if wc < 80
            and "Final Thoughts" not in heading
            and "Conclusion" not in heading
        ]

        if not thin_sections:
            return content, ""

        # Add annotations to thin sections
        revised = content
        annotated = 0
        for heading, wc in thin_sections[:2]:
            annotation = (
                f"\n\n<!-- Editor note: This section ({wc} words) could be expanded "
                f"with an additional paragraph covering a specific example, case study, "
                f"or statistic. Target: 100-150 words per body section. -->"
            )
            if annotation not in revised and heading in revised:
                # Insert annotation after the section's first paragraph
                pattern = re.escape(heading) + r"(\n\n[^\n]+(?:\n[^\n]+)*)"
                match = re.search(pattern, revised)
                if match:
                    insert_pos = match.end()
                    revised = revised[:insert_pos] + annotation + revised[insert_pos:]
                    annotated += 1

        if annotated == 0:
            return content, ""

        shortfall = target_word_count - current_word_count
        description = (
            f"Flagged {annotated} thin section(s) for expansion. "
            f"Article is {current_word_count} words — {shortfall} below the "
            f"{target_word_count}-word target. "
            f"Added expansion guidance annotations to shortest sections."
        )
        return revised, description

    # ==========================================================================
    # OUTPUT HELPERS
    # ==========================================================================

    def _print_edit_summary(
        self,
        edits_made: List[Dict[str, Any]],
        readability_data: Dict[str, Any],
        grammar_data: Dict[str, Any],
    ) -> None:
        """
        Print a readable editorial summary to the console.

        This summary shows — in a clear, human-readable format — exactly
        what the EditorAgent did to the draft.  It is the ACP equivalent
        of an editor's revision memo.

        In a real editorial system, this summary would be:
          - Stored in the CMS alongside the article
          - Sent to the original writer as feedback
          - Used by the QA team to audit the pipeline's edit quality

        Parameters
        ----------
        edits_made : list[dict]
            The structured edit records from _apply_edits().
        readability_data : dict
            Readability metrics from the MCP tool.
        grammar_data : dict
            Grammar check results from the MCP tool.
        """
        print("\n  ╔═ EDITORIAL REVIEW SUMMARY ══════════════════════════════════╗")
        print(
            f"  ║  Readability : Flesch {readability_data.get('flesch_score', '?')}/100 "
            f"— {readability_data.get('reading_level', '?')}"
        )
        print(
            f"  ║  Avg sentence: {readability_data.get('avg_sentence_length', '?')} words  "
            f"│  Read time: {readability_data.get('reading_time_minutes', '?')} min"
        )
        print(
            f"  ║  Grammar     : {grammar_data.get('overall_quality', '?')} "
            f"({grammar_data.get('issues_found', 0)} issue(s))"
        )
        print(f"  ║  Edits made  : {len(edits_made)}")
        print("  ╠══════════════════════════════════════════════════════════════╣")

        substantive_edits = [e for e in edits_made if e.get("category") != "annotation"]
        for edit in substantive_edits:
            category = edit.get("category", "?").upper()
            action = edit.get("action", "?").replace("_", " ")
            impact = (edit.get("impact", "") or "")[:65]
            print(f"  ║  [{edit.get('edit_number', '?')}] [{category:<12}] {action}")
            if impact:
                print(f"  ║       → {impact}")

        print("  ╚══════════════════════════════════════════════════════════════╝")

    @staticmethod
    def _extract_sample(content: str, chars: int = 120) -> str:
        """
        Extract a short text sample from the start of the content
        for use in before/after edit records.

        Skips Markdown headings (## ...) and finds the first prose
        paragraph to ensure the sample is meaningful text.

        Parameters
        ----------
        content : str
            The full content to sample from.
        chars : int
            Maximum characters to include.

        Returns
        -------
        str
            A short sample of the content's prose.
        """
        # Skip heading lines and find first paragraph
        lines = content.split("\n")
        prose_lines: List[str] = []
        for line in lines:
            stripped = line.strip()
            if (
                stripped
                and not stripped.startswith("#")
                and not stripped.startswith("---")
            ):
                prose_lines.append(stripped)
                if sum(len(l) for l in prose_lines) >= chars:
                    break

        sample = " ".join(prose_lines)
        return sample[:chars] + ("..." if len(sample) > chars else "")

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
            name="EditorAgent",
            description=(
                "Stage 3: Receives the article draft from WriterAgent and applies "
                "a professional editorial pass. Uses MCP tools to measure readability "
                "(Flesch score, sentence length) and check grammar/style. Makes targeted "
                "improvements: tightening wordy phrases, converting passive voice, "
                "strengthening the introduction and CTA, and improving paragraph structure. "
                "Forwards the revised content with a full edit history to SEOAgent."
            ),
            input_schema={
                "title": "str — article title from WriterAgent",
                "draft_content": "str — full Markdown article draft",
                "word_count": "int — word count from WriterAgent",
                "style_used": "dict — style guide metadata (tone, target_word_count, etc.)",
                "topic": "str — article topic (passed through)",
                "research_brief": "dict — original research data (pass-through)",
            },
            output_schema={
                "title": "str — article title (unchanged)",
                "revised_content": "str — editorially improved Markdown content",
                "edits_made": "list[dict] — structured record of every edit applied",
                "readability_score": "float — Flesch Reading Ease score (0-100)",
                "readability_level": "str — human-readable reading level label",
                "grammar_quality": "str — overall grammar quality assessment",
                "word_count": "int — word count of the revised content",
                "word_count_delta": "int — change in word count (positive = added words)",
                "reading_time_minutes": "float — estimated reading time",
                "readability_full": "dict — full readability metrics from MCP tool",
                "topic": "str — passed through",
                "style_used": "dict — passed through",
                "research_brief": "dict — passed through",
                "edited_by": "str — agent_id of this agent ('editor')",
            },
            status=self._status,
            pipeline_stage=3,
            tags=["editing", "quality-assurance", "readability", "grammar", "stage-3"],
            metadata={
                "mcp_tools_used": ["check_readability", "grammar_check"],
                "sends_to": ["seo"],
                "receives_from": ["writer"],
                "output_format": "markdown",
            },
        )
