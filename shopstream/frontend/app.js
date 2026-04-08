// =============================================================================
// SHOPSTREAM — app.js
// AG-UI Protocol EventSource Consumer
// =============================================================================
//
// This file is the frontend counterpart to the AG-UI event stream.
// It opens a Server-Sent Events (SSE) connection to the FastAPI /stream
// endpoint and routes every incoming AG-UI event to the correct UI handler.
//
// PROTOCOL CONCEPTS DEMONSTRATED:
//
//   AG-UI over SSE
//   ─────────────
//   The browser's built-in EventSource API connects to GET /stream.
//   The server sends "data: {json}\n\n" chunks — one per AG-UI event.
//   EventSource fires a "message" event for each chunk, giving us the
//   JSON string in event.data.  We parse it and dispatch by event.type.
//
//   Shared State
//   ────────────
//   AG-UI defines a shared state object that both agent and UI maintain.
//   STATE_SNAPSHOT replaces the whole state; STATE_DELTA patches it.
//   We mirror this in the JS `agentState` object below.
//
//   Event Routing
//   ─────────────
//   Each AG-UI event type maps to a dedicated handler function.
//   This mirrors the pattern you'd use in a React/Vue integration
//   (e.g. ag-ui-react's useCoAgent hook) but in plain JavaScript.
//
// =============================================================================

"use strict";

// ---------------------------------------------------------------------------
// 1. APPLICATION STATE
// ---------------------------------------------------------------------------

/**
 * agentState mirrors the AG-UI shared state published by the server.
 * Updated by STATE_SNAPSHOT (full replace) and STATE_DELTA (patch).
 * The UI reads from this object to stay in sync with the agent.
 */
let agentState = {
  status: "idle",
  query: "",
  user_id: "",
  products: [],
  recommendations: [],
  comparison: null,
  message: "",
  run_id: null,
};

/** All raw AG-UI events received this session — used by the event log. */
let allEvents = [];

/** Currently active EventSource connection (or null if idle). */
let activeEventSource = null;

/** Map of tool_call_id → DOM element for live updates on TOOL_CALL_END. */
const toolCallElements = {};

/** Set of product IDs that are "recommended" (for card badging). */
let recommendedProductIds = new Set();

/** Full list of products currently rendered (for filtering). */
let renderedProducts = [];

/** Total AG-UI event count for the counter badge. */
let eventCount = 0;

/** Whether the event log panel is expanded. */
let eventLogExpanded = true;

/** Timestamp when the current run started (for duration display). */
let runStartTime = null;

// ---------------------------------------------------------------------------
// 2. ENTRY POINT — Search button handler
// ---------------------------------------------------------------------------

/**
 * startSearch()
 * Called by the Search button (and Enter key handler).
 *
 * AG-UI CONCEPT: Each search opens a fresh SSE connection, which triggers
 * a new agent run.  The run_id in RUN_STARTED ties every subsequent event
 * to this specific run.
 */
function startSearch() {
  const userId = document.getElementById("user-select").value.trim();
  const query = document.getElementById("query-input").value.trim();

  if (!query) {
    shakeInput();
    return;
  }

  // Close any existing stream before opening a new one
  if (activeEventSource) {
    cancelStream();
  }

  // Reset UI to a clean state for the new run
  resetUI();

  // Build the SSE URL — query params are the only "request" the client sends.
  // The entire response is the AG-UI event stream.
  const url = `/stream?user_id=${encodeURIComponent(userId)}&query=${encodeURIComponent(query)}`;

  console.log(`[AG-UI] Opening SSE connection → ${url}`);

  // ---------------------------------------------------------------------------
  // AG-UI TRANSPORT: SSE via EventSource
  // The browser's EventSource API handles reconnection, buffering, and the
  // "data: ...\n\n" framing automatically.  We just handle the messages.
  // ---------------------------------------------------------------------------
  activeEventSource = new EventSource(url);

  // Single "message" listener — all AG-UI events arrive on the default
  // unnamed event channel (i.e. lines starting with "data:").
  activeEventSource.addEventListener("message", onSSEMessage);

  // Connection lifecycle callbacks
  activeEventSource.addEventListener("open", onSSEOpen);
  activeEventSource.addEventListener("error", onSSEError);

  // Disable search button and show cancel button while streaming
  document.getElementById("search-btn").disabled = true;
  document.getElementById("cancel-btn").classList.remove("hidden");

  // Show the status bar
  showSection("status-section");
  updateStatusBar("started", "Connecting…");
}

// ---------------------------------------------------------------------------
// 3. SSE / TRANSPORT HANDLERS
// ---------------------------------------------------------------------------

/**
 * onSSEOpen — SSE connection established.
 * The server has accepted the request and is about to send AG-UI events.
 */
function onSSEOpen() {
  console.log("[AG-UI] SSE connection open");
  updateStatusBar("started", "Connected — waiting for agent…");
}

/**
 * onSSEMessage — core dispatch function.
 *
 * AG-UI CONCEPT: Every message is a complete JSON-encoded AGUIEvent.
 * We parse it and route it to the handler for its `type` field.
 * Unknown types are logged but otherwise ignored — forward compatibility.
 */
