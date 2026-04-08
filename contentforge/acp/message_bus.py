# =============================================================================
# acp/message_bus.py  —  ACP Message Bus
# =============================================================================
#
# WHY THIS FILE EXISTS
# --------------------
# Agents must be able to communicate without knowing each other's internal
# implementation details.  If AgentA called AgentB.process() directly, the
# two would be tightly coupled — you couldn't swap out AgentB, add a new
# agent to the pipeline, or replay a failed run without touching AgentA's
# code.
#
# ACP solves this with the MESSAGE BUS pattern (also called pub/sub or
# event bus).  The bus is the ONLY channel through which agents interact:
#
#   Agent publishes  →  Bus routes  →  Subscriber receives
#
# No agent ever holds a reference to another agent.  Every agent only
# holds a reference to the bus.  This is sometimes called the
# "mediator pattern" — the bus mediates all conversations.
#
# WHAT THE BUS GUARANTEES
# -----------------------
#  1. Decoupling    — Agents don't need to know each other exist.
#  2. Observability — Every message passes through one place, so the bus
#                     can log the complete conversation history.
#  3. Extensibility — Adding a new agent = subscribe it to the bus.
#                     No existing agent code changes required.
#  4. Replay        — The full message history can be replayed for
#                     debugging, auditing, or testing.
#
# ACP vs DIRECT CALLS (why the bus matters)
# ------------------------------------------
# Direct call model:   ResearcherAgent → WriterAgent.receive(data)
#   Problem: ResearcherAgent must import WriterAgent. Tight coupling.
#            Can't add a monitoring agent without editing Researcher.
#
# ACP bus model:       ResearcherAgent → bus.publish(msg to "writer")
#   Benefit: ResearcherAgent only knows the bus and the string "writer".
#            You can add a MonitorAgent that also subscribes — zero
#            changes to ResearcherAgent required.
#
# IMPLEMENTATION NOTE
# -------------------
# ContentForge runs synchronously (no asyncio) to keep the educational
# focus on the PROTOCOL rather than Python's concurrency model.  In a
# production system you would replace the direct handler() calls here
# with async task queues (Celery, RabbitMQ, Redis Streams, etc.) and the
# fundamental ACP contracts would remain identical.
# =============================================================================

from typing import Callable, Dict, List, Optional

from acp.message import ACPMessage, ACPMessageType


