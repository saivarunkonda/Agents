# =============================================================================
# ContentForge - MCP (Model Context Protocol) Package
# =============================================================================
#
# MCP defines how agents access EXTERNAL RESOURCES and TOOLS.
#
# The core MCP idea:
#   - Agents need to reach beyond their own logic (search data, save files, etc.)
#   - Rather than hard-coding these calls, MCP exposes them as NAMED TOOLS
#     on a server that any agent can call through a standard client interface.
#   - This separates WHAT an agent wants to do from HOW it's done.
#
# MCP Components in ContentForge:
#   - MCPServer  : Hosts the tool implementations (search, style guides, SEO, etc.)
#   - MCPClient  : Thin wrapper used by agents to invoke tools on the server
#
# Analogy: If ACP is the agents' email system (how they talk to each other),
#          MCP is their shared toolkit (the software on every agent's desktop).
# =============================================================================

from mcp.mcp_client import MCPClient
from mcp.mcp_server import MCPServer

__all__ = ["MCPServer", "MCPClient"]
