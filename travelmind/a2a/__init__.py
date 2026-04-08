"""
A2A (Agent2Agent Protocol) Package
====================================
Implements the Agent-to-Agent communication protocol for TravelMind.

Provides:
  - AgentCard       : Identity and capability descriptor for each agent
  - A2ATask         : Structured task message sent between agents
  - A2AResponse     : Structured response returned by an agent
  - A2ARegistry     : Discovery registry for finding agents by capability
  - A2AClient       : Client that handles discovery → auth → task dispatch
  - auth utilities  : Token generation and verification helpers
"""

from .a2a_protocol import A2AClient, A2ARegistry, A2AResponse, A2ATask
from .agent_card import AgentCard
from .auth import generate_token, verify_token

__all__ = [
    "AgentCard",
    "A2ATask",
    "A2AResponse",
    "A2ARegistry",
    "A2AClient",
    "generate_token",
    "verify_token",
]