class ACPMessageBus:
    """
    ACP Message Bus — central hub for all inter-agent communication.

    Lifecycle
    ---------
    1. BOOTSTRAP  : Create one shared bus instance for the pipeline run.
    2. SUBSCRIBE  : Each agent calls bus.subscribe(agent_id, handler) once
                    at startup.  The handler is any callable that accepts a
                    single ACPMessage argument.
    3. PUBLISH    : Agents call bus.publish(message) to send a message.
                    The bus inspects receiver_id and routes accordingly:
                      - receiver_id is set  → deliver to that agent only
                      - receiver_id is None → deliver to ALL subscribers
                                              (BROADCAST)
    4. HISTORY    : After the pipeline finishes, call print_message_log()
                    to see every message that flowed through the bus.

    Thread-safety note
    ------------------
    This implementation is single-threaded.  publish() is synchronous:
    it calls the handler immediately and blocks until the handler returns.
    This makes the execution order deterministic and easy to follow, which
    is ideal for educational purposes.  Production systems would push
    messages onto a thread-safe queue instead.
    """

    def __init__(self) -> None:
        # ----------------------------------------------------------------
        # _subscribers: maps agent_id (str) -> handler callable
        #
        # Each agent registers exactly one handler.  The handler is the
        # agent's "inbox" — when the bus delivers a message to agent X,
        # it simply calls _subscribers["X"](message).
        # ----------------------------------------------------------------
        self._subscribers: Dict[str, Callable[[ACPMessage], None]] = {}

        # ----------------------------------------------------------------
        # _message_history: append-only log of every message published.
        #
        # This is one of ACP's most valuable features: because ALL
        # communication flows through the bus, the bus automatically
        # accumulates a complete, ordered audit trail of the pipeline run.
        # You can reconstruct exactly what every agent knew and when.
        # ----------------------------------------------------------------
        self._message_history: List[ACPMessage] = []

        # ----------------------------------------------------------------
        # _message_counter: simple sequence number for human-readable logs.
        # Not part of the ACP spec — purely for educational output.
        # ----------------------------------------------------------------
        self._message_counter: int = 0

    # ------------------------------------------------------------------
    # subscribe()
    # ------------------------------------------------------------------

    def subscribe(self, agent_id: str, handler: Callable[[ACPMessage], None]) -> None:
        """
        Register an agent as a subscriber on the bus.

        ACP CONCEPT: Subscription
        -------------------------
        In pub/sub systems, a subscriber registers its interest in receiving
        messages.  In ACP, every agent subscribes using its agent_id as the
        key.  This means any other agent can address a message to this agent
        simply by setting receiver_id = agent_id — no imports, no direct
        references.

        Parameters
        ----------
        agent_id : str
            The unique identifier for the agent.  This becomes the agent's
            "address" on the bus.  Must match the agent_id used in any
            ACPMessage.receiver_id that should reach this agent.

        handler : callable
            A function (or bound method) with signature:
                handler(message: ACPMessage) -> None
            The bus calls this whenever a message is routed to agent_id.
        """
        if agent_id in self._subscribers:
            # Re-registration is allowed (e.g. agent restart) but we warn
            # so developers notice accidental double-registration.
            print(
                f"  [BUS] ⚠  Agent '{agent_id}' is re-registering on the bus. "
                f"Previous handler replaced."
            )

        self._subscribers[agent_id] = handler
        print(
            f"  [BUS] ✓  Agent '{agent_id}' subscribed to the ACP Message Bus. "
            f"({len(self._subscribers)} total subscribers)"
        )

    # ------------------------------------------------------------------
    # publish()
    # ------------------------------------------------------------------

    def publish(self, message: ACPMessage) -> None:
        """
        Publish a message onto the bus.

        ACP CONCEPT: Publishing & Routing
        ----------------------------------
        Publishing is the ONLY way agents communicate in ACP.  When an
        agent calls bus.publish(msg), the bus:

          Step 1 — RECORD: Append the message to the history log.
                   This happens unconditionally — even if routing fails,
                   the attempt is recorded for debugging.

          Step 2 — VALIDATE: Check the message has required fields.

          Step 3 — ROUTE:
            a) If receiver_id is set → unicast (point-to-point delivery).
               Look up the subscriber and call its handler.
               If the receiver is not registered, log an error but do NOT
               raise an exception — the sending agent shouldn't crash
               because its target is unavailable.  This mirrors how real
               message brokers handle unroutable messages (dead-letter
               queues).

            b) If receiver_id is None → multicast (broadcast).
               Deliver to EVERY registered subscriber EXCEPT the sender
               (an agent broadcasting to itself is almost always a bug).

        Parameters
        ----------
        message : ACPMessage
            The fully constructed ACP envelope to route.
        """
        self._message_counter += 1

        # Step 1 — Record in history (always, regardless of routing outcome)
        self._message_history.append(message)

        # Step 2 — Validate
        if not message.sender_id:
            print(
                "  [BUS] ✗  Message rejected: sender_id is empty. "
                "ACP requires all messages to identify their sender."
            )
            return

        # Log the routing decision (educational — shows ACP in action)
        self._log_routing(message)

        # Step 3a — UNICAST: deliver to a specific agent
        if message.receiver_id is not None:
            self._deliver_unicast(message)

        # Step 3b — BROADCAST: deliver to all subscribers except sender
        else:
            self._deliver_broadcast(message)

    # ------------------------------------------------------------------
    # Internal routing helpers
    # ------------------------------------------------------------------

    def _deliver_unicast(self, message: ACPMessage) -> None:
        """
        Deliver a message to a single named recipient.

        ACP CONCEPT: Point-to-Point Delivery
        --------------------------------------
        Unicast delivery is used for REQUEST/RESPONSE pairs — one agent
        explicitly addressing another.  The receiver_id acts like a postal
        address; the bus looks it up in the subscriber registry and calls
        the handler directly.

        If the recipient is not registered, the message is dropped with a
        warning.  In production ACP systems this would trigger a
        dead-letter mechanism or a retry policy.
        """
        target_id = message.receiver_id

        if target_id not in self._subscribers:
            print(
                f"  [BUS] ✗  UNROUTABLE: No agent '{target_id}' registered. "
                f"Message {message.message_id} dropped. "
                f"(Registered agents: {list(self._subscribers.keys())})"
            )
            return

        # Deliver — synchronous call in this implementation
        handler = self._subscribers[target_id]
        handler(message)

    def _deliver_broadcast(self, message: ACPMessage) -> None:
        """
        Deliver a message to ALL registered agents (except the sender).

        ACP CONCEPT: Broadcast / Fan-Out
        ---------------------------------
        Broadcast is used for pipeline-wide announcements — "I finished",
        "error occurred", "article published", etc.  No receiver is
        specified (receiver_id = None) so the bus fans the message out to
        every subscriber.

        We skip the sender itself to avoid infinite self-notification loops
        that would be a common bug in naive pub/sub implementations.
        """
        recipients = [aid for aid in self._subscribers if aid != message.sender_id]

        if not recipients:
            print(
                f"  [BUS] ℹ  BROADCAST from '{message.sender_id}' — "
                f"no other agents registered to receive it."
            )
            return

        print(
            f"  [BUS] 📢 BROADCAST from '{message.sender_id}' — "
            f"delivering to {len(recipients)} agent(s): {recipients}"
        )

        for agent_id in recipients:
            handler = self._subscribers[agent_id]
            handler(message)

    def _log_routing(self, message: ACPMessage) -> None:
        """
        Print a formatted routing log entry for educational visibility.

        This output deliberately mimics the kind of trace you would see in
        a real message broker's admin console (e.g. RabbitMQ management UI,
        AWS SQS CloudWatch logs) so that readers understand what ACP's
        observability looks like in practice.
        """
        routing_mode = "UNICAST  " if message.receiver_id else "BROADCAST"
        receiver_label = message.receiver_id if message.receiver_id else "ALL"

        print(
            f"\n  ┌─ [BUS #{self._message_counter:03d}] ─────────────────────────────────────────"
        )
        print(f"  │  Mode    : {routing_mode}")
        print(f"  │  MsgID   : {message.message_id}")
        print(f"  │  Type    : {message.message_type.value}")
        print(f"  │  From    : {message.sender_id}")
        print(f"  │  To      : {receiver_label}")
        print(f"  │  Content : {message.content_type.value}")
        if message.correlation_id:
            print(f"  │  Corr.ID : {message.correlation_id}")
        print(f"  └──────────────────────────────────────────────────────────")

    # ------------------------------------------------------------------
    # Introspection helpers
    # ------------------------------------------------------------------

    def get_history(self) -> List[ACPMessage]:
        """
        Return the full, ordered list of all messages published to this bus.

        ACP CONCEPT: Observability via History
        ----------------------------------------
        Because every message passes through the bus, the history is a
        complete, causal record of everything that happened in the pipeline.
        You can:
          - Audit: who said what to whom and when?
          - Debug: at what point did a message go missing or get corrupted?
          - Replay: re-run the pipeline from any checkpoint.
          - Test: assert that specific messages were exchanged.

        Returns a copy of the list so callers cannot accidentally mutate
        the bus's internal history.
        """
        return list(self._message_history)

    def get_history_for_agent(self, agent_id: str) -> List[ACPMessage]:
        """
        Filter the message history to only messages involving a given agent
        (either as sender or receiver, including broadcasts).

        Useful for per-agent debugging: "show me everything the WriterAgent
        sent or received during this pipeline run."
        """
        return [
            msg
            for msg in self._message_history
            if msg.sender_id == agent_id
            or msg.receiver_id == agent_id
            or msg.receiver_id is None  # broadcasts go to everyone
        ]

    def get_conversation_chain(self, root_message_id: str) -> List[ACPMessage]:
        """
        Follow correlation_id links to reconstruct a full request/response
        chain starting from a given root message.

        ACP CONCEPT: Correlation Chains
        --------------------------------
        Every RESPONSE carries a correlation_id pointing to the REQUEST
        that triggered it.  By walking these links you can reconstruct the
        complete conversational thread — "this publisher message was the
        result of this SEO message, which came from this editor message,
        which came from this writer message, which came from this initial
        research request."

        This is the ACP equivalent of distributed tracing (cf. OpenTelemetry
        trace IDs / span IDs).
        """
        chain: List[ACPMessage] = []
        seen_ids: set = set()

        # Add the root message if it exists
        root_msgs = [
            m for m in self._message_history if m.message_id == root_message_id
        ]
        if not root_msgs:
            return chain

        chain.append(root_msgs[0])
        seen_ids.add(root_message_id)

        # Iteratively find all messages that correlate to any message already
        # in the chain (breadth-first traversal of the correlation graph)
        changed = True
        while changed:
            changed = False
            for msg in self._message_history:
                if msg.message_id not in seen_ids and msg.correlation_id in seen_ids:
                    chain.append(msg)
                    seen_ids.add(msg.message_id)
                    changed = True

        # Sort by timestamp so the chain reads chronologically
        chain.sort(key=lambda m: m.timestamp)
        return chain

    def list_subscribers(self) -> List[str]:
        """Return the agent_ids of all currently registered subscribers."""
        return list(self._subscribers.keys())

    def get_stats(self) -> Dict[str, int]:
        """
        Return a summary statistics dictionary for this bus instance.

        Keys
        ----
        total_messages      : total messages published (including failed)
        unicast_messages    : messages with a specific receiver_id
        broadcast_messages  : messages with receiver_id == None
        request_count       : messages of type REQUEST
        response_count      : messages of type RESPONSE
        error_count         : messages of type ERROR
        subscriber_count    : number of registered agents
        """
        history = self._message_history
        return {
            "total_messages": len(history),
            "unicast_messages": sum(1 for m in history if m.receiver_id is not None),
            "broadcast_messages": sum(1 for m in history if m.receiver_id is None),
            "request_count": sum(
                1 for m in history if m.message_type == ACPMessageType.REQUEST
            ),
            "response_count": sum(
                1 for m in history if m.message_type == ACPMessageType.RESPONSE
            ),
            "error_count": sum(
                1 for m in history if m.message_type == ACPMessageType.ERROR
            ),
            "subscriber_count": len(self._subscribers),
        }

    # ------------------------------------------------------------------
    # Educational output
    # ------------------------------------------------------------------

    def print_message_log(self) -> None:
        """
        Print a formatted, human-readable log of all messages that flowed
        through this bus during the pipeline run.

        ACP CONCEPT: Full Pipeline Observability
        -----------------------------------------
        One of ACP's core promises is that the entire communication history
        is observable from a single vantage point — the bus.  This method
        materialises that promise.

        Reading this log you should be able to answer:
          - How many messages were exchanged?
          - In what order did agents communicate?
          - Which messages were requests vs responses?
          - How does data flow from ResearcherAgent to PublisherAgent?

        This kind of visibility is invaluable for debugging multi-agent
        systems where emergent behaviour (unexpected message sequences) is
        a common failure mode.
        """
        print("\n")
        print("=" * 70)
        print("  ACP MESSAGE BUS — COMPLETE PIPELINE MESSAGE HISTORY")
        print("=" * 70)
        print(
            "  This log shows EVERY message exchanged between agents via ACP.\n"
            "  In a real system this would be your message broker's audit log.\n"
            "  Each entry shows: sequence#, type, sender → receiver, msg-id\n"
        )

        if not self._message_history:
            print("  (no messages recorded)")
            print("=" * 70)
            return

        for i, msg in enumerate(self._message_history, start=1):
            receiver_label = msg.receiver_id if msg.receiver_id else "BROADCAST→ALL"
            corr_str = f"  ↳ corr:{msg.correlation_id}" if msg.correlation_id else ""

            # Use different symbols for different message types
            type_symbols = {
                "REQUEST": "→",
                "RESPONSE": "←",
                "BROADCAST": "📢",
                "ERROR": "✗",
                "ACK": "✓",
            }
            symbol = type_symbols.get(msg.message_type.value, "?")

            print(
                f"  [{i:02d}] {symbol} {msg.message_type.value:<10} "
                f"| {msg.sender_id:<20} → {receiver_label:<20} "
                f"| id:{msg.message_id}{corr_str}"
            )

        print()
        print("  ── STATISTICS ──────────────────────────────────────────────")
        stats = self.get_stats()
        print(f"  Total messages   : {stats['total_messages']}")
        print(f"  Unicast          : {stats['unicast_messages']}")
        print(f"  Broadcast        : {stats['broadcast_messages']}")
        print(f"  Requests         : {stats['request_count']}")
        print(f"  Responses        : {stats['response_count']}")
        print(f"  Errors           : {stats['error_count']}")
        print(f"  Active agents    : {stats['subscriber_count']}")
        print("=" * 70)
