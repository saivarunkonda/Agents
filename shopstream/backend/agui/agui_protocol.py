# backend/agui/agui_protocol.py
#
# =============================================================================
# AG-UI PROTOCOL — Agent-User Interface Protocol Implementation
# =============================================================================
#
# AG-UI is an open, lightweight protocol that standardises how AI agents
# communicate with frontend UIs in real time.  Instead of a single HTTP
# response arriving all at once, AG-UI defines a *stream* of typed events
# that flow from the agent to the browser over Server-Sent Events (SSE).
#
# This gives the frontend a live, structured window into the agent's mind:
#   • When did the run start / finish?
#   • What tool is the agent calling right now?
#   • Which words has it produced so far?
#   • How has the shared application state changed?
#
# CORE AG-UI CONCEPTS implemented in this file:
#
#   1. EVENT TYPES  (AGUIEventType)
#      A controlled vocabulary of event identifiers.  Every event the agent
#      emits MUST use one of these types so the frontend can route it to the
#      correct handler without inspecting the payload.
#
#   2. EVENT ENVELOPE  (AGUIEvent)
#      A thin wrapper that pairs an event type with a timestamp and an
#      arbitrary payload dict.  The envelope is always present; the payload
#      schema varies per event type (documented inline below).
#
#   3. SSE WIRE FORMAT  (format_sse)
#      AG-UI rides on top of the W3C Server-Sent Events standard.  Each event
#      is serialised as:
#
#          data: {"type": "...", "timestamp": "...", "data": {...}}\n\n
#
#      The double newline is the SSE message terminator that the browser's
#      EventSource API uses to delimit events.
#
#   4. HELPER FACTORIES  (make_* functions)
#      Convenience constructors for every event type so calling code never
#      has to remember the exact payload shape.
#
# LIFECYCLE OF A TYPICAL AG-UI RUN:
#
#   RUN_STARTED
#       └─► STATE_SNAPSHOT          (initial shared state)
#           └─► TEXT_MESSAGE_START  (agent begins speaking)
#               ├─► TEXT_MESSAGE_CONTENT  (word 1)
#               ├─► TEXT_MESSAGE_CONTENT  (word 2 …)
#               └─► TEXT_MESSAGE_END
#           ├─► TOOL_CALL_START     (agent invokes an MCP tool)
#           │       └─► TOOL_CALL_END  (tool returned a result)
#           ├─► STATE_DELTA         (shared state patch)
#           └─► …more tool calls / text chunks…
#   RUN_FINISHED
#
# =============================================================================

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

# ---------------------------------------------------------------------------
# 1. AG-UI EVENT TYPES
# ---------------------------------------------------------------------------


