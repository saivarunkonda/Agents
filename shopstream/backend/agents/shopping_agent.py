# backend/agents/shopping_agent.py
#
# =============================================================================
# SHOPPING AGENT — AG-UI + MCP Orchestrator
# =============================================================================
#
# The ShoppingAgent is the brain of ShopStream.  It sits at the intersection
# of two protocols:
#
#   • MCP  — it CALLS tools on the MCP server to gather product data
#   • AG-UI — it EMITS events that describe its reasoning and results to the UI
#
# DESIGN PATTERN: Async Generator Pipeline
# ─────────────────────────────────────────
# process_query() is an async generator.  Instead of collecting all results
# and returning them at once, it `yield`s AG-UI events one by one as work
# progresses.  The FastAPI endpoint consumes this generator and forwards each
# yielded event to the browser over SSE — achieving true real-time streaming
# with zero buffering.
#
# AGENT LIFECYCLE (matches AG-UI event lifecycle):
#
#   1. RUN_STARTED          — announce the run to the frontend
#   2. STATE_SNAPSHOT       — publish initial shared state
#   3. TEXT_MESSAGE_START   — open the "thinking" message bubble
#   4. TEXT_MESSAGE_CONTENT — stream "Searching for products..." word-by-word
#   5. TOOL_CALL_START/END  — search_products via MCP
#   6. STATE_DELTA          — push found products into shared state
#   7. TOOL_CALL_START/END  — get_user_preferences via MCP
#   8. TOOL_CALL_START/END  — get_recommendations via MCP
#   9. STATE_DELTA          — push recommendations into shared state
#  10. TOOL_CALL_START/END  — compare_products via MCP (top 2 results)
#  11. TEXT_MESSAGE_CONTENT — stream full recommendation text word-by-word
#  12. TEXT_MESSAGE_END     — close the message bubble
#  13. STATE_DELTA          — set status = "done"
#  14. RUN_FINISHED         — signal the frontend the run is complete
#
# =============================================================================

from __future__ import annotations

import asyncio
import uuid
from typing import AsyncGenerator

from backend.agui.agui_protocol import (
    AGUIEvent,
    format_sse,
    make_run_finished,
    make_run_started,
    make_state_delta,
    make_state_snapshot,
    make_text_message_content,
    make_text_message_end,
    make_text_message_start,
    make_tool_call_end,
    make_tool_call_start,
)
from backend.mcp.mcp_client import MCPClient
from backend.mcp.mcp_server import MCPServer

# ---------------------------------------------------------------------------
# Token streaming delay — makes the word-by-word text feel natural.
# Lower values = faster streaming; raise to 0.12 for a more "typewriter" feel.
# ---------------------------------------------------------------------------
_WORD_DELAY_SECONDS = 0.07