function onSSEMessage(sseEvent) {
  let agEvent;
  try {
    agEvent = JSON.parse(sseEvent.data);
  } catch (err) {
    console.warn("[AG-UI] Could not parse event:", sseEvent.data, err);
    return;
  }

  // Track every event for the educational log
  allEvents.push(agEvent);
  eventCount++;
  updateEventCounter(eventCount);
  appendToEventLog(agEvent);

  // ---------------------------------------------------------------------------
  // AG-UI EVENT ROUTER
  // Each case maps to a handler that updates the relevant section of the UI.
  // This is the AG-UI "message bus" — the central dispatch table.
  // ---------------------------------------------------------------------------
  switch (agEvent.type) {
    // ── Run lifecycle ────────────────────────────────────────────────────────
    case "RUN_STARTED":
      handleRunStarted(agEvent);
      break;
    case "RUN_FINISHED":
      handleRunFinished(agEvent);
      break;

    // ── Text streaming ───────────────────────────────────────────────────────
    case "TEXT_MESSAGE_START":
      handleTextMessageStart(agEvent);
      break;
    case "TEXT_MESSAGE_CONTENT":
      handleTextMessageContent(agEvent);
      break;
    case "TEXT_MESSAGE_END":
      handleTextMessageEnd(agEvent);
      break;

    // ── Tool calls ───────────────────────────────────────────────────────────
    case "TOOL_CALL_START":
      handleToolCallStart(agEvent);
      break;
    case "TOOL_CALL_END":
      handleToolCallEnd(agEvent);
      break;

    // ── State management ─────────────────────────────────────────────────────
    case "STATE_SNAPSHOT":
      handleStateSnapshot(agEvent);
      break;
    case "STATE_DELTA":
      handleStateDelta(agEvent);
      break;

    // ── Custom / escape hatch ─────────────────────────────────────────────────
    case "CUSTOM":
      handleCustomEvent(agEvent);
      break;

    default:
      console.warn("[AG-UI] Unknown event type:", agEvent.type);
  }
}

/**
 * onSSEError — SSE connection error or server close.
 * EventSource fires this when the connection drops or the server ends the stream.
 */
function onSSEError(err) {
  console.warn("[AG-UI] SSE error / stream ended:", err);

  // Close our side of the connection — prevents EventSource auto-reconnect
  // which is desirable here because the stream is intentionally one-shot.
  if (activeEventSource) {
    activeEventSource.close();
    activeEventSource = null;
  }

  // Only show error state if the run never finished cleanly
  if (agentState.status !== "done") {
    updateStatusBar("error", "Stream ended — try again");
    document.getElementById("search-btn").disabled = false;
    document.getElementById("cancel-btn").classList.add("hidden");
  }
}

// ---------------------------------------------------------------------------
// 4. AG-UI EVENT HANDLERS
//    One function per event type — keeps the dispatch table above clean
//    and makes it easy to add new event types in the future.
// ---------------------------------------------------------------------------

/**
 * RUN_STARTED
 * The agent has begun a new run.  Record the run_id and show loading UI.
 *
 * Payload: { run_id, metadata: { user_id, query, agent, protocols } }
 */
function handleRunStarted(event) {
  const { run_id, metadata = {} } = event.data;
  runStartTime = Date.now();

  // Store run_id in shared state — subsequent events reference it
  agentState.run_id = run_id;
  agentState.status = "started";

  console.log(`[AG-UI] RUN_STARTED  run_id=${run_id}`);

  // Show run ID in the status bar meta area
  const runIdDisplay = document.getElementById("run-id-display");
  runIdDisplay.textContent = `run: ${run_id.slice(0, 8)}`;

  updateStatusBar(
    "started",
    `Agent running — query: "${metadata.query || agentState.query}"`,
  );

  // Show the thinking section so tool calls can populate it
  showSection("thinking-section");
  showSection("response-section");
  showSection("event-log-section");
}

/**
 * RUN_FINISHED
 * The agent has completed all work.  Show the final state and close the stream.
 *
 * Payload: { run_id, final_state, mcp_summary: { total_calls, calls } }
 */
function handleRunFinished(event) {
  const { run_id, final_state = {}, mcp_summary = {} } = event.data;

  console.log(
    `[AG-UI] RUN_FINISHED  run_id=${run_id}  mcp_calls=${mcp_summary.total_calls}`,
  );

  // Apply the final state as an authoritative snapshot
  Object.assign(agentState, final_state);

  // Compute run duration
  const durationMs = runStartTime ? Date.now() - runStartTime : 0;
  const durationStr =
    durationMs < 1000
      ? `${durationMs}ms`
      : `${(durationMs / 1000).toFixed(1)}s`;

  updateStatusBar(
    "done",
    `Done ✓  — ${mcp_summary.total_calls || 0} MCP tool calls  ·  ${durationStr}`,
  );

  // Hide the pulsing "thinking" dot
  const pulse = document.getElementById("thinking-pulse");
  if (pulse) {
    pulse.style.animation = "none";
    pulse.style.color = "#16a34a";
  }

  // Re-enable search, hide cancel
  document.getElementById("search-btn").disabled = false;
  document.getElementById("cancel-btn").classList.add("hidden");

  // Close the EventSource — the run is over, no more events expected
  if (activeEventSource) {
    activeEventSource.close();
    activeEventSource = null;
  }

  // Scroll the event log to the bottom so RUN_FINISHED is visible
  scrollEventLogToBottom();
}

