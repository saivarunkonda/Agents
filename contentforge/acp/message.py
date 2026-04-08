# =============================================================================
# acp/message.py  —  ACP Message Envelope
# =============================================================================
#
# WHY THIS FILE EXISTS
# --------------------
# In any multi-agent system the first question is: "How do agents talk to each
# other?"  Without a standard, every agent pair would invent its own format,
# making the system impossible to observe, debug, or extend.
#
# ACP (Agent Communication Protocol) solves this with a single, mandatory
# message envelope — the ACPMessage.  EVERY communication between agents,
# no matter how simple, must be wrapped in this envelope.
#
# WHAT THE ENVELOPE GUARANTEES
# -----------------------------
#  1. Traceability   — every message has a unique ID and a timestamp, so you
#                      can replay the entire conversation history later.
#  2. Routing        — sender_id and receiver_id tell the bus where to deliver
#                      the message without the bus needing to understand the
#                      payload at all.
#  3. Correlation    — correlation_id links a RESPONSE back to the REQUEST
#                      that caused it, enabling request/reply patterns.
#  4. Typing         — message_type and content_type let agents (and the bus)
#                      make routing decisions without parsing the payload.
#  5. Extensibility  — the metadata dict is a free-form escape hatch for
#                      protocol extensions without breaking existing agents.
#
# ANALOGY
# -------
# Think of ACPMessage like an envelope in the postal system:
#   • The envelope (fields) carries addressing and handling instructions.
#   • The letter inside (payload) is the actual content.
#   • The post office (message bus) reads ONLY the envelope — it never opens
#     the letter to decide where to deliver it.
# =============================================================================

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# ACPMessageType — what kind of interaction is this?
# ---------------------------------------------------------------------------


class ACPMessageType(Enum):
    """
    ACP standardised message types.

    These types mirror common patterns seen in distributed systems and
    messaging protocols (cf. AMQP, FIPA-ACL, JSON-RPC).  Keeping them
    as an enum (rather than free-form strings) means:
      - Typos are caught at startup, not at runtime deep in the pipeline.
      - IDEs can offer auto-complete, reducing integration errors.
      - The bus can validate messages without touching the payload.

    Usage guidance
    --------------
    REQUEST   →  "I need you to do something and report back."
                  Always set receiver_id to the target agent's ID.
                  Set correlation_id = None (this IS the origin message).

    RESPONSE  →  "Here is the result of the work you requested."
                  Set receiver_id to whoever sent the original REQUEST.
                  Set correlation_id = the REQUEST's message_id so the
                  recipient can match this response to its pending request.

    BROADCAST →  "I am announcing something to everyone."
                  Set receiver_id = None.
                  The bus will deliver this to ALL registered subscribers.
                  Used for pipeline-wide events (e.g. "article published").

    ERROR     →  "Something went wrong while processing your request."
                  Set receiver_id to the original REQUEST sender.
                  Set correlation_id = the failed REQUEST's message_id.
                  The payload should describe what went wrong.

    ACK       →  "I received your message and will begin processing."
                  Lightweight receipt — no payload required.
                  Useful in truly async pipelines to prevent timeouts.
    """

    REQUEST = "REQUEST"
    RESPONSE = "RESPONSE"
    BROADCAST = "BROADCAST"
    ERROR = "ERROR"
    ACK = "ACK"


# ---------------------------------------------------------------------------
# ACPContentType — what format is the payload in?
# ---------------------------------------------------------------------------


class ACPContentType(Enum):
    """
    ACP payload content types (MIME-style).

    Declaring the content type on the envelope means:
      - The receiving agent knows how to deserialise the payload without
        having to sniff it.
      - Middleware (logging, encryption, compression) can act on the
        payload appropriately.
      - Humans reading the message log immediately understand what they
        are looking at.

    TEXT      →  Raw string — simple status messages, log lines, etc.
    JSON      →  Python dict/list serialised to JSON.  The most common
                  type for structured inter-agent data exchange.
    MARKDOWN  →  Formatted text — used when the payload is intended for
                  human consumption or when passing article drafts between
                  writing/editing agents.
    """

    TEXT = "text/plain"
    JSON = "application/json"
    MARKDOWN = "text/markdown"


# ---------------------------------------------------------------------------
# ACPMessage — the universal envelope
# ---------------------------------------------------------------------------


