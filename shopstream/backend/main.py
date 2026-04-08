# backend/main.py
#
# =============================================================================
# SHOPSTREAM — FastAPI Server (Entry Point)
# =============================================================================
#
# This file is the heart of the ShopStream backend.  It wires together:
#
#   • Static file serving  — delivers the frontend (HTML/CSS/JS) to the browser
#   • SSE streaming route  — the /stream endpoint that sends AG-UI events
#   • Health check route   — /health for quick liveness verification
#
# HOW THE PROTOCOLS FIT TOGETHER HERE:
#
#   Browser                FastAPI                  ShoppingAgent
#   ───────                ───────                  ─────────────
#   GET /stream   ──────►  StreamingResponse  ────► process_query()
#   (EventSource)          (text/event-stream)        │
#                                                      ├─ MCP tool calls
#                                                      │  (MCPClient → MCPServer)
#                                                      │
#                          ◄── SSE chunks ────────────┘
#   onmessage()            "data: {...}\n\n"
#   handles AG-UI events
#
# AG-UI EVENTS ride over SSE:
#   Every event the ShoppingAgent yields is already formatted as
#   "data: {json}\n\n" by format_sse().  FastAPI's StreamingResponse
#   forwards each chunk to the browser as soon as it is yielded —
#   no buffering, no batching.  The browser's built-in EventSource
#   API parses the SSE framing and fires a "message" event for each
#   AG-UI event JSON object.
#
# RUN:
#   From the project2_shopstream/ directory:
#       uvicorn backend.main:app --reload --port 8000
#   Then open:  http://localhost:8000
#
# =============================================================================

import pathlib

from fastapi import FastAPI, Query
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from backend.agents.shopping_agent import ShoppingAgent

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# Resolve the project root regardless of where uvicorn is launched from.
# __file__ is  …/project2_shopstream/backend/main.py
# PROJECT_ROOT is …/project2_shopstream/
PROJECT_ROOT = pathlib.Path(__file__).parent.parent.resolve()
FRONTEND_DIR = PROJECT_ROOT / "frontend"

# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="ShopStream",
    description=(
        "Real-time AI Shopping Assistant demonstrating AG-UI and MCP protocols. "
        "The /stream endpoint emits AG-UI events over Server-Sent Events while "
        "the ShoppingAgent orchestrates MCP tool calls internally."
    ),
    version="1.0.0",
)

# ---------------------------------------------------------------------------
# Static files — serve the frontend directory
# ---------------------------------------------------------------------------
# Mount /static so that style.css and app.js are reachable from the browser.
# The index.html is served explicitly by the GET / route below so that we can
# return it as an HTMLResponse (which sets the correct Content-Type header).
app.mount(
    "/static",
    StaticFiles(directory=str(FRONTEND_DIR)),
    name="static",
)

# ---------------------------------------------------------------------------
# Shared agent instance
# ---------------------------------------------------------------------------
# One ShoppingAgent is created at startup and reused for all requests.
# It holds a single MCPServer (the in-memory product catalogue) but creates
# a fresh MCPClient (= new MCP session) for every query — exactly as you
# would in production.
shopping_agent = ShoppingAgent()

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def serve_index():
    """
    Serve the frontend SPA.

    Returns the contents of frontend/index.html directly.  In production
    you would typically serve this through a CDN or a dedicated static host,
    but serving it from the same FastAPI process keeps the demo self-contained.
    """
    index_path = FRONTEND_DIR / "index.html"
    if not index_path.exists():
        return HTMLResponse(
            content="<h1>Frontend not found</h1>"
            "<p>Make sure frontend/index.html exists.</p>",
            status_code=404,
        )
    return HTMLResponse(content=index_path.read_text(encoding="utf-8"))


@app.get("/health")
async def health_check():
    """
    Liveness check endpoint.

    Returns a JSON object confirming the server is running and lists the
    MCP tools registered on the shopping agent's server.  Useful for
    verifying the deployment is healthy without triggering a real query.
    """
    tools = shopping_agent._server.list_tools()
    return JSONResponse(
        content={
            "status": "ok",
            "service": "ShopStream",
            "protocols": ["AG-UI", "MCP", "SSE"],
            "mcp_tools_registered": [t["name"] for t in tools],
            "mcp_tool_count": len(tools),
        }
    )


