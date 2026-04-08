# AG-UI Protocol Package
# This package implements the AG-UI (Agent-User Interface) protocol for ShopStream.
#
# AG-UI is an open protocol that standardizes how AI agents communicate with
# frontend UIs in real time. It defines a set of event types that flow over
# Server-Sent Events (SSE), giving the frontend a structured window into the
# agent's internal state: when it starts/stops, what tools it calls, what text
# it produces, and how the shared state evolves.
#
# Key concepts implemented here:
#   - AGUIEventType  : Enum of all standard AG-UI event type identifiers
#   - AGUIEvent      : Dataclass representing a single protocol event
#   - format_sse()   : Serialises an AGUIEvent into the SSE wire format

from .agui_protocol import AGUIEvent, AGUIEventType, format_sse

__all__ = ["AGUIEventType", "AGUIEvent", "format_sse"]