/**
 * TEXT_MESSAGE_START
 * The agent is about to begin streaming text.
 * Create/prepare the message container identified by message_id.
 *
 * Payload: { message_id, role }
 *
 * AG-UI CONCEPT: message_id links the START, CONTENT chunks, and END together.
 * The UI opens a container on START, appends chunks on each CONTENT, and
 * finalises it on END — enabling true incremental rendering.
 */
function handleTextMessageStart(event) {
  const { message_id, role } = event.data;

  console.log(
    `[AG-UI] TEXT_MESSAGE_START  message_id=${message_id}  role=${role}`,
  );

  // Clear previous response and show the cursor
  const responseContent = document.getElementById("response-content");
  const cursor = document.getElementById("response-cursor");
  if (responseContent) responseContent.textContent = "";
  if (cursor) cursor.classList.remove("hidden-cursor");

  // Store active message_id for content chunks to reference
  agentState._active_message_id = message_id;
}

/**
 * TEXT_MESSAGE_CONTENT
 * Append one token/word to the open message container.
 *
 * Payload: { message_id, delta }
 *
 * AG-UI CONCEPT: This is the token-streaming pattern — the most important
 * event type for perceived performance.  Each `delta` is appended directly
 * to the DOM so the user sees text appear word-by-word in real time,
 * exactly as they would with a streaming LLM response.
 */
function handleTextMessageContent(event) {
  const { message_id, delta } = event.data;

  // Only append if this chunk belongs to the currently active message
  if (message_id !== agentState._active_message_id) return;

  const responseContent = document.getElementById("response-content");
  if (responseContent) {
    responseContent.textContent += delta;
  }

  // Keep the response box scrolled to the latest text
  const responseText = document.getElementById("response-text");
  if (responseText) {
    responseText.scrollTop = responseText.scrollHeight;
  }
}

/**
 * TEXT_MESSAGE_END
 * The streamed message is complete — hide the typing cursor.
 *
 * Payload: { message_id }
 */
function handleTextMessageEnd(event) {
  const { message_id } = event.data;

  console.log(`[AG-UI] TEXT_MESSAGE_END  message_id=${message_id}`);

  const cursor = document.getElementById("response-cursor");
  if (cursor) cursor.classList.add("hidden-cursor");

  agentState._active_message_id = null;
}

/**
 * TOOL_CALL_START
 * The agent is invoking an MCP tool.  Add a "pending" entry to the tool log.
 *
 * Payload: { tool_call_id, tool_name, tool_args }
 *
 * AG-UI CONCEPT: TOOL_CALL_START lets the UI show a live "agent is working"
 * indicator before the result arrives.  The tool_call_id is used to match
 * this entry with its corresponding TOOL_CALL_END event.
 *
 * MCP CONCEPT: tool_name and tool_args come directly from the MCP tool
 * invocation — the same values the MCPClient logged on the server side.
 */
function handleToolCallStart(event) {
  const { tool_call_id, tool_name, tool_args = {} } = event.data;

  console.log(
    `[AG-UI] TOOL_CALL_START  tool=${tool_name}  id=${tool_call_id.slice(0, 8)}`,
  );

  const log = document.getElementById("tool-call-log");
  if (!log) return;

  // Build the args summary string
  const argsSummary = formatToolArgs(tool_args);

  // Create the pending tool entry DOM element
  const entry = document.createElement("div");
  entry.className = "tool-entry pending";
  entry.dataset.toolCallId = tool_call_id;
  entry.innerHTML = `
    <div class="tool-icon">
      <span class="tool-spinner" title="Running…"></span>
    </div>
    <div>
      <div class="tool-name">🔧 ${escapeHtml(tool_name)}</div>
      <div class="tool-args">${argsSummary}</div>
      <div class="tool-summary" id="tool-summary-${tool_call_id.slice(0, 8)}">
        <em>Calling MCP server…</em>
      </div>
    </div>
    <div class="tool-duration" id="tool-duration-${tool_call_id.slice(0, 8)}">
      ⏳
    </div>
  `;

  log.appendChild(entry);
  log.scrollTop = log.scrollHeight;

  // Store reference so TOOL_CALL_END can update it
  toolCallElements[tool_call_id] = {
    entry,
    startTime: Date.now(),
  };
}

/**
 * TOOL_CALL_END
 * The MCP tool has returned.  Update the pending log entry with the result.
 *
 * Payload: { tool_call_id, tool_name, success, result, summary }
 *
 * AG-UI CONCEPT: tool_call_id matches back to the TOOL_CALL_START entry so
 * the frontend can update the exact DOM element that was marked as pending.
 */
function handleToolCallEnd(event) {
  const { tool_call_id, tool_name, success, result, summary } = event.data;

  console.log(
    `[AG-UI] TOOL_CALL_END  tool=${tool_name}  success=${success}  id=${tool_call_id.slice(0, 8)}`,
  );

  const stored = toolCallElements[tool_call_id];
  if (!stored) return;

  const { entry, startTime } = stored;
  const durationMs = Date.now() - startTime;

  // Transition from pending → success / error
  entry.classList.remove("pending");
  entry.classList.add(success ? "success" : "error");

  // Replace spinner with checkmark/cross icon
  const iconEl = entry.querySelector(".tool-icon");
  if (iconEl) {
    iconEl.innerHTML = success ? "✅" : "❌";
  }

  // Update summary line
  const shortId = tool_call_id.slice(0, 8);
  const summaryEl = document.getElementById(`tool-summary-${shortId}`);
  if (summaryEl) {
    summaryEl.innerHTML = escapeHtml(
      summary || (success ? "Success" : "Error"),
    );
    summaryEl.style.fontStyle = "normal";
  }

  // Update duration
  const durationEl = document.getElementById(`tool-duration-${shortId}`);
  if (durationEl) {
    durationEl.textContent =
      durationMs < 1000
        ? `${durationMs}ms`
        : `${(durationMs / 1000).toFixed(2)}s`;
  }

  // Scroll tool log to latest entry
  const log = document.getElementById("tool-call-log");
  if (log) log.scrollTop = log.scrollHeight;
}

/**
 * STATE_SNAPSHOT
 * Replace the entire shared state with the published snapshot.
 *
 * Payload: { snapshot: { status, query, products, recommendations, … } }
 *
 * AG-UI CONCEPT: STATE_SNAPSHOT is the "ground truth" reset — the frontend
 * discards any locally computed state and adopts the agent's authoritative
 * version.  Typically sent once at the beginning of a run.
 */
function handleStateSnapshot(event) {
  const { snapshot = {} } = event.data;

  console.log("[AG-UI] STATE_SNAPSHOT  keys=", Object.keys(snapshot));

  // Full replace
  agentState = { ...agentState, ...snapshot };

  // Reflect initial state in the status bar
  if (snapshot.status) {
    updateStatusBar(snapshot.status, statusLabel(snapshot.status));
  }
}

/**
 * STATE_DELTA
 * Apply a partial patch to the shared state.
 *
 * Payload: { patch: { <key>: <new_value>, … } }
 *
 * AG-UI CONCEPT: STATE_DELTA is the incremental update mechanism.  Each
 * patch dict is shallow-merged into agentState.  The UI then inspects
 * which keys changed and re-renders only the affected sections.
 *
 * This is the most frequent event type during a run — every time the agent
 * updates the product list, recommendations, or run status, it emits a delta.
 */
function handleStateDelta(event) {
  const { patch = {} } = event.data;

  console.log("[AG-UI] STATE_DELTA  patch_keys=", Object.keys(patch));

  // Merge the patch into local state (shallow merge)
  Object.assign(agentState, patch);

  // ── React to specific state keys ─────────────────────────────────────────

  // Status update
  if (patch.status) {
    updateStatusBar(patch.status, statusLabel(patch.status));
  }

  // Products list arrived — render product cards
  if (patch.products !== undefined && Array.isArray(patch.products)) {
    renderProductCards(patch.products);
  }

  // Recommendations arrived — mark those products with badges
  if (
    patch.recommendations !== undefined &&
    Array.isArray(patch.recommendations)
  ) {
    applyRecommendationBadges(patch.recommendations);
  }

  // Comparison data arrived — could be used for an enhanced view
  if (patch.comparison !== undefined && patch.comparison !== null) {
    // Comparison data is included in the narrative text and product cards;
    // no separate rendering needed here — but it is available in agentState.comparison
    console.log(
      "[AG-UI] Comparison data received for",
      patch.comparison.products?.map((p) => p.name) ?? [],
    );
  }
}

/**
 * CUSTOM
 * Application-specific events that don't fit standard AG-UI types.
 * Currently used for AGENT_ERROR events from the server's error handler.
 *
 * Payload: { event_name, payload }
 */
function handleCustomEvent(event) {
  const { event_name, payload = {} } = event.data;

  console.warn("[AG-UI] CUSTOM event:", event_name, payload);

  if (event_name === "AGENT_ERROR") {
    updateStatusBar("error", `Agent error: ${payload.error || "unknown"}`);
    document.getElementById("search-btn").disabled = false;
    document.getElementById("cancel-btn").classList.add("hidden");
  }
}

// ---------------------------------------------------------------------------
// 5. PRODUCT CARD RENDERING
// ---------------------------------------------------------------------------

/**
 * renderProductCards(products)
 * Create product cards in the grid from an array of product objects.
 * Called when STATE_DELTA delivers products.
 *
 * Each card uses data- attributes for filtering without re-rendering.
 */
function renderProductCards(products) {
  if (!products || products.length === 0) return;

  const grid = document.getElementById("product-grid");
  const section = document.getElementById("products-section");
  const badge = document.getElementById("products-count-badge");

  if (!grid) return;

  // Clear previous cards
  grid.innerHTML = "";
  renderedProducts = products;

  // Update count badge
  if (badge) badge.textContent = products.length;

  products.forEach((product, index) => {
    const isRecommended = recommendedProductIds.has(product.id);
    const card = createProductCard(product, isRecommended, index);
    grid.appendChild(card);
  });

  showSection("products-section");

  // Reset filter buttons to "All"
  document
    .querySelectorAll(".filter-btn")
    .forEach((btn) => btn.classList.remove("active"));
  const allBtn = document.querySelector('.filter-btn[data-filter="all"]');
  if (allBtn) allBtn.classList.add("active");
}