@app.get("/stream")
async def stream_events(
    user_id: str = Query(
        default="user123",
        description="User identifier used to personalise recommendations",
        example="user123",
    ),
    query: str = Query(
        ...,
        description="The shopping query to process (e.g. 'wireless headphones')",
        example="wireless headphones",
    ),
):
    """
    AG-UI / SSE Streaming Endpoint.

    Opens a Server-Sent Events stream and emits AG-UI protocol events as the
    ShoppingAgent processes the query.  The browser's EventSource API connects
    here and fires a 'message' event for each AG-UI event that arrives.

    AG-UI Event sequence emitted:
        RUN_STARTED → STATE_SNAPSHOT → TEXT_MESSAGE_START
        → TEXT_MESSAGE_CONTENT (× N words)
        → TOOL_CALL_START/END (search_products)
        → STATE_DELTA (products)
        → TOOL_CALL_START/END (get_user_preferences)
        → STATE_DELTA (prefs loaded)
        → TOOL_CALL_START/END (get_recommendations)
        → STATE_DELTA (recommendations)
        → TOOL_CALL_START/END (compare_products)
        → STATE_DELTA (comparison)
        → TEXT_MESSAGE_CONTENT (× N words — full response)
        → TEXT_MESSAGE_END
        → STATE_DELTA (status=done)
        → RUN_FINISHED

    Parameters
    ----------
    user_id : str
        Identifies the shopper — used by the agent to load preferences and
        personalise recommendations.  Defaults to "user123".
    query : str
        The free-text shopping query (e.g. "wireless headphones under $100").

    Returns
    -------
    StreamingResponse
        Content-Type: text/event-stream
        Each body chunk is a complete SSE message:  "data: {json}\\n\\n"
    """

    async def event_generator():
        """
        Async generator that drives the ShoppingAgent and yields raw SSE bytes.

        FastAPI's StreamingResponse will call next() on this generator and
        write each chunk to the HTTP response body as soon as it is available,
        without waiting for the generator to finish.  This is what makes the
        streaming real-time from the browser's perspective.
        """
        try:
            # process_query() is itself an async generator that yields
            # SSE-formatted strings ("data: {...}\n\n").  We forward each
            # chunk straight to the HTTP response — zero intermediate buffering.
            async for sse_chunk in shopping_agent.process_query(
                user_id=user_id,
                query=query,
            ):
                yield sse_chunk

        except Exception as exc:  # noqa: BLE001
            # If the agent raises an unexpected error, emit a CUSTOM AG-UI
            # event so the frontend knows something went wrong rather than
            # silently closing the stream.
            import json

            from backend.agui.agui_protocol import AGUIEventType

            error_event = {
                "type": AGUIEventType.CUSTOM.value,
                "timestamp": _utc_now(),
                "data": {
                    "event_name": "AGENT_ERROR",
                    "payload": {
                        "error": str(exc),
                        "user_id": user_id,
                        "query": query,
                    },
                },
            }
            yield f"data: {json.dumps(error_event)}\n\n"
            print(f"[main.py] Agent error: {exc}", flush=True)

    # SSE requires specific headers so the browser treats the response as
    # an event stream rather than a regular HTTP download:
    #
    #   Content-Type        : text/event-stream  — tells EventSource this is SSE
    #   Cache-Control       : no-cache           — prevent proxy/CDN buffering
    #   X-Accel-Buffering   : no                 — disable nginx proxy buffering
    #   Connection          : keep-alive         — keep the TCP connection open
    headers = {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
        "Access-Control-Allow-Origin": "*",  # allow browser requests from any origin
    }

    return StreamingResponse(
        content=event_generator(),
        media_type="text/event-stream",
        headers=headers,
    )


@app.get("/api/products")
async def list_all_products():
    """
    Convenience endpoint — return the full product catalogue as JSON.

    Not used by the main streaming flow; handy for debugging and for
    developers exploring the data model.
    """
    from backend.mcp.mcp_server import PRODUCTS

    return JSONResponse(content={"products": PRODUCTS, "count": len(PRODUCTS)})


@app.get("/api/users")
async def list_users():
    """
    Return the available user IDs (without exposing private preference data).
    """
    from backend.mcp.mcp_server import USER_PREFS

    return JSONResponse(
        content={
            "users": [
                {"user_id": uid, "name": prefs.get("name", uid)}
                for uid, prefs in USER_PREFS.items()
            ]
        }
    )


@app.get("/api/tools")
async def list_mcp_tools():
    """
    Return the MCP tool registry — useful for educational inspection.

    Shows every tool the agent can call, including its description and
    parameter schema.  Mirrors the MCP tools/list operation.
    """
    tools = shopping_agent._server.list_tools()
    return JSONResponse(content={"tools": tools, "count": len(tools)})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    """Return current UTC time as an ISO-8601 string (used in error events)."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ---------------------------------------------------------------------------
# Dev server entry point
# ---------------------------------------------------------------------------
# Allows running directly with:  python -m backend.main
# (uvicorn backend.main:app --reload  is preferred for development)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )
