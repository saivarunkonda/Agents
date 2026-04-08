# =============================================================================
# agents/base_agent.py  —  Base ACP Agent
# =============================================================================
#
# WHY THIS FILE EXISTS
# --------------------
# All five ContentForge agents (Researcher, Writer, Editor, SEO, Publisher)
# share a common set of responsibilities:
#   - Subscribe to the ACP Message Bus at startup
#   - Receive and dispatch incoming ACPMessages
#   - Send messages back through the bus (never directly to other agents)
#   - Use the MCPClient for external tool access
#   - Update their status in the ACP Agent Registry
#   - Log their activity with consistent [ACP] prefixes
#
# Rather than duplicating this boilerplate in every agent, we capture it
# here in BaseACPAgent.  Concrete agents inherit from this class and only
# need to implement handle_message() — the business logic specific to their
# role in the pipeline.
#
# ACP DESIGN PRINCIPLE: Uniform Agent Contract
# ----------------------------------------------
# Every ACP agent, regardless of its role, must honour the same contract:
#   1. It communicates ONLY through the bus (no direct agent-to-agent calls)
#   2. It identifies itself in every message it sends (sender_id)
#   3. It can receive any ACPMessage, but only acts on ones it understands
#   4. It reports errors via the bus (ACPMessageType.ERROR), not exceptions
#   5. It declares its input/output schemas when registering
#
# Enforcing this contract in the base class means we can guarantee it
# across all agents without relying on each implementer to remember.
#
# ACP CONCEPT: Agent as a "Mailbox"
# -----------------------------------
# Think of each agent as having a private mailbox:
#   - The bus is the postal service
#   - subscribe() gives the agent its mailbox address
#   - handle_message() is what happens when mail arrives
#   - send() / broadcast() put new letters in the outbox (the bus)
#   - The agent never walks to another agent's mailbox directly
#
# This abstraction is what enables ACP's key properties:
#   - Loose coupling    (agents only know bus addresses, not implementations)
#   - Observability     (all mail flows through the postal service)
#   - Replaceability    (swap any agent without touching others)
#   - Composability     (add new agents by giving them a mailbox address)
# =============================================================================

from datetime import datetime
from typing import Any, Dict, Optional

from acp.agent_registry import ACPAgentInfo, ACPAgentRegistry
from acp.message import ACPContentType, ACPMessage, ACPMessageType
from acp.message_bus import ACPMessageBus
from mcp.mcp_client import MCPClient