/**
 * createProductCard(product, isRecommended, index)
 * Build and return a single product card DOM element.
 */
function createProductCard(product, isRecommended, index) {
  const card = document.createElement("div");
  card.className = "product-card" + (isRecommended ? " recommended" : "");
  card.dataset.category = product.category;
  card.dataset.productId = product.id;
  card.dataset.recommended = isRecommended ? "1" : "0";
  card.style.animationDelay = `${Math.min(index * 0.04, 0.36)}s`;

  // Stock label
  const stockClass =
    product.stock === 0
      ? "stock-out"
      : product.stock <= 10
        ? "stock-low"
        : "stock-in";
  const stockLabel =
    product.stock === 0
      ? "Out of Stock"
      : product.stock <= 10
        ? `Low Stock (${product.stock})`
        : `In Stock (${product.stock})`;

  // Star rating display
  const stars = renderStars(product.rating);

  card.innerHTML = `
    ${isRecommended ? '<div class="rec-badge">⭐ Recommended</div>' : ""}
    <span class="product-category-tag cat-${product.category}">${product.category}</span>
    <div class="product-name">${escapeHtml(product.name)}</div>
    <div class="product-price-row">
      <span class="product-price">$${product.price.toFixed(2)}</span>
      <span class="product-rating">${stars} ${product.rating}</span>
    </div>
    <span class="product-stock ${stockClass}">${stockLabel}</span>
    <div class="product-desc">${escapeHtml(product.description)}</div>
    <div class="product-tags">
      ${product.tags
        .slice(0, 4)
        .map((t) => `<span class="tag">${escapeHtml(t)}</span>`)
        .join("")}
    </div>
  `;

  // Click → open detail modal
  card.addEventListener("click", () =>
    openProductModal(product, isRecommended),
  );

  return card;
}

/**
 * applyRecommendationBadges(recommendations)
 * Mark the top-ranked products with "Recommended" badges.
 * Called when STATE_DELTA delivers recommendations.
 */
function applyRecommendationBadges(recommendations) {
  // Build set of recommended IDs
  recommendedProductIds = new Set(
    recommendations.map((r) => r.product?.id).filter(Boolean),
  );

  // If product cards are already rendered, update their badges
  document.querySelectorAll(".product-card").forEach((card) => {
    const pid = card.dataset.productId;
    if (recommendedProductIds.has(pid)) {
      card.classList.add("recommended");
      card.dataset.recommended = "1";
      // Add badge if not already present
      if (!card.querySelector(".rec-badge")) {
        const badge = document.createElement("div");
        badge.className = "rec-badge";
        badge.textContent = "⭐ Recommended";
        card.insertBefore(badge, card.firstChild);
      }
    }
  });
}

/**
 * filterProducts(filter)
 * Show/hide product cards based on category or "recommended" filter.
 * Uses data- attributes — no re-render needed.
 */
function filterProducts(filter) {
  // Update active button
  document.querySelectorAll(".filter-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.filter === filter);
  });

  document.querySelectorAll(".product-card").forEach((card) => {
    let visible = false;
    if (filter === "all") {
      visible = true;
    } else if (filter === "recommended") {
      visible = card.dataset.recommended === "1";
    } else {
      visible = card.dataset.category === filter;
    }
    card.style.display = visible ? "" : "none";
  });

  // Update count badge
  const visibleCount = document.querySelectorAll(
    '.product-card:not([style*="display: none"])',
  ).length;
  const badge = document.getElementById("products-count-badge");
  if (badge) badge.textContent = visibleCount;
}

// ---------------------------------------------------------------------------
// 6. PRODUCT DETAIL MODAL
// ---------------------------------------------------------------------------

/**
 * openProductModal(product, isRecommended)
 * Display a full-detail modal for a product card click.
 */
function openProductModal(product, isRecommended) {
  const modal = document.getElementById("product-modal");
  const content = document.getElementById("modal-content");
  if (!modal || !content) return;

  const stockClass =
    product.stock === 0
      ? "stock-out"
      : product.stock <= 10
        ? "stock-low"
        : "stock-in";
  const stockLabel =
    product.stock === 0
      ? "Out of Stock"
      : product.stock <= 10
        ? `Only ${product.stock} left!`
        : `${product.stock} in stock`;

  const stars = renderStars(product.rating);

  content.innerHTML = `
    <div class="modal-category">
      ${isRecommended ? '<div class="rec-badge" style="display:inline-flex;margin-bottom:8px;">⭐ Recommended for you</div><br>' : ""}
      <span class="product-category-tag cat-${product.category}">${product.category}</span>
    </div>
    <div class="modal-title">${escapeHtml(product.name)}</div>
    <div class="modal-price-row">
      <span class="modal-price">$${product.price.toFixed(2)}</span>
      <span class="modal-rating">${stars} ${product.rating} / 5.0</span>
      <span class="modal-stock product-stock ${stockClass}">${stockLabel}</span>
    </div>
    <p class="modal-desc">${escapeHtml(product.description)}</p>
    <div class="modal-meta-row">
      <span class="modal-meta-label">Product ID</span>
      <span>${escapeHtml(product.id)}</span>
    </div>
    <div class="modal-meta-row">
      <span class="modal-meta-label">Category</span>
      <span>${escapeHtml(product.category)}</span>
    </div>
    <div class="modal-tags">
      ${product.tags.map((t) => `<span class="tag">${escapeHtml(t)}</span>`).join("")}
    </div>
  `;

  modal.classList.remove("hidden");

  // Trap focus inside modal for accessibility
  setTimeout(() => modal.querySelector(".modal-close")?.focus(), 50);
}

