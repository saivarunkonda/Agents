# =============================================================================
# ACP (Agent Communication Protocol) Package
# =============================================================================
#
# ACP defines the standard for how autonomous agents communicate with each other.
# It is built around three core concepts:
#
#   1. MESSAGE ENVELOPE  (acp.message)
#      Every piece of inter-agent communication is wrapped in an ACPMessage.
#      The envelope carries metadata (who sent it, who should receive it, when,
#      what type of interaction it is) so that any observer — including the bus
#      itself — can understand the communication without parsing the payload.
#
#   2. MESSAGE BUS  (acp.message_bus)
#      Agents never talk directly to each other.  Instead they publish messages
#      to a shared bus and subscribe to messages addressed to them.  This
#      decoupling is the cornerstone of ACP's interoperability guarantee: you
#      can add, remove, or replace any agent without changing any other agent.
#
#   3. AGENT REGISTRY  (acp.agent_registry)
#      A lightweight service-discovery layer.  Agents announce their
#      capabilities (input schema, output schema, current status) when they
#      start up, and any other agent — or a human operator — can query the
#      registry to understand what the pipeline looks like at runtime.
#
# Typical usage
# -------------
#   from acp import ACPMessage, ACPMessageType, ACPContentType
#   from acp import ACPMessageBus
#   from acp import ACPAgentRegistry, ACPAgentInfo
#
# =============================================================================

from acp.agent_registry import ACPAgentInfo, ACPAgentRegistry
from acp.message import ACPContentType, ACPMessage, ACPMessageType
from acp.message_bus import ACPMessageBus

__all__ = [
    # Message envelope and enums
    "ACPMessage",
    "ACPMessageType",
    "ACPContentType",
    # Central message bus
    "ACPMessageBus",
    # Agent registry / discovery
    "ACPAgentRegistry",
    "ACPAgentInfo",
]
