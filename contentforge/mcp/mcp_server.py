# =============================================================================
# mcp/mcp_server.py  —  MCP Server (Model Context Protocol)
# =============================================================================
#
# WHY THIS FILE EXISTS
# --------------------
# Agents need to access external resources: databases, file systems, APIs,
# style guides, keyword databases, and content management systems.
#
# Without MCP, each agent would hard-code its own file I/O, API calls, and
# resource access logic.  This creates:
#   - Duplication: every agent reimplements file reading
#   - Coupling: agents depend directly on file paths and data formats
#   - Fragility: changing a data source requires editing every agent
#
# MCP (Model Context Protocol) solves this by providing a TOOL SERVER:
#   - Tools are named, documented functions registered on the server
#   - Agents invoke tools by name through a client interface
#   - The server handles all resource access details
#   - Any agent can use any tool without knowing HOW it's implemented
#
# ANALOGY
# -------
# Think of the MCP Server as a shared API layer:
#   - The server exposes "endpoints" (tools) like a REST API
#   - Agents are "clients" that call these endpoints
#   - The server manages all the messy implementation details
#   - Adding a new tool = adding one method to this server
#
# MCP vs direct file access
# -------------------------
# WITHOUT MCP:  agent.py opens "data/knowledge_base.json" directly
#               → agent is coupled to the file path and JSON structure
#
# WITH MCP:     agent calls mcp_client.call_tool("search_topic", topic="AI")
#               → agent only knows the tool name and its parameter contract
#               → the server can change the data source (file → database → API)
#                 without any agent code changing
#
# TOOLS PROVIDED BY THIS SERVER
# ------------------------------
#   search_topic(topic)              — Look up research facts from knowledge base
#   get_style_guide(content_type)    — Fetch writing style guidelines
#   get_seo_keywords(topic)          — Get SEO keyword recommendations
#   save_draft(title, content)       — Persist a draft to temp storage
#   publish_article(title, content,  — Write final article to published directory
#                   metadata)
#   check_readability(text)          — Analyze text readability metrics
#   grammar_check(text)              — Simulate grammar issue detection
# =============================================================================

import json
import os
import re
import tempfile
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple


class MCPTool:
    """
    MCP Tool descriptor — metadata about a single registered tool.

    MCP CONCEPT: Tool Registration
    --------------------------------
    In MCP, every capability exposed by the server is a "tool" — a named,
    typed, documented function.  Registering tools explicitly (rather than
    just defining methods) means:
      - Clients can DISCOVER available tools at runtime (list_tools())
      - Tools have a formal description that agents (and LLMs) can read
      - The server can validate calls before dispatching them
      - Tools can be enabled/disabled without changing client code

    This mirrors how OpenAI function calling works: you define the function
    schema, and the model decides when and how to call it.

    Fields
    ------
    name        : Unique tool identifier used in call_tool() invocations
    description : Human/LLM-readable description of what the tool does
    parameters  : Dict describing expected parameter names and their types/descriptions
    handler     : The actual callable that executes the tool logic
    """

    def __init__(
        self,
        name: str,
        description: str,
        parameters: Dict[str, str],
        handler: Callable,
    ) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters  # {param_name: "type — description"}
        self.handler = handler
        self.call_count: int = 0  # Track usage for observability

    def __repr__(self) -> str:
        return f"MCPTool(name={self.name!r}, calls={self.call_count})"