/**
 * closeModal(event)
 * Close the product detail modal.
 * Called by the ✕ button or by clicking the overlay.
 */
function closeModal(event) {
  // If called from the overlay click, only close if the overlay itself was clicked
  if (event && event.target !== document.getElementById("product-modal"))
    return;
  document.getElementById("product-modal").classList.add("hidden");
}

// Close modal on Escape key
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    document.getElementById("product-modal")?.classList.add("hidden");
  }
});

// ---------------------------------------------------------------------------
// 7. EVENT LOG (Educational raw event viewer)
// ---------------------------------------------------------------------------

/**
 * appendToEventLog(agEvent)
 * Add a row to the protocol event log panel.
 *
 * AG-UI CONCEPT: This panel exposes the raw event stream so developers can
 * inspect every AG-UI event as it arrives — its type, timestamp, and payload.
 * This is the "educational" view that makes the protocol visible.
 */
function appendToEventLog(agEvent) {
  const log = document.getElementById("event-log");
  if (!log) return;

  // Extract timestamp — show only the time portion for brevity
  const ts = agEvent.timestamp
    ? agEvent.timestamp.replace(/^\d{4}-\d{2}-\d{2}T/, "").replace("Z", "")
    : "—";

  // Build a concise payload preview (truncated to avoid overwhelming the log)
  const payloadPreview = buildPayloadPreview(agEvent);

  const row = document.createElement("div");
  row.className = `event-row event-row-type-${agEvent.type}`;
  row.dataset.eventType = agEvent.type;
  row.title = JSON.stringify(agEvent, null, 2); // full JSON on hover (tooltip)

  row.innerHTML = `
    <span class="event-type-pill pill-${agEvent.type}">${shortTypeName(agEvent.type)}</span>
    <span class="event-ts">${escapeHtml(ts)}</span>
    <span class="event-payload">${escapeHtml(payloadPreview)}</span>
  `;

  log.appendChild(row);

  // Auto-scroll to the newest event
  scrollEventLogToBottom();

  // Update the event log count badge
  const badge = document.getElementById("event-log-count-badge");
  if (badge) badge.textContent = allEvents.length;

  // Apply current filter state
  applyEventFilter();
}

/**
 * buildPayloadPreview(agEvent)
 * Generate a concise one-line summary of an event's payload for the log.
 */
function buildPayloadPreview(agEvent) {
  const d = agEvent.data || {};
  switch (agEvent.type) {
    case "RUN_STARTED":
      return `run_id=${(d.run_id || "").slice(0, 8)}  user=${d.metadata?.user_id || "?"}  query="${d.metadata?.query || "?"}"`;
    case "RUN_FINISHED":
      return `run_id=${(d.run_id || "").slice(0, 8)}  mcp_calls=${d.mcp_summary?.total_calls || 0}`;
    case "TEXT_MESSAGE_START":
      return `message_id=${(d.message_id || "").slice(0, 8)}  role=${d.role}`;
    case "TEXT_MESSAGE_CONTENT":
      return `"${(d.delta || "").slice(0, 60)}"`;
    case "TEXT_MESSAGE_END":
      return `message_id=${(d.message_id || "").slice(0, 8)}`;
    case "TOOL_CALL_START":
      return `tool=${d.tool_name}  args=${JSON.stringify(d.tool_args || {}).slice(0, 60)}`;
    case "TOOL_CALL_END":
      return `tool=${d.tool_name}  success=${d.success}  summary="${(d.summary || "").slice(0, 60)}"`;
    case "STATE_SNAPSHOT":
      return `keys=[${Object.keys(d.snapshot || {}).join(", ")}]`;
    case "STATE_DELTA":
      return `patch_keys=[${Object.keys(d.patch || {}).join(", ")}]`;
    case "CUSTOM":
      return `event_name=${d.event_name}  payload=${JSON.stringify(d.payload || {}).slice(0, 60)}`;
    default:
      return JSON.stringify(d).slice(0, 80);
  }
}

/**
 * applyEventFilter()
 * Show/hide log rows based on the filter checkboxes.
 */
function applyEventFilter() {
  const showState = document.getElementById("filter-state")?.checked ?? true;
  const showText = document.getElementById("filter-text")?.checked ?? true;
  const showTools = document.getElementById("filter-tools")?.checked ?? true;
  const showLifecycle =
    document.getElementById("filter-lifecycle")?.checked ?? true;

  const STATE_TYPES = ["STATE_SNAPSHOT", "STATE_DELTA"];
  const TEXT_TYPES = [
    "TEXT_MESSAGE_START",
    "TEXT_MESSAGE_CONTENT",
    "TEXT_MESSAGE_END",
  ];
  const TOOL_TYPES = ["TOOL_CALL_START", "TOOL_CALL_END"];
  const LIFECYCLE_TYPES = ["RUN_STARTED", "RUN_FINISHED", "CUSTOM"];

  document.querySelectorAll(".event-row").forEach((row) => {
    const type = row.dataset.eventType || "";
    let visible = true;
    if (STATE_TYPES.includes(type)) visible = showState;
    else if (TEXT_TYPES.includes(type)) visible = showText;
    else if (TOOL_TYPES.includes(type)) visible = showTools;
    else if (LIFECYCLE_TYPES.includes(type)) visible = showLifecycle;
    row.style.display = visible ? "" : "none";
  });
}