class AGUIEventType(Enum):
    """
    AG-UI Event Type identifiers.

    Each member maps to a string value that is embedded verbatim in the
    JSON payload so the browser-side EventSource handler can switch on it
    without a lookup table.

    Groupings (for readability — not part of the spec):
      • Run lifecycle   : RUN_STARTED, RUN_FINISHED
      • Text streaming  : TEXT_MESSAGE_START, TEXT_MESSAGE_CONTENT, TEXT_MESSAGE_END
      • Tool calls      : TOOL_CALL_START, TOOL_CALL_END
      • State management: STATE_SNAPSHOT, STATE_DELTA
      • Escape hatch    : CUSTOM
    """

    # ── Run lifecycle ────────────────────────────────────────────────────────
    # Emitted once at the very beginning and very end of an agent run.
    # The frontend uses these to show/hide loading spinners and to know when
    # it is safe to let the user start a new query.
    RUN_STARTED = "RUN_STARTED"
    RUN_FINISHED = "RUN_FINISHED"

    # ── Text message streaming ───────────────────────────────────────────────
    # AG-UI splits streamed text into three events so the frontend can render
    # tokens incrementally without buffering the entire response:
    #   START   → open a new message bubble / text container
    #   CONTENT → append the token/chunk to the open container
    #   END     → close the container, mark message complete
    TEXT_MESSAGE_START = "TEXT_MESSAGE_START"
    TEXT_MESSAGE_CONTENT = "TEXT_MESSAGE_CONTENT"
    TEXT_MESSAGE_END = "TEXT_MESSAGE_END"

    # ── Tool calls ───────────────────────────────────────────────────────────
    # Wrap every MCP tool invocation so the UI can display a live "agent is
    # calling tool X" indicator and then show the result when it arrives.
    #   START → tool invocation begins  (show spinner in tool-call log)
    #   END   → tool returned a result  (update log entry with result)
    TOOL_CALL_START = "TOOL_CALL_START"
    TOOL_CALL_END = "TOOL_CALL_END"

    # ── State management ─────────────────────────────────────────────────────
    # AG-UI defines a *shared state* object that both the agent and the UI
    # can read.  The agent publishes state changes via two event types:
    #   SNAPSHOT → replace the entire state with a new object (used at start)
    #   DELTA    → apply a partial patch to the existing state (RFC 6902 style,
    #              or simply a dict of keys to update — we use the latter here)
    STATE_SNAPSHOT = "STATE_SNAPSHOT"
    STATE_DELTA = "STATE_DELTA"

    # ── Escape hatch ─────────────────────────────────────────────────────────
    # CUSTOM lets agents emit application-specific events without extending
    # the core enum.  The "event_name" field in the payload identifies the
    # specific custom event type.
    CUSTOM = "CUSTOM"


# ---------------------------------------------------------------------------
# 2. AG-UI EVENT ENVELOPE
# ---------------------------------------------------------------------------


@dataclass
class AGUIEvent:
    """
    A single AG-UI protocol event.

    Fields
    ------
    type : AGUIEventType
        Which kind of event this is.  Determines how the frontend routes it.
    data : dict
        Event-specific payload.  Schema varies by type; see helper factories
        below for the canonical shape of each event's data dict.
    timestamp : str
        ISO-8601 UTC timestamp, auto-populated at construction time.
        Lets the frontend display or log the precise moment each event fired.
    run_id : str | None
        Optional identifier linking all events in the same agent run.
        Useful when multiple concurrent runs share a single SSE connection.
    """

    type: AGUIEventType
    data: dict = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: _utc_now())
    run_id: str | None = None

    def to_dict(self) -> dict:
        """
        Serialise the event to a plain dict suitable for JSON encoding.

        The "type" field uses the enum's string value (e.g. "RUN_STARTED")
        so the JavaScript client never needs to import Python enums.
        """
        payload = {
            "type": self.type.value,  # string, not enum object
            "timestamp": self.timestamp,
            "data": self.data,
        }
        if self.run_id is not None:
            payload["run_id"] = self.run_id
        return payload

    def to_json(self) -> str:
        """Return the event as a compact JSON string."""
        return json.dumps(self.to_dict(), ensure_ascii=False)


# ---------------------------------------------------------------------------
# 3. SSE WIRE FORMAT
# ---------------------------------------------------------------------------


def format_sse(event: AGUIEvent) -> str:
    """
    Serialise an AGUIEvent into the W3C Server-Sent Events wire format.

    SSE messages consist of one or more "field: value" lines followed by a
    blank line.  The browser's built-in EventSource API parses this format
    natively — no extra library required on the client side.

    We use only the "data:" field (the most portable option) and embed the
    entire AG-UI event as a single-line JSON object.  The double newline at
    the end is the mandatory SSE message terminator.

    Wire format produced:
        data: {"type":"RUN_STARTED","timestamp":"…","data":{…}}\n\n

    Parameters
    ----------
    event : AGUIEvent
        The event to serialise.

    Returns
    -------
    str
        A complete SSE message string ready to be written to the response stream.
    """
    json_payload = event.to_json()
    return f"data: {json_payload}\n\n"


# ---------------------------------------------------------------------------
# 4. HELPER FACTORIES
# Convenience constructors — one per event type.
# Using these keeps calling code declarative and avoids payload shape bugs.
# ---------------------------------------------------------------------------