class ShoppingAgent:
    """
    The ShopStream AI Shopping Assistant agent.

    Each call to process_query() creates a fresh MCP session, runs the full
    shopping pipeline, and yields AG-UI events that the FastAPI SSE endpoint
    forwards directly to the browser.

    The agent shares one MCPServer instance (the mock product database) across
    all sessions but creates a new MCPClient (and therefore a new session ID)
    per query — mirroring how a real MCP deployment would work.

    Usage (inside FastAPI):
        agent = ShoppingAgent()
        async for sse_chunk in agent.process_query("user123", "wireless headphones"):
            yield sse_chunk   # raw SSE-formatted string
    """

    def __init__(self):
        # One shared server — the mock "database" is stateless and read-only
        # so sharing it across requests is perfectly safe.
        self._server = MCPServer()
        print("[ShoppingAgent] Initialised — MCP server ready with tools:", flush=True)
        for tool in self._server.list_tools():
            print(f"  • {tool['name']}: {tool['description'][:60]}…", flush=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def process_query(
        self,
        user_id: str,
        query: str,
    ) -> AsyncGenerator[str, None]:
        """
        Run the full shopping pipeline for a user query.

        Yields
        ------
        str
            SSE-formatted AG-UI event strings ("data: {...}\\n\\n").
            Yielding strings (not AGUIEvent objects) means the FastAPI
            StreamingResponse can forward them directly without extra
            serialisation at the route level.
        """
        # Create a fresh MCP client (= new MCP session) for this query.
        # AG-UI CONCEPT: run_id ties all events in this run together so the
        # frontend can filter events if multiple runs share a connection.
        run_id = str(uuid.uuid4())
        mcp_client = MCPClient(server=self._server)

        print(
            f"\n[ShoppingAgent] ── New run ──────────────────────────────────",
            flush=True,
        )
        print(
            f"[ShoppingAgent] run_id={run_id[:8]}  user={user_id}  query='{query}'",
            flush=True,
        )

        # Shared state object — the agent updates this throughout the run and
        # publishes changes via STATE_SNAPSHOT / STATE_DELTA events so the
        # frontend always has an accurate picture of what the agent knows.
        state: dict = {
            "status": "started",
            "query": query,
            "user_id": user_id,
            "products": [],
            "recommendations": [],
            "comparison": None,
            "message": "",
            "run_id": run_id,
        }

        # ── Helper: emit one AG-UI event as an SSE string ─────────────────
        def sse(event: AGUIEvent) -> str:
            return format_sse(event)

        # ==================================================================
        # PHASE 1 — Run Startup
        # ==================================================================

        # AG-UI EVENT: RUN_STARTED
        # The very first event.  Tells the frontend a new agent run has begun
        # so it can reset its UI, show a loading indicator, etc.
        yield sse(
            make_run_started(
                run_id=run_id,
                metadata={
                    "user_id": user_id,
                    "query": query,
                    "agent": "ShoppingAgent v1.0",
                    "protocols": ["AG-UI", "MCP"],
                },
            )
        )
        await asyncio.sleep(0.05)

        # AG-UI EVENT: STATE_SNAPSHOT
        # Publish the complete initial state.  The frontend replaces any stale
        # state it might have from a previous run with this fresh snapshot.
        yield sse(make_state_snapshot(state=state, run_id=run_id))
        await asyncio.sleep(0.05)

        # ==================================================================
        # PHASE 2 — Opening message (streamed word-by-word)
        # ==================================================================

        message_id = str(uuid.uuid4())

        # AG-UI EVENT: TEXT_MESSAGE_START
        # Opens a new message container in the UI.  All TEXT_MESSAGE_CONTENT
        # events that follow will be appended to this container.
        yield sse(make_text_message_start(message_id=message_id, run_id=run_id))

        opening = f"Let me find the best options for '{query}'. Searching the catalog and personalising results for you..."
        async for chunk in _stream_words(opening, _WORD_DELAY_SECONDS):
            # AG-UI EVENT: TEXT_MESSAGE_CONTENT
            # Each word/token is its own event — this is the AG-UI streaming
            # pattern that allows incremental rendering on the frontend.
            yield sse(
                make_text_message_content(
                    message_id=message_id, delta=chunk, run_id=run_id
                )
            )

        # ==================================================================
        # PHASE 3 — MCP Tool Call: search_products
        # ==================================================================

        tool_call_id = str(uuid.uuid4())

        # AG-UI EVENT: TOOL_CALL_START
        # Announces to the UI that the agent is about to call an MCP tool.
        # The frontend adds a pending entry to the tool-call log.
        yield sse(
            make_tool_call_start(
                tool_call_id=tool_call_id,
                tool_name="search_products",
                tool_args={"query": query},
                run_id=run_id,
            )
        )

        # ── MCP CALL ──────────────────────────────────────────────────────
        # The agent delegates to the MCP client, which logs the call with
        # its session ID and routes it to the MCPServer handler.
        search_result = mcp_client.call_tool("search_products", query=query)
        await asyncio.sleep(0.1)  # simulate slight network/processing latency

        products_found: list[dict] = []
        if search_result["success"]:
            products_found = search_result["result"].get("products", [])

        # AG-UI EVENT: TOOL_CALL_END
        # The tool has returned.  Passes the full result so the UI can display
        # a summary and give the user access to the raw MCP response.
        yield sse(
            make_tool_call_end(
                tool_call_id=tool_call_id,
                tool_name="search_products",
                result=search_result["result"],
                success=search_result["success"],
                run_id=run_id,
            )
        )
        await asyncio.sleep(0.05)

        # AG-UI EVENT: STATE_DELTA
        # Push the found products into shared state.  The frontend merges this
        # patch into its state and re-renders the product cards section.
        state["products"] = products_found
        state["status"] = "searching"
        yield sse(
            make_state_delta(
                patch={
                    "products": products_found,
                    "status": "searching",
                    "products_count": len(products_found),
                },
                run_id=run_id,
            )
        )
        await asyncio.sleep(0.05)

        # ==================================================================
        # PHASE 4 — MCP Tool Call: get_user_preferences
        # ==================================================================

        tool_call_id = str(uuid.uuid4())

        yield sse(
            make_tool_call_start(
                tool_call_id=tool_call_id,
                tool_name="get_user_preferences",
                tool_args={"user_id": user_id},
                run_id=run_id,
            )
        )

        prefs_result = mcp_client.call_tool("get_user_preferences", user_id=user_id)
        await asyncio.sleep(0.08)

        user_prefs: dict = {}
        if prefs_result["success"]:
            user_prefs = prefs_result["result"].get("preferences", {})

        yield sse(
            make_tool_call_end(
                tool_call_id=tool_call_id,
                tool_name="get_user_preferences",
                result=prefs_result["result"],
                success=prefs_result["success"],
                run_id=run_id,
            )
        )
        await asyncio.sleep(0.05)

        # Update state with user context
        state["status"] = "personalising"
        yield sse(
            make_state_delta(
                patch={
                    "status": "personalising",
                    "user_prefs_loaded": prefs_result["success"],
                    "budget_max": user_prefs.get("budget_max"),
                },
                run_id=run_id,
            )
        )
        await asyncio.sleep(0.05)

        # ==================================================================
        # PHASE 5 — MCP Tool Call: get_recommendations
        # ==================================================================

        tool_call_id = str(uuid.uuid4())

        yield sse(
            make_tool_call_start(
                tool_call_id=tool_call_id,
                tool_name="get_recommendations",
                tool_args={"user_id": user_id, "query": query},
                run_id=run_id,
            )
        )

        recs_result = mcp_client.call_tool(
            "get_recommendations", user_id=user_id, query=query
        )
        await asyncio.sleep(0.12)

        recommendations: list[dict] = []
        if recs_result["success"]:
            recommendations = recs_result["result"].get("recommendations", [])

        yield sse(
            make_tool_call_end(
                tool_call_id=tool_call_id,
                tool_name="get_recommendations",
                result=recs_result["result"],
                success=recs_result["success"],
                run_id=run_id,
            )
        )
        await asyncio.sleep(0.05)

        # AG-UI EVENT: STATE_DELTA — push recommendations into shared state
        state["recommendations"] = recommendations
        state["status"] = "comparing"
        yield sse(
            make_state_delta(
                patch={
                    "recommendations": recommendations,
                    "status": "comparing",
                },
                run_id=run_id,
            )
        )
        await asyncio.sleep(0.05)

        # ==================================================================
        # PHASE 6 — MCP Tool Call: compare_products (top 2 recommendations)
        # ==================================================================

        comparison_result_data: dict | None = None

        if len(recommendations) >= 2:
            top_ids = [r["product"]["id"] for r in recommendations[:2]]
            tool_call_id = str(uuid.uuid4())

            yield sse(
                make_tool_call_start(
                    tool_call_id=tool_call_id,
                    tool_name="compare_products",
                    tool_args={"product_ids": top_ids},
                    run_id=run_id,
                )
            )

            compare_result = mcp_client.call_tool(
                "compare_products", product_ids=top_ids
            )
            await asyncio.sleep(0.1)

            if compare_result["success"]:
                comparison_result_data = compare_result["result"]

            yield sse(
                make_tool_call_end(
                    tool_call_id=tool_call_id,
                    tool_name="compare_products",
                    result=compare_result["result"],
                    success=compare_result["success"],
                    run_id=run_id,
                )
            )
            await asyncio.sleep(0.05)

            state["comparison"] = comparison_result_data
            yield sse(
                make_state_delta(
                    patch={"comparison": comparison_result_data},
                    run_id=run_id,
                )
            )
            await asyncio.sleep(0.05)

        # ==================================================================
        # PHASE 7 — Stream the final recommendation text word-by-word
        # ==================================================================

        state["status"] = "responding"
        yield sse(make_state_delta(patch={"status": "responding"}, run_id=run_id))

        # Build the recommendation narrative
        response_text = _build_response_text(
            query=query,
            products_found=products_found,
            recommendations=recommendations,
            comparison=comparison_result_data,
            user_prefs=user_prefs,
        )

        # Stream the response word-by-word using TEXT_MESSAGE_CONTENT events.
        # This is the AG-UI token-streaming pattern — identical to how a real
        # LLM would stream its output through the protocol.
        async for chunk in _stream_words(response_text, _WORD_DELAY_SECONDS):
            yield sse(
                make_text_message_content(
                    message_id=message_id, delta=chunk, run_id=run_id
                )
            )

        # AG-UI EVENT: TEXT_MESSAGE_END
        # Signals that the message is complete.  The frontend removes any
        # "typing" cursor and marks the message as final.
        yield sse(make_text_message_end(message_id=message_id, run_id=run_id))
        await asyncio.sleep(0.05)

        # ==================================================================
        # PHASE 8 — Run Teardown
        # ==================================================================

        state["status"] = "done"
        state["message"] = response_text

        # Final state delta — set status to done so the UI hides the spinner
        yield sse(
            make_state_delta(
                patch={"status": "done", "message": response_text},
                run_id=run_id,
            )
        )
        await asyncio.sleep(0.05)

        # Collect the MCP session summary for the RUN_FINISHED payload
        mcp_summary = mcp_client.get_call_summary()
        mcp_client.close()

        # AG-UI EVENT: RUN_FINISHED
        # The very last event.  Carries the final state and the MCP call
        # summary so the frontend (and any observers) can see what happened.
        yield sse(
            make_run_finished(
                run_id=run_id,
                final_state=state,
                mcp_summary=mcp_summary,
            )
        )

        print(
            f"[ShoppingAgent] run_id={run_id[:8]} completed — "
            f"{mcp_summary['total_calls']} MCP calls, "
            f"{len(products_found)} products found, "
            f"{len(recommendations)} recommendations",
            flush=True,
        )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


async def _stream_words(text: str, delay: float) -> AsyncGenerator[str, None]:
    """
    Async generator that yields the text word-by-word with a small delay.

    In a real LLM integration, this generator would be driven by the model's
    token stream.  Here we simulate it by splitting on whitespace so the
    AG-UI streaming behaviour is visible and educational.

    Each yielded chunk includes the trailing space so the frontend can append
    chunks directly to the message container without extra whitespace logic.
    """
    words = text.split(" ")
    for i, word in enumerate(words):
        # Append a space after every word except the last
        chunk = word + (" " if i < len(words) - 1 else "")
        yield chunk
        await asyncio.sleep(delay)


def _build_response_text(
    query: str,
    products_found: list[dict],
    recommendations: list[dict],
    comparison: dict | None,
    user_prefs: dict,
) -> str:
    """
    Compose the natural-language recommendation narrative.

    This function is deliberately straightforward — in a real agent you would
    pass all gathered context to an LLM and stream its output.  Here we build
    the text programmatically so ShopStream works with zero LLM API keys.
    """
    parts: list[str] = []

    budget = user_prefs.get("budget_max")
    preferred_brands = user_prefs.get("preferred_brands", [])

    # ── Opening summary ────────────────────────────────────────────────────
    if not products_found:
        return (
            f"I searched our catalog for '{query}' but couldn't find any matching "
            "products. Try broadening your search terms or removing category filters."
        )

    count = len(products_found)
    parts.append(
        f"I found {count} product{'s' if count != 1 else ''} matching '{query}'."
    )

    if budget:
        within_budget = [p for p in products_found if p["price"] <= budget]
        parts.append(
            f"Of these, {len(within_budget)} fit{'s' if len(within_budget) == 1 else ''} "
            f"within your ${budget} budget."
        )

    # ── Top recommendation ─────────────────────────────────────────────────
    if recommendations:
        top = recommendations[0]
        product = top["product"]
        reason = top["reason"]

        parts.append(
            f"\n\nMy top pick for you is the {product['name']} at ${product['price']:.2f} "
            f"(rated {product['rating']}/5.0). {product['description']}"
        )
        parts.append(f"Why this one? It {reason}.")

        # ── Comparison insight ─────────────────────────────────────────────
        if comparison and len(recommendations) >= 2:
            runner_up = recommendations[1]["product"]
            summary = comparison.get("summary", {})
            cheapest = summary.get("cheapest", {})
            best_rated = summary.get("best_rated", {})

            parts.append(
                f"\n\nCompared to the {runner_up['name']} (${runner_up['price']:.2f}, "
                f"rated {runner_up['rating']}/5.0):"
            )

            if cheapest.get("id") == product["id"]:
                savings = runner_up["price"] - product["price"]
                parts.append(f"• The {product['name']} is ${savings:.2f} cheaper.")
            elif cheapest.get("id") == runner_up["id"]:
                premium = product["price"] - runner_up["price"]
                parts.append(
                    f"• The {runner_up['name']} is ${premium:.2f} less expensive — "
                    "a solid budget alternative."
                )

            if best_rated.get("id") == product["id"]:
                parts.append(f"• The {product['name']} has the higher customer rating.")
            elif best_rated.get("id") == runner_up["id"]:
                parts.append(f"• The {runner_up['name']} edges it out on ratings.")

        # ── Runner-up mention ──────────────────────────────────────────────
        if len(recommendations) >= 2:
            runner_up_rec = recommendations[1]
            runner_up_product = runner_up_rec["product"]
            parts.append(
                f"\n\nRunner-up: {runner_up_product['name']} at ${runner_up_product['price']:.2f}. "
                f"{runner_up_rec['reason'].capitalize()}."
            )

        # ── Third recommendation ───────────────────────────────────────────
        if len(recommendations) >= 3:
            third_rec = recommendations[2]
            third_product = third_rec["product"]
            parts.append(
                f"Also worth considering: the {third_product['name']} "
                f"(${third_product['price']:.2f}, {third_product['rating']}/5.0)."
            )

    # ── Brand affinity note ────────────────────────────────────────────────
    if preferred_brands:
        brands_str = " and ".join(preferred_brands[:2])
        brand_matches = [
            p
            for p in products_found
            if any(b.lower() in p["name"].lower() for b in preferred_brands)
        ]
        if brand_matches:
            parts.append(
                f"\n\nI noticed you prefer {brands_str} — "
                f"{len(brand_matches)} result{'s' if len(brand_matches) != 1 else ''} "
                "in your list come from those brands."
            )

    # ── Closing call-to-action ─────────────────────────────────────────────
    parts.append(
        "\n\nAll product cards are shown below. Click any card to see full details "
        "and check live inventory."
    )

    return " ".join(parts)