/**
 * toggleEventLog()
 * Collapse or expand the event log body.
 */
function toggleEventLog() {
  eventLogExpanded = !eventLogExpanded;
  const body = document.getElementById("event-log-body");
  const btn = document.getElementById("event-log-toggle");
  if (body) body.style.display = eventLogExpanded ? "" : "none";
  if (btn) btn.textContent = eventLogExpanded ? "▼ Collapse" : "▶ Expand";
}

/**
 * clearEventLog()
 * Remove all rows from the event log display (does NOT clear allEvents array).
 */
function clearEventLog() {
  const log = document.getElementById("event-log");
  if (log) log.innerHTML = "";
}

/**
 * copyEventLog()
 * Copy all captured AG-UI events as pretty-printed JSON to the clipboard.
 */
function copyEventLog() {
  const json = JSON.stringify(allEvents, null, 2);
  navigator.clipboard
    .writeText(json)
    .then(() => {
      const btn = document.querySelector(".small-btn:last-child");
      if (btn) {
        const orig = btn.textContent;
        btn.textContent = "✓ Copied!";
        setTimeout(() => {
          btn.textContent = orig;
        }, 2000);
      }
    })
    .catch(() => {
      console.warn("Clipboard write failed");
    });
}

function scrollEventLogToBottom() {
  const log = document.getElementById("event-log");
  if (log) {
    // Use requestAnimationFrame so the DOM has painted the new row first
    requestAnimationFrame(() => {
      log.scrollTop = log.scrollHeight;
    });
  }
}

// ---------------------------------------------------------------------------
// 8. STATUS BAR HELPERS
// ---------------------------------------------------------------------------

/**
 * updateStatusBar(status, label)
 * Update the status dot class and label text.
 * Status maps to a CSS animation class on the dot.
 */
function updateStatusBar(status, label) {
  const dot = document.getElementById("status-dot");
  const text = document.getElementById("status-label");
  if (!dot || !text) return;

  // Remove all dot state classes
  dot.className = "status-dot";
  const validStatuses = [
    "started",
    "thinking",
    "searching",
    "personalising",
    "comparing",
    "responding",
    "done",
    "error",
  ];
  if (validStatuses.includes(status)) {
    dot.classList.add(`dot-${status}`);
  }

  text.textContent = label || statusLabel(status);
}

/**
 * statusLabel(status)
 * Map a machine status string to a human-readable label.
 */
function statusLabel(status) {
  const labels = {
    idle: "Idle",
    started: "Starting agent…",
    thinking: "Agent is thinking…",
    searching: "Searching catalog…",
    personalising: "Personalising results…",
    comparing: "Comparing products…",
    responding: "Writing recommendation…",
    done: "Done",
    error: "Error",
  };
  return labels[status] || status;
}

/**
 * updateEventCounter(count)
 * Update the "N events" badge in the status bar.
 */
function updateEventCounter(count) {
  const el = document.getElementById("event-counter");
  if (el) el.textContent = `${count} event${count !== 1 ? "s" : ""}`;
}

// ---------------------------------------------------------------------------
// 9. STREAM CONTROL
// ---------------------------------------------------------------------------

/**
 * cancelStream()
 * Close the active SSE connection and reset the UI to an interactable state.
 */
function cancelStream() {
  if (activeEventSource) {
    activeEventSource.close();
    activeEventSource = null;
    console.log("[AG-UI] SSE connection cancelled by user");
  }
  updateStatusBar("idle", "Cancelled");
  document.getElementById("search-btn").disabled = false;
  document.getElementById("cancel-btn").classList.add("hidden");
}

// ---------------------------------------------------------------------------
// 10. UI UTILITIES
// ---------------------------------------------------------------------------

/**
 * resetUI()
 * Clear all dynamic content before starting a new search.
 */
function resetUI() {
  // Reset shared state
  agentState = {
    status: "idle",
    query: document.getElementById("query-input")?.value || "",
    user_id: document.getElementById("user-select")?.value || "",
    products: [],
    recommendations: [],
    comparison: null,
    message: "",
    run_id: null,
  };

  allEvents = [];
  eventCount = 0;
  recommendedProductIds = new Set();
  renderedProducts = [];
  runStartTime = null;
  Object.keys(toolCallElements).forEach((k) => delete toolCallElements[k]);

  // Clear dynamic DOM sections
  const toolLog = document.getElementById("tool-call-log");
  if (toolLog) toolLog.innerHTML = "";

  const productGrid = document.getElementById("product-grid");
  if (productGrid) productGrid.innerHTML = "";

  const responseContent = document.getElementById("response-content");
  if (responseContent) responseContent.textContent = "";

  const cursor = document.getElementById("response-cursor");
  if (cursor) cursor.classList.remove("hidden-cursor");

  const eventLog = document.getElementById("event-log");
  if (eventLog) eventLog.innerHTML = "";

  // Hide sections that only appear during/after a run
  hideSection("products-section");

  // Reset status bar counters
  updateEventCounter(0);
  const runIdDisplay = document.getElementById("run-id-display");
  if (runIdDisplay) runIdDisplay.textContent = "";

  // Reset thinking pulse animation
  const pulse = document.getElementById("thinking-pulse");
  if (pulse) {
    pulse.style.animation = "";
    pulse.style.color = "";
  }

  // Reset filter buttons
  document.querySelectorAll(".filter-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.filter === "all");
  });
}