class BaseACPAgent:
    """
    Base class for all ACP-compliant agents in the ContentForge pipeline.

    Concrete agents subclass this and implement:
        handle_message(self, message: ACPMessage) -> None

    Everything else — bus subscription, message sending, logging, status
    updates — is handled here so subclasses stay focused on their logic.

    ACP CONCEPT: The Agent Lifecycle
    ---------------------------------
    1. INSTANTIATION  : __init__ sets up identity, bus subscription,
                        and MCP client connection.
    2. IDLE           : Agent is registered and waiting for messages.
    3. PROCESSING     : Agent received a message and is working on it.
                        handle_message() is executing.
    4. SENDING        : Agent calls send() or broadcast() to emit results.
    5. DONE / ERROR   : Agent finished (or failed) and updates its status.
    6. IDLE again     : Agent is ready for the next message in the pipeline.

    Parameters
    ----------
    agent_id : str
        Unique identifier for this agent.  Used as the routing address
        on the ACP Message Bus.  Convention: lowercase, no spaces.
        Examples: "researcher", "writer", "editor", "seo", "publisher"

    name : str
        Human-readable display name shown in logs and the registry.
        Examples: "ResearcherAgent", "WriterAgent"

    bus : ACPMessageBus
        The shared message bus for this pipeline run.  The agent
        subscribes to this bus in __init__ and all outgoing messages
        are published through it.

    mcp_client : MCPClient
        Pre-configured MCP client for this agent.  Used to call tools
        on the MCP server (search, style guides, SEO data, etc.).

    registry : ACPAgentRegistry, optional
        The shared agent registry.  If provided, the agent updates its
        status (idle → processing → done/error) during processing so
        the registry reflects live pipeline state.
    """

    def __init__(
        self,
        agent_id: str,
        name: str,
        bus: ACPMessageBus,
        mcp_client: MCPClient,
        registry: Optional[ACPAgentRegistry] = None,
    ) -> None:
        # ---- Core identity ------------------------------------------------
        self.agent_id: str = agent_id
        self.name: str = name

        # ---- Infrastructure references ------------------------------------
        self.bus: ACPMessageBus = bus
        self.mcp: MCPClient = mcp_client
        self.registry: Optional[ACPAgentRegistry] = registry

        # ---- Internal state -----------------------------------------------
        self._status: str = "idle"
        self._messages_received: int = 0
        self._messages_sent: int = 0
        self._errors_encountered: int = 0
        self._started_at: str = datetime.now().isoformat()

        # ---- Update the MCP client so its logs show our agent_id ----------
        # This means [MCP] log lines will say "researcher → search_topic(...)"
        # instead of "unknown → search_topic(...)"
        self.mcp.set_agent_id(agent_id)

        # ---- Subscribe to the ACP Message Bus ----------------------------
        # ACP CONCEPT: Subscription at construction time
        # -----------------------------------------------
        # By subscribing in __init__, we guarantee that the agent is
        # "on the network" as soon as it is created.  Any message
        # addressed to self.agent_id that arrives after this point
        # will be delivered to self.handle_message.
        #
        # The bus.subscribe() call passes self.handle_message (a bound
        # method) as the handler.  When the bus delivers a message, it
        # calls handler(message) — i.e. self.handle_message(message).
        self.bus.subscribe(self.agent_id, self.handle_message)

        self._log(
            f"Initialized and subscribed to ACP Message Bus. "
            f"Ready to receive messages addressed to '{self.agent_id}'."
        )

    # ==========================================================================
    # ABSTRACT: subclasses MUST override this
    # ==========================================================================

    def handle_message(self, message: ACPMessage) -> None:
        """
        Process an incoming ACP message.

        ACP CONCEPT: The Handler Contract
        -----------------------------------
        This method is the agent's "inbox processor" — the bus calls it
        whenever a message is routed to this agent.  Every agent MUST
        override this method.

        The base implementation:
          - Increments the message counter (for stats)
          - Updates status to "processing"
          - Calls _dispatch(message) which subclasses implement
          - Handles any unexpected exceptions and emits ERROR messages
          - Resets status to "idle" after processing

        Subclasses should override _dispatch() rather than handle_message()
        directly, so they automatically get the status tracking and error
        handling in this base implementation.

        Parameters
        ----------
        message : ACPMessage
            The incoming ACP message envelope.  The handler should check
            message.message_type and message.payload to decide what to do.
        """
        self._messages_received += 1

        self._log(
            f"Received {message.message_type.value} message from "
            f"'{message.sender_id}' (id:{message.message_id})"
        )

        # Mark ourselves as busy in the registry
        self._set_status("processing")

        try:
            # Delegate to the subclass-specific dispatch logic
            self._dispatch(message)
        except Exception as exc:
            # ACP CONCEPT: Graceful error propagation via ERROR messages
            # -----------------------------------------------------------
            # Instead of letting the exception propagate (which would
            # crash the bus's publish() call and potentially stall the
            # pipeline), we catch it here and emit an ERROR message back
            # to the sender.  This keeps the pipeline observable —
            # the error shows up in the ACP message history — and allows
            # the sender to decide how to handle the failure.
            self._errors_encountered += 1
            self._set_status("error")
            error_payload = {
                "agent_id": self.agent_id,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "original_message_id": message.message_id,
                "original_sender": message.sender_id,
            }
            self._log(
                f"ERROR processing message {message.message_id}: "
                f"{type(exc).__name__}: {exc}"
            )
            # Send error back to whoever sent us the message
            if message.sender_id:
                self.send(
                    receiver_id=message.sender_id,
                    payload=error_payload,
                    msg_type=ACPMessageType.ERROR,
                    correlation_id=message.message_id,
                )
            return

        # If we get here, processing completed successfully
        # Status will have been set to "done" inside _dispatch() by the
        # concrete agent — but if they forgot, we set it here as a fallback.
        if self._status == "processing":
            self._set_status("idle")

    def _dispatch(self, message: ACPMessage) -> None:
        """
        Subclass-specific message processing logic.

        MUST be overridden by concrete agent classes.
        The base implementation raises NotImplementedError so that
        forgetting to override is caught immediately at runtime.

        Parameters
        ----------
        message : ACPMessage
            The incoming message to process.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement _dispatch(). "
            f"Override this method to define how the agent processes "
            f"incoming ACP messages."
        )

    # ==========================================================================
    # OUTGOING MESSAGE HELPERS
    # ==========================================================================

    def send(
        self,
        receiver_id: str,
        payload: Any,
        msg_type: ACPMessageType = ACPMessageType.RESPONSE,
        content_type: ACPContentType = ACPContentType.JSON,
        correlation_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ACPMessage:
        """
        Send a unicast message to a specific agent via the ACP Message Bus.

        ACP CONCEPT: Unicast Sending
        -----------------------------
        This method constructs a fully-formed ACPMessage envelope and
        publishes it to the bus.  Key points:
          - The sender_id is ALWAYS set to self.agent_id (enforced here)
          - The message is published to the BUS, not delivered directly
          - The bus routes it to the receiver_id agent
          - The correlation_id links this message to a previous one,
            forming the request/response chain in the message history

        Agents SHOULD call this method rather than constructing and
        publishing ACPMessages manually, because:
          1. It ensures sender_id is always correctly set
          2. It increments the sent counter for stats
          3. It logs the outgoing message consistently

        Parameters
        ----------
        receiver_id : str
            The agent_id of the intended recipient.
        payload : Any
            The data to send.  Structure is agreed between the sender
            and receiver — this base class does not enforce a schema.
        msg_type : ACPMessageType
            Defaults to RESPONSE (most common case: replying to a request).
        content_type : ACPContentType
            Defaults to JSON (most agent payloads are dicts).
        correlation_id : str, optional
            The message_id of the REQUEST this is responding to.
            Set this whenever this send is in response to a received message.
        metadata : dict, optional
            Free-form key/value pairs for protocol extensions.

        Returns
        -------
        ACPMessage
            The message that was published, so callers can inspect it
            (e.g. to note its message_id for correlation tracking).
        """
        message = ACPMessage(
            sender_id=self.agent_id,
            receiver_id=receiver_id,
            message_type=msg_type,
            content_type=content_type,
            payload=payload,
            correlation_id=correlation_id,
            metadata=metadata or {},
        )

        self._messages_sent += 1

        self._log(
            f"Sending {msg_type.value} to '{receiver_id}' "
            f"(msg:{message.message_id}"
            + (f", corr:{correlation_id}" if correlation_id else "")
            + ")"
        )

        # ACP CONCEPT: The bus is the ONLY delivery mechanism.
        # We never call receiver_agent.handle_message(message) directly.
        # Everything goes through bus.publish() so it gets logged and routed.
        self.bus.publish(message)

        return message

    def broadcast(
        self,
        payload: Any,
        msg_type: ACPMessageType = ACPMessageType.BROADCAST,
        content_type: ACPContentType = ACPContentType.JSON,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ACPMessage:
        """
        Broadcast a message to ALL agents currently subscribed to the bus.

        ACP CONCEPT: Broadcast / Fan-Out
        ---------------------------------
        Setting receiver_id = None in an ACPMessage signals the bus to
        deliver it to every registered subscriber (except the sender).

        Use broadcast() for pipeline-wide announcements:
          - "I have completed my stage, pipeline can advance"
          - "An article has been published at <URL>"
          - "An error occurred, all agents should halt"
          - "New configuration available, agents should refresh their state"

        Broadcast messages appear in the ACP history just like unicast
        messages, so the full pipeline story is always captured.

        Parameters
        ----------
        payload : Any
            The announcement data to send to all agents.
        msg_type : ACPMessageType
            Defaults to BROADCAST.
        content_type : ACPContentType
            Defaults to JSON.
        metadata : dict, optional
            Free-form key/value pairs for protocol extensions.

        Returns
        -------
        ACPMessage
            The message that was published.
        """
        message = ACPMessage(
            sender_id=self.agent_id,
            receiver_id=None,  # None = broadcast to all
            message_type=msg_type,
            content_type=content_type,
            payload=payload,
            metadata=metadata or {},
        )

        self._messages_sent += 1

        self._log(
            f"Broadcasting {msg_type.value} to ALL agents (msg:{message.message_id})"
        )

        self.bus.publish(message)

        return message

    def acknowledge(self, original_message: ACPMessage) -> ACPMessage:
        """
        Send an ACK (acknowledgment) back to the sender of a message.

        ACP CONCEPT: Acknowledgment
        ----------------------------
        In truly asynchronous ACP pipelines, an agent might take a long time
        to process a message.  Sending an ACK immediately lets the original
        sender know "I received your message and will start working on it."
        This prevents the sender from timing out or resending.

        In ContentForge's synchronous pipeline, ACKs are optional (processing
        is immediate), but this method is provided to show the full ACP
        protocol toolkit and to support future async upgrades.

        Parameters
        ----------
        original_message : ACPMessage
            The message being acknowledged.

        Returns
        -------
        ACPMessage
            The ACK message that was sent.
        """
        return self.send(
            receiver_id=original_message.sender_id,
            payload={"status": "received", "processing": True},
            msg_type=ACPMessageType.ACK,
            content_type=ACPContentType.JSON,
            correlation_id=original_message.message_id,
        )

    # ==========================================================================
    # STATUS MANAGEMENT
    # ==========================================================================

    def _set_status(self, new_status: str) -> None:
        """
        Update this agent's operational status.

        Updates both the local _status field and the shared ACPAgentRegistry
        (if one was provided at construction time).

        ACP CONCEPT: Live Status Updates
        ----------------------------------
        Calling registry.update_status() here means the registry is always
        an accurate reflection of what each agent is doing right now.  An
        operator watching the registry sees:

            researcher: idle → processing → done
            writer:     idle → processing → done
            editor:     idle → processing → done
            ...

        This real-time status dashboard is one of ACP's key operational
        benefits — you don't need to instrument every agent individually;
        the base class handles it uniformly.

        Parameters
        ----------
        new_status : str
            One of: "idle", "processing", "done", "error"
        """
        old_status = self._status
        self._status = new_status

        # Update the shared registry so the pipeline dashboard reflects
        # this agent's current state
        if self.registry is not None:
            self.registry.update_status(self.agent_id, new_status)
        else:
            # Log locally if no registry is connected
            if old_status != new_status:
                self._log(f"Status: {old_status} → {new_status}")

    def mark_done(self) -> None:
        """
        Mark this agent as done with its current work.

        Convenience method for concrete agents to call at the end of
        their _dispatch() implementation when processing completed
        successfully.
        """
        self._set_status("done")

    def mark_error(self, reason: str = "") -> None:
        """
        Mark this agent as having encountered an error.

        Parameters
        ----------
        reason : str, optional
            Human-readable description of what went wrong.
        """
        self._errors_encountered += 1
        self._set_status("error")
        if reason:
            self._log(f"Error: {reason}")

    # ==========================================================================
    # LOGGING
    # ==========================================================================

    def _log(self, message: str, level: str = "INFO") -> None:
        """
        Print a formatted [ACP] log line for this agent.

        ACP CONCEPT: Consistent Log Formatting
        ----------------------------------------
        All agent activity is logged with a consistent prefix:
            [ACP][AgentName] message text

        The [ACP] tag makes it easy to grep the full console output and
        see only inter-agent protocol activity (as opposed to [MCP] lines
        which show tool calls, or the bus's own [BUS] lines).

        This mirrors how production distributed systems use structured
        logging with consistent field names (agent_id, level, timestamp)
        so logs can be parsed and filtered by monitoring tools like
        Datadog, Splunk, or the ELK stack.

        Parameters
        ----------
        message : str
            The log message text.
        level : str
            Log level label for filtering. Default "INFO".
        """
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]  # HH:MM:SS.ms
        prefix = f"[ACP][{self.name}]"
        print(f"  {prefix} {message}")

    def _log_section(self, title: str) -> None:
        """
        Print a visual section divider in the log output.

        Used by concrete agents to mark the beginning of major processing
        phases, making the console output much easier to read when following
        the pipeline execution.

        Parameters
        ----------
        title : str
            The section title to display.
        """
        width = 60
        padding = max(0, width - len(title) - 4)
        print(f"\n  ╔═ {title} {'═' * padding}╗")

    def _log_section_end(self) -> None:
        """Print a section end divider."""
        print(f"  ╚{'═' * 62}╝")

    # ==========================================================================
    # STATS & INTROSPECTION
    # ==========================================================================

    def get_stats(self) -> Dict[str, Any]:
        """
        Return a dict of operational statistics for this agent.

        Useful for post-run analysis: "How many messages did each agent
        send and receive?  Did any agents encounter errors?"

        Returns
        -------
        Dict[str, Any]
            {
                agent_id            : str,
                name                : str,
                status              : str,
                messages_received   : int,
                messages_sent       : int,
                errors_encountered  : int,
                started_at          : str,
                mcp_calls           : int   (from the MCP client ledger)
            }
        """
        mcp_stats = self.mcp.get_call_stats()
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "status": self._status,
            "messages_received": self._messages_received,
            "messages_sent": self._messages_sent,
            "errors_encountered": self._errors_encountered,
            "started_at": self._started_at,
            "mcp_calls": mcp_stats["total_calls"],
        }

    def print_stats(self) -> None:
        """Print a human-readable stats summary for this agent."""
        stats = self.get_stats()
        print(
            f"  [{self.name}] "
            f"recv={stats['messages_received']} "
            f"sent={stats['messages_sent']} "
            f"mcp={stats['mcp_calls']} "
            f"errors={stats['errors_encountered']} "
            f"status={stats['status']}"
        )

    def get_agent_info(self) -> ACPAgentInfo:
        """
        Build and return an ACPAgentInfo record for this agent.

        Concrete agents should override this to provide accurate
        input_schema, output_schema, pipeline_stage, and tags.

        The base implementation returns a minimal info record that is
        sufficient for bus subscription but lacks the richer metadata
        that makes the registry truly useful.

        Returns
        -------
        ACPAgentInfo
            A registry record describing this agent.
        """
        return ACPAgentInfo(
            agent_id=self.agent_id,
            name=self.name,
            description=f"ACP Agent: {self.name}",
            input_schema={},
            output_schema={},
            status=self._status,
            pipeline_stage=0,
            tags=[],
        )

    # ==========================================================================
    # DUNDER METHODS
    # ==========================================================================

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"{self.__class__.__name__}("
            f"id={self.agent_id!r}, "
            f"status={self._status!r}, "
            f"recv={self._messages_received}, "
            f"sent={self._messages_sent}"
            f")"
        )