@dataclass
class ACPMessage:
    """
    ACP Message Envelope — the standard unit of communication between agents.

    ALL agent communication in ContentForge MUST use this envelope.
    Agents that bypass the envelope and communicate via direct method calls
    violate the ACP contract and break the pipeline's observability guarantees.

    Fields
    ------
    message_id      Globally unique identifier for this specific message.
                    Generated automatically using UUID4 (first 8 hex chars).
                    Used by the bus to deduplicate and by agents to build
                    correlation chains.

    sender_id       The agent_id of the agent that created this message.
                    Agents set this in their send() / broadcast() helpers
                    so the bus always knows who sent what.

    receiver_id     The agent_id of the intended recipient.
                    If None, the message is a BROADCAST delivered to all
                    subscribers currently registered with the bus.

    message_type    ACPMessageType enum value — the interaction pattern
                    (REQUEST, RESPONSE, BROADCAST, ERROR, ACK).

    content_type    ACPContentType enum value — the MIME-style type of the
                    payload, so recipients know how to deserialise it.

    payload         The actual data being exchanged.  This is a plain Python
                    object (dict, str, list, None, …); agents are responsible
                    for agreeing on the payload schema for each interaction.
                    The envelope deliberately does NOT enforce payload schema
                    — that is the agents' concern, not the protocol's.

    correlation_id  Links this message to a previously sent message.
                    Convention:
                      • REQUEST  → correlation_id is None  (origin)
                      • RESPONSE → correlation_id = sender's REQUEST message_id
                      • ERROR    → correlation_id = the failing REQUEST's id
                    This field is what lets you reconstruct a full
                    request-response chain from the message history.

    timestamp       ISO-8601 datetime string captured at the moment the
                    ACPMessage object is instantiated.  Tells you exactly
                    when the message was CREATED (not delivered).

    metadata        Free-form key/value bag for out-of-band information.
                    Examples: {"priority": "high"}, {"retry_count": 2},
                              {"pipeline_run_id": "abc123"}.
                    Agents should not rely on metadata for core logic —
                    it exists for observability and protocol extensions.

    Example: Researcher → Writer request
    -------------------------------------
    ACPMessage(
        message_id    = "a3f9b12c",
        sender_id     = "researcher",
        receiver_id   = "writer",
        message_type  = ACPMessageType.RESPONSE,
        content_type  = ACPContentType.JSON,
        payload       = {"topic": "AI", "facts": [...], ...},
        correlation_id= "7e2d4a01",   # the original REQUEST's id
        timestamp     = "2024-01-15T10:23:45.123456",
        metadata      = {"pipeline_stage": 1}
    )
    """

    # ---- identity & routing ------------------------------------------------
    message_id: str = field(
        default_factory=lambda: str(uuid.uuid4())[:8],
        metadata={"description": "Unique 8-char message identifier"},
    )
    sender_id: str = field(
        default="", metadata={"description": "agent_id of the sending agent"}
    )
    receiver_id: Optional[str] = field(
        default=None,
        metadata={"description": "agent_id of recipient; None = broadcast"},
    )

    # ---- interaction typing ------------------------------------------------
    message_type: ACPMessageType = field(
        default=ACPMessageType.REQUEST,
        metadata={"description": "Interaction pattern (REQUEST/RESPONSE/etc.)"},
    )
    content_type: ACPContentType = field(
        default=ACPContentType.JSON,
        metadata={"description": "MIME-style payload format"},
    )

    # ---- payload -----------------------------------------------------------
    payload: Any = field(
        default=None,
        metadata={"description": "Actual data; schema agreed between agents"},
    )

    # ---- correlation & timing ----------------------------------------------
    correlation_id: Optional[str] = field(
        default=None, metadata={"description": "message_id of the triggering message"}
    )
    timestamp: str = field(
        default_factory=lambda: datetime.now().isoformat(),
        metadata={"description": "ISO-8601 creation time"},
    )

    # ---- extensibility -----------------------------------------------------
    metadata: Dict[str, Any] = field(
        default_factory=dict,
        metadata={"description": "Free-form protocol extension data"},
    )

    # -----------------------------------------------------------------------
    # Helper / display methods
    # -----------------------------------------------------------------------

    def summary(self) -> str:
        """
        Return a compact one-line summary suitable for log output.

        Format:
          [MSG:<id>] <type> | <sender> -> <receiver|BROADCAST> | <content_type>
        """
        receiver_label = self.receiver_id if self.receiver_id else "BROADCAST"
        corr_label = f" (corr:{self.correlation_id})" if self.correlation_id else ""
        return (
            f"[MSG:{self.message_id}] "
            f"{self.message_type.value:<10} | "
            f"{self.sender_id:<20} -> {receiver_label:<20} | "
            f"{self.content_type.value}"
            f"{corr_label}"
        )

    def to_dict(self) -> Dict[str, Any]:
        """
        Serialise the envelope to a plain dictionary.

        Useful for logging, persistence, and debugging.  Note that the
        payload is included as-is — callers must serialise it separately
        if needed (e.g. for JSON storage).
        """
        return {
            "message_id": self.message_id,
            "sender_id": self.sender_id,
            "receiver_id": self.receiver_id,
            "message_type": self.message_type.value,
            "content_type": self.content_type.value,
            "payload": self.payload,
            "correlation_id": self.correlation_id,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"ACPMessage("
            f"id={self.message_id!r}, "
            f"type={self.message_type.value}, "
            f"from={self.sender_id!r}, "
            f"to={self.receiver_id!r}, "
            f"corr={self.correlation_id!r}"
            f")"
        )