/**
 * showSection(id) / hideSection(id)
 * Toggle the visibility of a major UI section.
 */
function showSection(id) {
  document.getElementById(id)?.classList.remove("hidden");
}

function hideSection(id) {
  document.getElementById(id)?.classList.add("hidden");
}

/**
 * setQuery(text)
 * Set the query input value from a quick-chip click.
 */
function setQuery(text) {
  const input = document.getElementById("query-input");
  if (input) {
    input.value = text;
    input.focus();
  }
}

/**
 * shakeInput()
 * Briefly animate the query input to indicate it is required.
 */
function shakeInput() {
  const input = document.getElementById("query-input");
  if (!input) return;
  input.style.transition = "transform 0.08s ease";
  const shake = ["-6px", "6px", "-4px", "4px", "0px"];
  let i = 0;
  const interval = setInterval(() => {
    input.style.transform = `translateX(${shake[i]})`;
    i++;
    if (i >= shake.length) {
      clearInterval(interval);
      input.style.transform = "";
      input.focus();
    }
  }, 60);
}

/**
 * renderStars(rating)
 * Convert a numeric rating (0–5) to a star string.
 */
function renderStars(rating) {
  const full = Math.floor(rating);
  const half = rating % 1 >= 0.5 ? 1 : 0;
  const empty = 5 - full - half;
  return "★".repeat(full) + (half ? "½" : "") + "☆".repeat(empty);
}

/**
 * escapeHtml(str)
 * Escape HTML special characters to prevent XSS when inserting user/API data.
 */
function escapeHtml(str) {
  if (str == null) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/**
 * shortTypeName(type)
 * Abbreviate long event type names for the narrow log pill column.
 */
function shortTypeName(type) {
  const short = {
    RUN_STARTED: "RUN▶",
    RUN_FINISHED: "RUN■",
    TEXT_MESSAGE_START: "TXT▶",
    TEXT_MESSAGE_CONTENT: "TXT~",
    TEXT_MESSAGE_END: "TXT■",
    TOOL_CALL_START: "MCP▶",
    TOOL_CALL_END: "MCP■",
    STATE_SNAPSHOT: "ST=",
    STATE_DELTA: "ST∆",
    CUSTOM: "CUST",
  };
  return short[type] || type.slice(0, 6);
}

/**
 * formatToolArgs(args)
 * Build a concise string representation of MCP tool arguments for the log.
 */
function formatToolArgs(args) {
  if (!args || Object.keys(args).length === 0) return "<em>no args</em>";
  return Object.entries(args)
    .map(([k, v]) => {
      const val = Array.isArray(v)
        ? `[${v.join(", ")}]`
        : String(v).slice(0, 50);
      return `<span style="color:#7c3aed">${escapeHtml(k)}</span>=<span style="color:#0369a1">${escapeHtml(val)}</span>`;
    })
    .join("  ");
}

// ---------------------------------------------------------------------------
// 11. KEYBOARD SHORTCUTS
// ---------------------------------------------------------------------------

document.addEventListener("keydown", (e) => {
  // Enter in query input → trigger search
  if (e.key === "Enter" && document.activeElement?.id === "query-input") {
    startSearch();
  }
  // Escape → cancel active stream (if not handled by modal close above)
  if (e.key === "Escape" && activeEventSource) {
    cancelStream();
  }
});

// ---------------------------------------------------------------------------
// 12. INITIALISATION
// ---------------------------------------------------------------------------

/**
 * On DOMContentLoaded, perform any one-time setup.
 */
document.addEventListener("DOMContentLoaded", () => {
  console.log(
    "%c[ShopStream] AG-UI + MCP Demo ready.",
    "color: #2563eb; font-weight: bold; font-size: 14px;",
  );
  console.log(
    "%cOpen /stream?query=... in a new tab to see the raw SSE + AG-UI events.",
    "color: #475569; font-size: 12px;",
  );
  console.log(
    "%cAPI endpoints: GET /health  GET /api/products  GET /api/tools  GET /api/users",
    "color: #059669; font-size: 12px;",
  );

  // Focus the query input on page load for immediate typing
  document.getElementById("query-input")?.focus();

  // Verify backend health on load (non-blocking)
  fetch("/health")
    .then((r) => r.json())
    .then((data) => {
      console.log(
        `%c[MCP] Server healthy — ${data.mcp_tool_count} tools registered: ${data.mcp_tools_registered?.join(", ")}`,
        "color: #059669; font-size: 12px;",
      );
    })
    .catch(() => {
      console.warn(
        "[ShopStream] Backend not reachable — make sure uvicorn is running.",
      );
    });
});