def make_run_started(run_id: str, metadata: dict | None = None) -> AGUIEvent:
    """
    RUN_STARTED — emitted once before any other event in a run.

    Payload shape:
        { "run_id": str, "metadata": dict }
    """
    return AGUIEvent(
        type=AGUIEventType.RUN_STARTED,
        run_id=run_id,
        data={
            "run_id": run_id,
            "metadata": metadata or {},
        },
    )


def make_run_finished(
    run_id: str,
    final_state: dict | None = None,
    mcp_summary: dict | None = None,
) -> AGUIEvent:
    """
    RUN_FINISHED — emitted once after the agent has completed all work.

    Payload shape:
        {
            "run_id"      : str,
            "final_state" : dict,   # last known shared state snapshot
            "mcp_summary" : dict,   # summary of MCP calls made this run
        }
    """
    return AGUIEvent(
        type=AGUIEventType.RUN_FINISHED,
        run_id=run_id,
        data={
            "run_id": run_id,
            "final_state": final_state or {},
            "mcp_summary": mcp_summary or {},
        },
    )


def make_text_message_start(
    message_id: str,
    run_id: str | None = None,
    role: str = "assistant",
) -> AGUIEvent:
    """
    TEXT_MESSAGE_START — signals that a new streamed text message is beginning.

    The frontend should create a new message container (e.g. a <div>) keyed
    to message_id.  Subsequent TEXT_MESSAGE_CONTENT events with the same
    message_id append tokens to this container.

    Payload shape:
        { "message_id": str, "role": str }
    """
    return AGUIEvent(
        type=AGUIEventType.TEXT_MESSAGE_START,
        run_id=run_id,
        data={"message_id": message_id, "role": role},
    )


def make_text_message_content(
    message_id: str,
    delta: str,
    run_id: str | None = None,
) -> AGUIEvent:
    """
    TEXT_MESSAGE_CONTENT — one chunk (token / word) of a streamed message.

    The frontend appends `delta` to the container opened by the corresponding
    TEXT_MESSAGE_START.  Chunks may be individual characters, words, or
    sentences — the consumer should not assume granularity.

    Payload shape:
        { "message_id": str, "delta": str }
    """
    return AGUIEvent(
        type=AGUIEventType.TEXT_MESSAGE_CONTENT,
        run_id=run_id,
        data={"message_id": message_id, "delta": delta},
    )


def make_text_message_end(
    message_id: str,
    run_id: str | None = None,
) -> AGUIEvent:
    """
    TEXT_MESSAGE_END — signals that the streamed message is complete.

    The frontend should finalise the message container (e.g. remove a
    blinking cursor, mark the message as done).

    Payload shape:
        { "message_id": str }
    """
    return AGUIEvent(
        type=AGUIEventType.TEXT_MESSAGE_END,
        run_id=run_id,
        data={"message_id": message_id},
    )


def make_tool_call_start(
    tool_call_id: str,
    tool_name: str,
    tool_args: dict | None = None,
    run_id: str | None = None,
) -> AGUIEvent:
    """
    TOOL_CALL_START — the agent is about to invoke an MCP tool.

    The frontend adds a pending entry to the tool-call log showing the
    tool name and arguments.

    Payload shape:
        { "tool_call_id": str, "tool_name": str, "tool_args": dict }
    """
    return AGUIEvent(
        type=AGUIEventType.TOOL_CALL_START,
        run_id=run_id,
        data={
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "tool_args": tool_args or {},
        },
    )


def make_tool_call_end(
    tool_call_id: str,
    tool_name: str,
    result: Any,
    success: bool = True,
    run_id: str | None = None,
) -> AGUIEvent:
    """
    TOOL_CALL_END — the MCP tool has returned a result.

    The frontend updates the pending tool-call log entry with the result
    summary and changes its visual state from "pending" to "complete".

    Payload shape:
        {
            "tool_call_id" : str,
            "tool_name"    : str,
            "success"      : bool,
            "result"       : any,   # full tool result (may be large)
            "summary"      : str,   # one-line human-readable summary
        }
    """
    summary = _summarise_tool_result(tool_name, result, success)
    return AGUIEvent(
        type=AGUIEventType.TOOL_CALL_END,
        run_id=run_id,
        data={
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "success": success,
            "result": result,
            "summary": summary,
        },
    )


