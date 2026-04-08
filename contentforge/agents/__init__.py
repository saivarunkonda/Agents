# agents/__init__.py
# Agents package for ContentForge Multi-Agent Content Creation Pipeline
#
# This package contains all ACP-compliant agents that participate in the
# content creation pipeline. Each agent:
#   - Inherits from BaseACPAgent
#   - Communicates ONLY through the ACP Message Bus (never directly)
#   - Uses MCP tools for external resource access
#   - Processes one stage of the content pipeline
#
# Pipeline order:
#   ResearcherAgent -> WriterAgent -> EditorAgent -> SEOAgent -> PublisherAgent

from agents.base_agent import BaseACPAgent
from agents.editor_agent import EditorAgent
from agents.publisher_agent import PublisherAgent
from agents.researcher_agent import ResearcherAgent
from agents.seo_agent import SEOAgent
from agents.writer_agent import WriterAgent

__all__ = [
    "BaseACPAgent",
    "ResearcherAgent",
    "WriterAgent",
    "EditorAgent",
    "SEOAgent",
    "PublisherAgent",
]