class MCPServer:
    """
    MCP Server — the central tool provider for all ContentForge agents.

    This server hosts all tools that agents need to access external resources.
    It manages:
      1. TOOL REGISTRY     : Catalogue of available tools (name → MCPTool)
      2. DATA LOADING      : Reads JSON data files from the data/ directory
      3. TOOL EXECUTION    : Dispatches tool calls to their handlers
      4. TEMP STORAGE      : Manages draft files during pipeline processing
      5. PUBLISHING        : Writes completed articles to the published/ directory

    MCP CONCEPT: Server-Side Resource Management
    ---------------------------------------------
    The server is the ONLY component that knows about:
      - File system paths (where data files live)
      - Data formats (how JSON is structured)
      - External service contracts (what an API would return)

    Agents know NONE of this.  They only know:
      - What tools exist (tool names)
      - What parameters each tool needs
      - What the tool returns

    This separation of concerns is the core MCP value proposition.

    Initialization
    --------------
    The server finds its data directory relative to THIS file's location,
    so the pipeline works regardless of what directory you run it from.
    It loads all data files once at startup and caches them in memory.
    """

    def __init__(self) -> None:
        # ----------------------------------------------------------------
        # Resolve data directory path relative to this file.
        # Using __file__ makes the server location-independent.
        # ----------------------------------------------------------------
        self._base_dir: str = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))
        )
        self._data_dir: str = os.path.join(self._base_dir, "data")
        self._published_dir: str = os.path.join(self._data_dir, "published")
        self._temp_dir: str = tempfile.gettempdir()

        # ----------------------------------------------------------------
        # Load all data files into memory at startup.
        # In a real MCP server, some of these might be database connections
        # or API client initializations instead of file reads.
        # ----------------------------------------------------------------
        self._knowledge_base: Dict[str, Any] = self._load_json("knowledge_base.json")
        self._style_guides: Dict[str, Any] = self._load_json("style_guides.json")
        self._seo_keywords: Dict[str, Any] = self._load_json("seo_keywords.json")

        # ----------------------------------------------------------------
        # Tool registry: maps tool name (str) → MCPTool instance.
        # Tools are registered in __init__ so they are available
        # immediately when the first client connects.
        # ----------------------------------------------------------------
        self._tools: Dict[str, MCPTool] = {}

        # ----------------------------------------------------------------
        # Draft storage: maps draft_key → file path.
        # Tracks where each saved draft lives so it can be retrieved later.
        # ----------------------------------------------------------------
        self._draft_registry: Dict[str, str] = {}

        # ----------------------------------------------------------------
        # Ensure the published output directory exists.
        # ----------------------------------------------------------------
        os.makedirs(self._published_dir, exist_ok=True)

        # ----------------------------------------------------------------
        # Register all tools (must be last so self._ data is ready).
        # ----------------------------------------------------------------
        self._register_all_tools()

        print(
            f"  [MCP SERVER] ✓  MCPServer initialized with {len(self._tools)} tools. "
            f"Data dir: {os.path.relpath(self._data_dir)}"
        )

    # ==================================================================
    # DATA LOADING
    # ==================================================================

    def _load_json(self, filename: str) -> Dict[str, Any]:
        """
        Load a JSON file from the data directory.

        Returns an empty dict if the file is not found, rather than raising,
        so the server starts up even if some data files are missing.
        This mirrors how real servers handle optional configuration files.
        """
        filepath = os.path.join(self._data_dir, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            print(f"  [MCP SERVER] ✓  Loaded data file: {filename}")
            return data
        except FileNotFoundError:
            print(
                f"  [MCP SERVER] ⚠  Data file not found: {filename} (using empty dict)"
            )
            return {}
        except json.JSONDecodeError as exc:
            print(f"  [MCP SERVER] ✗  JSON parse error in {filename}: {exc}")
            return {}

    # ==================================================================
    # TOOL REGISTRATION
    # ==================================================================

    def _register_all_tools(self) -> None:
        """
        Register every tool this server provides.

        MCP CONCEPT: Declarative Tool Registration
        --------------------------------------------
        By registering tools explicitly (name + description + parameter schema
        + handler), the server self-documents its capabilities.  A client can
        call list_tools() and receive a complete catalogue without needing
        any external documentation.

        This is analogous to OpenAPI/Swagger specs for REST APIs, or
        GraphQL introspection — the server describes itself.
        """
        tools_to_register: List[Tuple[str, str, Dict[str, str], Callable]] = [
            (
                "search_topic",
                (
                    "Search the knowledge base for facts, key people, recent developments, "
                    "and statistics about a given topic. Returns a rich research package "
                    "suitable for article writing."
                ),
                {
                    "topic": "str — The topic to research (e.g. 'artificial intelligence')",
                },
                self._tool_search_topic,
            ),
            (
                "get_style_guide",
                (
                    "Retrieve the writing style guide for a specific content type. "
                    "Returns tone, structure, word count targets, and writing tips."
                ),
                {
                    "content_type": "str — Content format: 'blog', 'technical', or 'news'",
                },
                self._tool_get_style_guide,
            ),
            (
                "get_seo_keywords",
                (
                    "Get SEO keyword recommendations for a topic, including primary "
                    "keywords, secondary keywords, long-tail phrases, and search volumes."
                ),
                {
                    "topic": "str — The topic to get keywords for",
                },
                self._tool_get_seo_keywords,
            ),
            (
                "save_draft",
                (
                    "Save a content draft to temporary storage during pipeline processing. "
                    "Returns the file path where the draft was saved."
                ),
                {
                    "title": "str — The article title",
                    "content": "str — The full draft content (Markdown)",
                },
                self._tool_save_draft,
            ),
            (
                "publish_article",
                (
                    "Publish the final article to the published/ directory as both "
                    "a Markdown file and a JSON metadata file. Returns publication details."
                ),
                {
                    "title": "str — The final article title",
                    "content": "str — The final article content (Markdown)",
                    "metadata": "dict — SEO metadata, tags, publication info",
                },
                self._tool_publish_article,
            ),
            (
                "check_readability",
                (
                    "Analyze the readability of a text and return metrics including "
                    "word count, average sentence length, paragraph count, and a "
                    "simulated Flesch-Kincaid reading ease score."
                ),
                {
                    "text": "str — The text to analyze",
                },
                self._tool_check_readability,
            ),
            (
                "grammar_check",
                (
                    "Perform a simulated grammar and style check on the text. "
                    "Returns a list of issues found with suggestions for improvement."
                ),
                {
                    "text": "str — The text to grammar-check",
                },
                self._tool_grammar_check,
            ),
        ]

        for name, description, parameters, handler in tools_to_register:
            self._tools[name] = MCPTool(
                name=name,
                description=description,
                parameters=parameters,
                handler=handler,
            )

        print(
            f"  [MCP SERVER] ✓  Registered {len(self._tools)} tools: "
            f"{list(self._tools.keys())}"
        )

    # ==================================================================
    # TOOL DISPATCH
    # ==================================================================

    def call_tool(self, tool_name: str, **kwargs) -> Dict[str, Any]:
        """
        Execute a registered tool by name and return its result.

        MCP CONCEPT: Unified Tool Invocation
        --------------------------------------
        All tool calls go through this single dispatch method, which:
          1. Validates the tool exists
          2. Logs the invocation (full observability)
          3. Executes the handler
          4. Wraps the result in a standard response envelope
          5. Increments the tool's call counter
          6. Handles errors gracefully (never crash the pipeline)

        The standard response envelope always contains:
          {
            "success": bool,
            "tool": str,           # which tool was called
            "result": Any,         # the actual tool output
            "error": str | None,   # error message if success=False
            "timestamp": str       # when the call was made
          }

        This envelope mirrors MCP's standard tool result format and ensures
        callers always get a predictable structure regardless of which tool
        they called or whether it succeeded.

        Parameters
        ----------
        tool_name : str
            The name of the tool to invoke (must be in the registry).
        **kwargs
            Tool-specific parameters passed directly to the handler.

        Returns
        -------
        Dict[str, Any]
            Standard MCP response envelope.
        """
        # Tool existence check
        if tool_name not in self._tools:
            available = list(self._tools.keys())
            print(
                f"  [MCP SERVER] ✗  Unknown tool '{tool_name}'. "
                f"Available tools: {available}"
            )
            return {
                "success": False,
                "tool": tool_name,
                "result": None,
                "error": f"Tool '{tool_name}' not registered. Available: {available}",
                "timestamp": datetime.now().isoformat(),
            }

        tool = self._tools[tool_name]
        tool.call_count += 1

        # Execute the handler, catching all exceptions so the pipeline
        # never crashes due to a tool failure — mirrors real MCP behavior
        try:
            result = tool.handler(**kwargs)
            return {
                "success": True,
                "tool": tool_name,
                "result": result,
                "error": None,
                "timestamp": datetime.now().isoformat(),
            }
        except TypeError as exc:
            # Wrong parameters — likely a developer mistake
            error_msg = f"Parameter error calling '{tool_name}': {exc}"
            print(f"  [MCP SERVER] ✗  {error_msg}")
            return {
                "success": False,
                "tool": tool_name,
                "result": None,
                "error": error_msg,
                "timestamp": datetime.now().isoformat(),
            }
        except Exception as exc:
            # Any other error — log and return gracefully
            error_msg = f"Error in tool '{tool_name}': {type(exc).__name__}: {exc}"
            print(f"  [MCP SERVER] ✗  {error_msg}")
            return {
                "success": False,
                "tool": tool_name,
                "result": None,
                "error": error_msg,
                "timestamp": datetime.now().isoformat(),
            }

    def list_tools(self) -> List[Dict[str, Any]]:
        """
        Return a catalogue of all registered tools with their descriptions
        and parameter schemas.

        MCP CONCEPT: Tool Discovery
        ----------------------------
        Clients can ask "what can you do?" before deciding which tool to call.
        This is called tool discovery or capability introspection.

        In production MCP implementations (like Anthropic's MCP spec), this
        is the `tools/list` endpoint that LLM clients call to populate their
        tool-use context window.

        Returns
        -------
        List of dicts, each containing: name, description, parameters, call_count
        """
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
                "call_count": tool.call_count,
            }
            for tool in self._tools.values()
        ]

    def print_tool_catalogue(self) -> None:
        """Print all available tools in a human-readable format."""
        print("\n  ── MCP SERVER TOOL CATALOGUE ────────────────────────────────")
        for i, tool in enumerate(self._tools.values(), 1):
            print(f"  [{i}] {tool.name}")
            print(f"      {tool.description[:80]}...")
            params = ", ".join(
                f"{k}: {v.split(' — ')[0]}" for k, v in tool.parameters.items()
            )
            print(f"      Parameters: ({params})")
        print("  ─────────────────────────────────────────────────────────────")

    # ==================================================================
    # TOOL IMPLEMENTATIONS
    # ==================================================================
    # Each _tool_* method is a pure function: given inputs, return outputs.
    # They have no side effects except file writes (save_draft, publish).
    # This makes them easy to test in isolation.
    # ==================================================================

    def _tool_search_topic(self, topic: str) -> Dict[str, Any]:
        """
        MCP TOOL: search_topic
        ----------------------
        Simulates a knowledge-base search.  In production this would call
        a vector database (Pinecone, Weaviate), a search API (Bing, Google),
        or a retrieval-augmented generation (RAG) pipeline.

        The tool normalizes the topic to lowercase for case-insensitive
        matching, then does a three-level lookup:
          1. Exact match (best)
          2. Partial match (topic is a substring of a key)
          3. Key is a substring of topic

        If no match is found, returns a minimal "no results" structure
        rather than raising — the caller can decide how to handle missing data.
        """
        topic_lower = topic.lower().strip()

        # Level 1: exact match
        if topic_lower in self._knowledge_base:
            data = self._knowledge_base[topic_lower]
            match_type = "exact"

        else:
            # Level 2 & 3: fuzzy substring match
            data = None
            match_type = "none"
            for key in self._knowledge_base:
                if topic_lower in key or key in topic_lower:
                    data = self._knowledge_base[key]
                    match_type = f"partial ({key})"
                    break

        if data is None:
            # Return empty-but-valid structure so the pipeline can continue
            available_topics = list(self._knowledge_base.keys())
            return {
                "topic": topic,
                "found": False,
                "match_type": "none",
                "available_topics": available_topics,
                "facts": [f"No pre-existing knowledge found for '{topic}'"],
                "key_people": [],
                "recent_developments": [],
                "statistics": {},
                "subtopics": [],
            }

        return {
            "topic": topic,
            "found": True,
            "match_type": match_type,
            "facts": data.get("facts", []),
            "key_people": data.get("key_people", []),
            "recent_developments": data.get("recent_developments", []),
            "statistics": data.get("statistics", {}),
            "subtopics": data.get("subtopics", []),
        }

    def _tool_get_style_guide(self, content_type: str) -> Dict[str, Any]:
        """
        MCP TOOL: get_style_guide
        --------------------------
        Returns the writing style guide for the requested content type.
        In production this might query a CMS or editorial guidelines API.

        The guide tells the WriterAgent:
          - What tone to use (conversational, technical, news-style)
          - How to structure the content (blog hook → body → CTA)
          - Target word count and paragraph length
          - Specific tips and anti-patterns to avoid
        """
        content_type_lower = content_type.lower().strip()

        if content_type_lower in self._style_guides:
            guide = self._style_guides[content_type_lower]
            return {
                "content_type": content_type,
                "found": True,
                "tone": guide.get("tone", "neutral"),
                "structure": guide.get("structure", []),
                "avg_word_count": guide.get("avg_word_count", 800),
                "paragraph_length": guide.get("paragraph_length", "3-4 sentences"),
                "use_subheadings": guide.get("use_subheadings", True),
                "reading_level": guide.get("reading_level", "general"),
                "tips": guide.get("tips", []),
                "forbidden": guide.get("forbidden", []),
            }
        else:
            # Fall back to blog style if requested type is unknown
            available = list(self._style_guides.keys())
            print(
                f"  [MCP SERVER] ⚠  Style guide '{content_type}' not found. "
                f"Falling back to 'blog'. Available: {available}"
            )
            fallback = self._style_guides.get("blog", {})
            return {
                "content_type": "blog",
                "found": False,
                "requested": content_type,
                "note": f"Requested '{content_type}' not found; using 'blog' fallback",
                "tone": fallback.get("tone", "conversational yet authoritative"),
                "structure": fallback.get("structure", []),
                "avg_word_count": fallback.get("avg_word_count", 1200),
                "paragraph_length": fallback.get("paragraph_length", "3-4 sentences"),
                "use_subheadings": fallback.get("use_subheadings", True),
                "reading_level": fallback.get("reading_level", "8th grade"),
                "tips": fallback.get("tips", []),
                "forbidden": fallback.get("forbidden", []),
            }

    def _tool_get_seo_keywords(self, topic: str) -> Dict[str, Any]:
        """
        MCP TOOL: get_seo_keywords
        ---------------------------
        Returns SEO keyword data for the given topic.  In production this
        would query tools like SEMrush, Ahrefs, Google Keyword Planner, or
        Moz to get live search volume and competition data.

        The SEOAgent uses this to:
          - Identify which keywords to include in the article
          - Optimize keyword density (avoid over-stuffing)
          - Generate meta titles and descriptions
          - Select relevant tags for the CMS
        """
        topic_lower = topic.lower().strip()

        # Try exact match first, then partial match
        data = self._seo_keywords.get(topic_lower)
        if data is None:
            for key in self._seo_keywords:
                if topic_lower in key or key in topic_lower:
                    data = self._seo_keywords[key]
                    break

        if data is None:
            # Generate minimal keyword data from the topic itself
            words = topic_lower.split()
            return {
                "topic": topic,
                "found": False,
                "primary": [topic_lower],
                "secondary": words if len(words) > 1 else [],
                "long_tail": [
                    f"what is {topic_lower}",
                    f"how does {topic_lower} work",
                    f"benefits of {topic_lower}",
                ],
                "search_volume": {topic_lower: 1000},
                "related_topics": [],
                "competitor_keywords": [],
            }

        return {
            "topic": topic,
            "found": True,
            "primary": data.get("primary", []),
            "secondary": data.get("secondary", []),
            "long_tail": data.get("long_tail", []),
            "search_volume": data.get("search_volume", {}),
            "related_topics": data.get("related_topics", []),
            "competitor_keywords": data.get("competitor_keywords", []),
        }

    def _tool_save_draft(self, title: str, content: str) -> Dict[str, Any]:
        """
        MCP TOOL: save_draft
        ---------------------
        Saves a content draft to a temporary file during pipeline processing.
        The draft is NOT the final published article — it's a work-in-progress
        that the EditorAgent and SEOAgent will subsequently improve.

        In production this might:
          - Save to a CMS draft state (WordPress, Contentful, Ghost)
          - Write to a shared storage (S3, Google Drive)
          - Create a version in a version control system (like GitHub)

        The file is named using a URL-safe slug of the title so it's
        human-readable in the file system.
        """
        # Create a safe filename from the title
        safe_title = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
        safe_title = safe_title[:60]  # cap length
        filename = f"draft_{safe_title}_{datetime.now().strftime('%H%M%S')}.md"
        filepath = os.path.join(self._temp_dir, filename)

        try:
            with open(filepath, "w", encoding="utf-8") as fh:
                fh.write(f"# {title}\n\n")
                fh.write(f"*Draft saved: {datetime.now().isoformat()}*\n\n")
                fh.write(content)

            # Register the draft so it can be retrieved later
            draft_key = safe_title
            self._draft_registry[draft_key] = filepath

            return {
                "success": True,
                "title": title,
                "draft_key": draft_key,
                "filepath": filepath,
                "word_count": len(content.split()),
                "saved_at": datetime.now().isoformat(),
            }
        except IOError as exc:
            return {
                "success": False,
                "title": title,
                "error": f"Failed to save draft: {exc}",
                "filepath": None,
            }

    def _tool_publish_article(
        self, title: str, content: str, metadata: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        MCP TOOL: publish_article
        --------------------------
        Writes the final, editor-approved, SEO-optimized article to the
        published/ directory in two formats:

          1. <slug>.md  — The human-readable Markdown article with a full
                          YAML-style front matter block (used by static site
                          generators like Jekyll, Hugo, Gatsby).

          2. <slug>_meta.json — A JSON sidecar file with all publication
                                metadata (SEO tags, author info, timestamps,
                                pipeline run details).

        In production this would:
          - POST to a CMS API (WordPress REST, Contentful Management API)
          - Push to a Git repository (JAMstack workflow)
          - Trigger a CI/CD build pipeline
          - Send notifications to editors / social media bots

        The function also writes a publication summary to stdout so the
        operator knows the article is live.
        """
        # Generate a URL-safe slug for the filenames
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        slug = slug[:80]  # keep filenames reasonable

        timestamp = datetime.now()
        publish_time = timestamp.isoformat()
        date_str = timestamp.strftime("%Y-%m-%d")

        # ---- Build the Markdown file with front matter ----------------
        front_matter_lines = [
            "---",
            f'title: "{title}"',
            f"date: {publish_time}",
            f"slug: {slug}",
        ]

        # Add SEO metadata to front matter if provided
        if metadata.get("meta_title"):
            front_matter_lines.append(f'meta_title: "{metadata["meta_title"]}"')
        if metadata.get("meta_description"):
            front_matter_lines.append(
                f'meta_description: "{metadata["meta_description"]}"'
            )
        if metadata.get("tags"):
            tags_str = ", ".join(metadata["tags"])
            front_matter_lines.append(f"tags: [{tags_str}]")
        if metadata.get("keywords"):
            kw_str = ", ".join(metadata["keywords"])
            front_matter_lines.append(f"keywords: [{kw_str}]")

        front_matter_lines.extend(
            [
                "author: ContentForge Pipeline",
                f"pipeline_run: {date_str}",
                "status: published",
                "---",
                "",
            ]
        )

        full_content = "\n".join(front_matter_lines) + "\n" + content

        # ---- Write Markdown file --------------------------------------
        md_filename = f"{slug}.md"
        md_filepath = os.path.join(self._published_dir, md_filename)

        try:
            with open(md_filepath, "w", encoding="utf-8") as fh:
                fh.write(full_content)
        except IOError as exc:
            return {
                "success": False,
                "error": f"Failed to write article file: {exc}",
                "filepath": None,
            }

        # ---- Write JSON metadata sidecar ----------------------------
        meta_filename = f"{slug}_meta.json"
        meta_filepath = os.path.join(self._published_dir, meta_filename)

        meta_record = {
            "title": title,
            "slug": slug,
            "published_at": publish_time,
            "word_count": len(content.split()),
            "char_count": len(content),
            "md_file": md_filename,
            "meta_file": meta_filename,
            "seo": {
                "meta_title": metadata.get("meta_title", title),
                "meta_description": metadata.get("meta_description", ""),
                "keywords": metadata.get("keywords", []),
                "tags": metadata.get("tags", []),
                "primary_keyword": metadata.get("primary_keyword", ""),
            },
            "pipeline": {
                "system": "ContentForge",
                "agents": [
                    "ResearcherAgent",
                    "WriterAgent",
                    "EditorAgent",
                    "SEOAgent",
                    "PublisherAgent",
                ],
                "protocols": ["ACP", "MCP"],
                "run_date": date_str,
            },
            "readability": metadata.get("readability", {}),
            "edits_summary": metadata.get("edits_made", []),
        }

        try:
            with open(meta_filepath, "w", encoding="utf-8") as fh:
                json.dump(meta_record, fh, indent=2, ensure_ascii=False)
        except IOError as exc:
            # Non-fatal: article was written, metadata failed
            print(f"  [MCP SERVER] ⚠  Could not write metadata file: {exc}")

        return {
            "success": True,
            "title": title,
            "slug": slug,
            "published_at": publish_time,
            "word_count": len(content.split()),
            "md_filepath": md_filepath,
            "meta_filepath": meta_filepath,
            "url_slug": f"/articles/{slug}",
        }

    def _tool_check_readability(self, text: str) -> Dict[str, Any]:
        """
        MCP TOOL: check_readability
        ----------------------------
        Analyzes text and returns readability metrics.  The core metric is
        a simulated Flesch Reading Ease score, which rates text on a 0-100
        scale (higher = easier to read):

          90-100   Very Easy     (5th grade)
          70-90    Easy          (6th grade)
          60-70    Standard      (7th grade)
          50-60    Fairly Hard   (high school)
          30-50    Difficult     (college)
          0-30     Very Confusing (professional)

        The Flesch formula uses:
          score = 206.835 - 1.015 * (words/sentences) - 84.6 * (syllables/words)

        We approximate syllable count using vowel group counting, which is
        accurate enough for readability estimation without requiring a
        full pronunciation dictionary.

        In production this would use a library like textstat or call an
        NLP API (AWS Comprehend, Google Natural Language) for precise scoring.
        """
        if not text or not text.strip():
            return {
                "word_count": 0,
                "sentence_count": 0,
                "paragraph_count": 0,
                "avg_sentence_length": 0,
                "avg_word_length": 0,
                "syllable_count": 0,
                "flesch_score": 0,
                "reading_level": "N/A",
                "reading_time_minutes": 0,
                "issues": ["Text is empty"],
            }

        # ---- Basic counts ----------------------------------------
        # Remove Markdown formatting for accurate word/sentence counting
        clean_text = re.sub(r"[#*_`\[\]()]", "", text)
        clean_text = re.sub(r"\n{3,}", "\n\n", clean_text)

        # Word count (split on whitespace)
        words = [w for w in re.split(r"\s+", clean_text.strip()) if w]
        word_count = len(words)

        # Sentence count (split on . ! ?)
        sentences = [
            s.strip()
            for s in re.split(r"[.!?]+", clean_text)
            if s.strip() and len(s.strip()) > 3
        ]
        sentence_count = max(len(sentences), 1)  # avoid division by zero

        # Paragraph count (split on double newlines)
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        paragraph_count = len(paragraphs)

        # Average sentence length (words per sentence)
        avg_sentence_length = round(word_count / sentence_count, 1)

        # Average word length (characters per word)
        avg_word_length = round(
            sum(len(w.strip(".,!?;:\"'")) for w in words) / max(word_count, 1), 1
        )

        # ---- Syllable approximation ---------------------------------
        # Count vowel groups as a proxy for syllables
        def count_syllables(word: str) -> int:
            word = word.lower().strip(".,!?;:'\"")
            if not word:
                return 0
            vowels = re.findall(r"[aeiouy]+", word)
            count = len(vowels)
            # Adjust for silent 'e' at end
            if word.endswith("e") and count > 1:
                count -= 1
            return max(count, 1)

        syllable_count = sum(count_syllables(w) for w in words)
        avg_syllables_per_word = syllable_count / max(word_count, 1)

        # ---- Flesch Reading Ease Score ------------------------------
        flesch_score = round(
            206.835 - (1.015 * avg_sentence_length) - (84.6 * avg_syllables_per_word),
            1,
        )
        # Clamp to 0-100 range (formula can produce out-of-range values)
        flesch_score = max(0.0, min(100.0, flesch_score))

        # Map score to reading level label
        if flesch_score >= 90:
            reading_level = "Very Easy (5th grade)"
        elif flesch_score >= 70:
            reading_level = "Easy (6th grade)"
        elif flesch_score >= 60:
            reading_level = "Standard (7th grade)"
        elif flesch_score >= 50:
            reading_level = "Fairly Hard (high school)"
        elif flesch_score >= 30:
            reading_level = "Difficult (college)"
        else:
            reading_level = "Very Confusing (professional)"

        # Average adult reading speed is ~238 words per minute
        reading_time_minutes = round(word_count / 238, 1)

        # ---- Identify readability issues ----------------------------
        issues: List[str] = []
        if avg_sentence_length > 25:
            issues.append(
                f"Average sentence length is {avg_sentence_length} words "
                f"(target: under 20 for blog content)"
            )
        if avg_sentence_length < 8:
            issues.append(
                f"Sentences are very short ({avg_sentence_length} words avg) "
                f"— consider combining some for better flow"
            )
        if word_count < 300:
            issues.append(
                f"Article is short ({word_count} words) — "
                f"blog posts typically perform better at 800+ words"
            )
        if paragraph_count < 4:
            issues.append(
                "Too few paragraphs — consider breaking up the text "
                "for better scanability"
            )
        if flesch_score < 50:
            issues.append(
                f"Readability score {flesch_score} is quite low — "
                f"consider simplifying sentence structure and vocabulary"
            )

        if not issues:
            issues.append("No major readability issues detected.")

        return {
            "word_count": word_count,
            "sentence_count": sentence_count,
            "paragraph_count": paragraph_count,
            "avg_sentence_length": avg_sentence_length,
            "avg_word_length": avg_word_length,
            "syllable_count": syllable_count,
            "flesch_score": flesch_score,
            "reading_level": reading_level,
            "reading_time_minutes": reading_time_minutes,
            "issues": issues,
        }

    def _tool_grammar_check(self, text: str) -> Dict[str, Any]:
        """
        MCP TOOL: grammar_check
        ------------------------
        Performs a simulated grammar and style check using pattern-matching
        heuristics.  This simulates what a real grammar API would return
        (LanguageTool, Grammarly API, GPT-based grammar checking).

        The checker looks for:
          1. Common passive voice constructions
          2. Wordy / weak phrases that can be tightened
          3. Common spelling/word-choice errors
          4. Style issues (excessive adverbs, vague intensifiers, etc.)
          5. Structural issues (very long paragraphs, missing punctuation)

        Each issue includes:
          - type: category of the issue
          - text: the offending text snippet
          - suggestion: how to fix it
          - severity: "error", "warning", or "suggestion"

        In production this would call LanguageTool's REST API or a fine-tuned
        grammar model, returning standardized issue objects with character
        offsets for inline highlighting in an editor UI.
        """
        issues: List[Dict[str, str]] = []

        if not text or not text.strip():
            return {
                "issues_found": 0,
                "issues": [],
                "overall_quality": "N/A",
                "summary": "No text to check.",
            }

        # ---- Check 1: Passive voice constructions -------------------
        passive_patterns = [
            (r"\bwas\s+\w+ed\b", "was [verb]ed"),
            (r"\bwere\s+\w+ed\b", "were [verb]ed"),
            (r"\bis\s+being\s+\w+ed\b", "is being [verb]ed"),
            (r"\bhas\s+been\s+\w+ed\b", "has been [verb]ed"),
            (r"\bhave\s+been\s+\w+ed\b", "have been [verb]ed"),
        ]
        passive_count = 0
        for pattern, example in passive_patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            passive_count += len(matches)

        if passive_count > 3:
            issues.append(
                {
                    "type": "style",
                    "text": f"{passive_count} passive voice constructions found",
                    "suggestion": (
                        "Convert passive voice to active voice where possible "
                        "(e.g. 'was discovered by' → 'discovered')"
                    ),
                    "severity": "warning",
                }
            )

        # ---- Check 2: Wordy phrases that can be tightened -----------
        wordy_phrases = {
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
        }
        for phrase, replacement in wordy_phrases.items():
            if phrase in text.lower():
                issues.append(
                    {
                        "type": "wordiness",
                        "text": f'"{phrase}"',
                        "suggestion": f'Replace with "{replacement}"',
                        "severity": "suggestion",
                    }
                )

        # ---- Check 3: Weak / vague intensifiers ----------------------
        vague_intensifiers = [
            "very",
            "really",
            "quite",
            "rather",
            "somewhat",
            "fairly",
            "pretty",
            "extremely",
            "incredibly",
        ]
        for word in vague_intensifiers:
            pattern = r"\b" + word + r"\b"
            count = len(re.findall(pattern, text, re.IGNORECASE))
            if count > 2:
                issues.append(
                    {
                        "type": "style",
                        "text": f'"{word}" used {count} times',
                        "suggestion": (
                            f'Reduce usage of "{word}" — replace with '
                            f"stronger, more specific vocabulary"
                        ),
                        "severity": "suggestion",
                    }
                )

        # ---- Check 4: Common confused word pairs --------------------
        confused_pairs = [
            (r"\beffect\b", "Did you mean 'affect' (verb)? 'effect' is usually a noun"),
            (r"\bthen\b.{0,20}\bcompar", "Use 'than' for comparisons, 'then' for time"),
            (r"\bits'\b", "'its'' = 'it is'. Possessive 'its' has no apostrophe"),
            (r"\bthier\b", "Possible misspelling: 'thier' → 'their'"),
            (r"\brecieve\b", "Misspelling: 'recieve' → 'receive'"),
            (r"\boccured\b", "Misspelling: 'occured' → 'occurred'"),
            (r"\bseperate\b", "Misspelling: 'seperate' → 'separate'"),
        ]
        for pattern, suggestion in confused_pairs:
            if re.search(pattern, text, re.IGNORECASE):
                issues.append(
                    {
                        "type": "grammar",
                        "text": f"Matched pattern: {pattern}",
                        "suggestion": suggestion,
                        "severity": "error",
                    }
                )

        # ---- Check 5: Structural issues ------------------------------
        # Very long paragraphs (> 150 words)
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        for i, para in enumerate(paragraphs, 1):
            para_words = len(para.split())
            if para_words > 150:
                issues.append(
                    {
                        "type": "structure",
                        "text": f"Paragraph {i} has {para_words} words",
                        "suggestion": (
                            "Break this paragraph into 2-3 shorter paragraphs "
                            "(target: 60-100 words per paragraph for blog content)"
                        ),
                        "severity": "warning",
                    }
                )

        # ---- Check 6: Missing spaces after punctuation ---------------
        spacing_issues = re.findall(r"[.!?,;][A-Z]", text)
        if len(spacing_issues) > 2:
            issues.append(
                {
                    "type": "formatting",
                    "text": f"{len(spacing_issues)} possible missing spaces after punctuation",
                    "suggestion": "Ensure there is a space after each period, comma, etc.",
                    "severity": "warning",
                }
            )

        # ---- Overall quality assessment ----------------------------
        error_count = sum(1 for i in issues if i["severity"] == "error")
        warning_count = sum(1 for i in issues if i["severity"] == "warning")

        if error_count == 0 and warning_count == 0:
            overall_quality = "Excellent"
        elif error_count == 0 and warning_count <= 2:
            overall_quality = "Good"
        elif error_count <= 1 and warning_count <= 3:
            overall_quality = "Acceptable"
        elif error_count <= 2:
            overall_quality = "Needs Improvement"
        else:
            overall_quality = "Significant Issues"

        return {
            "issues_found": len(issues),
            "error_count": error_count,
            "warning_count": warning_count,
            "suggestion_count": sum(1 for i in issues if i["severity"] == "suggestion"),
            "issues": issues,
            "overall_quality": overall_quality,
            "summary": (
                f"{len(issues)} issue(s) found: "
                f"{error_count} error(s), "
                f"{warning_count} warning(s), "
                f"{len(issues) - error_count - warning_count} suggestion(s). "
                f"Overall quality: {overall_quality}."
            ),
        }

    # ==================================================================
    # UTILITY / INTROSPECTION
    # ==================================================================

    def get_tool_stats(self) -> Dict[str, int]:
        """Return a dict of tool_name → call_count for usage analytics."""
        return {name: tool.call_count for name, tool in self._tools.items()}

    def print_usage_stats(self) -> None:
        """Print a tool usage summary after the pipeline completes."""
        print("\n  ── MCP SERVER USAGE STATISTICS ──────────────────────────────")
        stats = self.get_tool_stats()
        total_calls = sum(stats.values())
        for tool_name, call_count in sorted(
            stats.items(), key=lambda x: x[1], reverse=True
        ):
            bar = "█" * call_count
            print(
                f"  {tool_name:<25} : {bar} ({call_count} call{'s' if call_count != 1 else ''})"
            )
        print(f"\n  Total tool calls: {total_calls}")
        print("  ─────────────────────────────────────────────────────────────")

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"MCPServer(tools={list(self._tools.keys())}, "
            f"data_dir={os.path.relpath(self._data_dir)!r})"
        )