def make_state_snapshot(state: dict, run_id: str | None = None) -> AGUIEvent:
    """
    STATE_SNAPSHOT — replace the entire shared state with `state`.

    Emitted once at the start of a run to establish the initial state.
    The frontend should treat this as an authoritative replacement of
    whatever state it held previously.

    Payload shape:
        { "snapshot": dict }   # the complete new state object
    """
    return AGUIEvent(
        type=AGUIEventType.STATE_SNAPSHOT,
        run_id=run_id,
        data={"snapshot": state},
    )


def make_state_delta(patch: dict, run_id: str | None = None) -> AGUIEvent:
    """
    STATE_DELTA — apply a partial update to the shared state.

    `patch` is a dict whose keys are top-level state keys to update.
    The frontend merges this into its current state (shallow merge).
    For nested updates, the entire sub-object is replaced.

    This is simpler than RFC 6902 JSON Patch but sufficient for our use case.

    Payload shape:
        { "patch": dict }
    """
    return AGUIEvent(
        type=AGUIEventType.STATE_DELTA,
        run_id=run_id,
        data={"patch": patch},
    )


def make_custom_event(
    event_name: str,
    payload: dict | None = None,
    run_id: str | None = None,
) -> AGUIEvent:
    """
    CUSTOM — an application-specific event that does not fit the standard types.

    Payload shape:
        { "event_name": str, "payload": dict }
    """
    return AGUIEvent(
        type=AGUIEventType.CUSTOM,
        run_id=run_id,
        data={"event_name": event_name, "payload": payload or {}},
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    """Return current UTC time as an ISO-8601 string with 'Z' suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _summarise_tool_result(tool_name: str, result: Any, success: bool) -> str:
    """
    Generate a concise one-line summary of a tool result for the UI log.

    This is purely presentational — the full result is also included in the
    event payload for consumers that need it.
    """
    if not success:
        return f"[ERROR] {tool_name} failed"

    if not isinstance(result, dict):
        return f"{tool_name} → {str(result)[:80]}"

    # Per-tool friendly summaries
    if tool_name == "search_products":
        count = result.get("total_found", 0)
        query = result.get("query", "")
        return f"Found {count} product(s) matching '{query}'"

    if tool_name == "get_product_details":
        product = result.get("product") or {}
        name = product.get("name", "unknown")
        price = product.get("price", "?")
        return f"Details for '{name}' — ${price}"

    if tool_name == "check_inventory":
        name = result.get("product_name", result.get("product_id", "?"))
        availability = result.get("availability", "unknown")
        stock = result.get("stock", "?")
        return f"'{name}': {availability} ({stock} units)"

    if tool_name == "get_user_preferences":
        user_id = result.get("user_id", "?")
        prefs = result.get("preferences", {})
        budget = prefs.get("budget_max", "unlimited")
        brands = prefs.get("preferred_brands", [])
        brands_str = ", ".join(brands[:3]) if brands else "none"
        return f"Prefs for {user_id} — budget ${budget}, brands: {brands_str}"

    if tool_name == "compare_products":
        products = result.get("products", [])
        names = [p.get("name", p.get("id", "?")) for p in products[:4]]
        summary = result.get("summary", {})
        best = summary.get("best_value", {}).get("name", "?")
        return f"Compared {len(names)} products — best value: '{best}'"

    if tool_name == "get_recommendations":
        recs = result.get("recommendations", [])
        if recs:
            top = recs[0].get("product", {}).get("name", "?")
            return f"Top recommendation: '{top}' (+{len(recs) - 1} more)"
        return "No recommendations found"

    # Generic fallback
    keys = list(result.keys())[:4]
    return f"{tool_name} returned keys: {keys}"
