# backend/mcp/__init__.py
# MCP (Model Context Protocol) package
# This package implements the MCP server and client for ShopStream's shopping tools.

from .mcp_client import MCPClient
from .mcp_server import MCPServer

__all__ = ["MCPServer", "MCPClient"]
