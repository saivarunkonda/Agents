# backend/mcp/mcp_server.py
#
# =============================================================================
# MCP SERVER — Model Context Protocol Implementation
# =============================================================================
#
# The Model Context Protocol (MCP) is an open standard that lets AI agents
# discover and call "tools" (functions) through a well-defined interface.
# Inspired by the official MCP spec, this implementation keeps things simple
# and self-contained so every concept is visible in the code.
#
# CORE MCP CONCEPTS demonstrated here:
#
#   1. TOOL REGISTRY
#      Every capability the agent can use is registered with:
#        • name        – unique identifier (snake_case)
#        • description – human-readable summary for the LLM / agent
#        • parameters  – JSON-Schema-style dict describing inputs
#        • handler     – the Python function that runs the tool
#
#   2. TOOL CALL & RESULT
#      Callers invoke tools by name + arguments.  The server validates the
#      call, executes the handler, and returns a structured result dict
#      that always contains:
#        { "tool": <name>, "success": bool, "result": <payload> }
#
#   3. STATELESS EXECUTION
#      Each tool call is independent — no hidden state is accumulated on the
#      server side.  The agent (client) is responsible for tracking context.
#
# =============================================================================

import json
import os
import re
from typing import Any

# ---------------------------------------------------------------------------
# Helpers — load the mock data files once at import time
# ---------------------------------------------------------------------------

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data")


def _load_json(filename: str) -> Any:
    path = os.path.join(_DATA_DIR, filename)
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# Loaded once and shared across all tool calls (read-only mock database)
PRODUCTS: list[dict] = _load_json("products.json")
USER_PREFS: dict = _load_json("user_preferences.json")


# ---------------------------------------------------------------------------
# MCPServer
# ---------------------------------------------------------------------------


class MCPServer:
    """
    MCP Server that hosts a registry of tools.

    In a production MCP deployment this class would speak the MCP wire
    protocol (JSON-RPC over stdio or HTTP/SSE).  Here we keep it in-process
    so the educational focus stays on the protocol *shape* rather than on
    transport plumbing.

    Usage:
        server = MCPServer()
        result = server.call_tool("search_products", query="headphones")
    """

    def __init__(self):
        # MCP CONCEPT: Tool Registry
        # Each entry describes one capability available to connected agents.
        # The "parameters" field follows JSON Schema conventions so an agent
        # (or an LLM) can understand what arguments are required vs optional.
        self._registry: dict[str, dict] = {}
        self._register_all_tools()

    # ------------------------------------------------------------------
    # Tool Registration
    # ------------------------------------------------------------------

    def _register(
        self,
        name: str,
        description: str,
        parameters: dict,
        handler,
    ) -> None:
        """
        Register a single tool.

        MCP CONCEPT: Each tool advertisement contains the schema the agent
        needs to construct a valid call — similar to an OpenAPI operation.
        """
        self._registry[name] = {
            "name": name,
            "description": description,
            "parameters": parameters,
            "handler": handler,
        }

    def _register_all_tools(self) -> None:
        """Register every shopping tool the agent can call."""

        # ── Tool 1: search_products ───────────────────────────────────────
        self._register(
            name="search_products",
            description=(
                "Search the product catalog using a free-text query. "
                "Optionally filter by category (Electronics, Clothing, Home) "
                "and/or a maximum price. Returns a list of matching products."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Free-text search term (e.g. 'wireless headphones')",
                    },
                    "category": {
                        "type": "string",
                        "description": "Optional category filter: Electronics | Clothing | Home",
                        "enum": ["Electronics", "Clothing", "Home"],
                    },
                    "max_price": {
                        "type": "number",
                        "description": "Optional maximum price filter in USD",
                    },
                },
                "required": ["query"],
            },
            handler=self._tool_search_products,
        )

        # ── Tool 2: get_product_details ───────────────────────────────────
        self._register(
            name="get_product_details",
            description=(
                "Retrieve the full details of a single product by its ID. "
                "Returns all fields including description, rating, and tags."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "product_id": {
                        "type": "string",
                        "description": "The unique product identifier (e.g. 'E001')",
                    }
                },
                "required": ["product_id"],
            },
            handler=self._tool_get_product_details,
        )

        # ── Tool 3: check_inventory ───────────────────────────────────────
        self._register(
            name="check_inventory",
            description=(
                "Check the current stock level and availability status for a "
                "given product. Returns stock count and a human-readable "
                "availability label."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "product_id": {
                        "type": "string",
                        "description": "The unique product identifier",
                    }
                },
                "required": ["product_id"],
            },
            handler=self._tool_check_inventory,
        )

        # ── Tool 4: get_user_preferences ─────────────────────────────────
        self._register(
            name="get_user_preferences",
            description=(
                "Fetch a user's saved preferences, budget, preferred brands, "
                "purchase history, and wishlist. Used to personalise search "
                "results and recommendations."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "user_id": {
                        "type": "string",
                        "description": "The user identifier (e.g. 'user123')",
                    }
                },
                "required": ["user_id"],
            },
            handler=self._tool_get_user_preferences,
        )

        # ── Tool 5: compare_products ──────────────────────────────────────
        self._register(
            name="compare_products",
            description=(
                "Generate a side-by-side comparison of two or more products. "
                "Returns a structured comparison dict with price differences, "
                "rating comparison, stock status, and a feature matrix."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "product_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of product IDs to compare (2–4 items)",
                    }
                },
                "required": ["product_ids"],
            },
            handler=self._tool_compare_products,
        )

        # ── Tool 6: get_recommendations ───────────────────────────────────
        self._register(
            name="get_recommendations",
            description=(
                "Return the top 3 recommended products for a user, based on "
                "their preferences, budget, and the current search query. "
                "Each recommendation includes a score and explanation."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "user_id": {
                        "type": "string",
                        "description": "The user identifier",
                    },
                    "query": {
                        "type": "string",
                        "description": "The user's current search query",
                    },
                },
                "required": ["user_id", "query"],
            },
            handler=self._tool_get_recommendations,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_tools(self) -> list[dict]:
        """
        MCP CONCEPT: Tool Discovery
        Return advertisement dicts for every registered tool (minus the
        internal handler reference so they are safely serialisable).
        """
        return [
            {k: v for k, v in tool.items() if k != "handler"}
            for tool in self._registry.values()
        ]

    def call_tool(self, name: str, **kwargs) -> dict:
        """
        MCP CONCEPT: Tool Invocation
        Execute a registered tool by name, passing keyword arguments.
        Always returns a structured result envelope:
            { "tool": str, "success": bool, "result": any }
        """
        if name not in self._registry:
            return {
                "tool": name,
                "success": False,
                "result": f"Unknown tool '{name}'. Available tools: {list(self._registry.keys())}",
            }

        handler = self._registry[name]["handler"]
        try:
            result = handler(**kwargs)
            return {"tool": name, "success": True, "result": result}
        except TypeError as exc:
            return {
                "tool": name,
                "success": False,
                "result": f"Invalid arguments for tool '{name}': {exc}",
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "tool": name,
                "success": False,
                "result": f"Tool '{name}' raised an error: {exc}",
            }

    # ------------------------------------------------------------------
    # Tool Handlers — the actual business logic
    # ------------------------------------------------------------------

    def _tool_search_products(
        self,
        query: str,
        category: str | None = None,
        max_price: float | None = None,
    ) -> dict:
        """
        Search products by free-text query with optional filters.

        Matching strategy:
          1. Tokenise the query into lowercase words.
          2. Score each product by how many query tokens appear in its
             name, description, or tags.
          3. Apply category and price filters.
          4. Return all products with score > 0, sorted by score desc.
        """
        tokens = re.findall(r"\w+", query.lower())

        scored: list[tuple[int, dict]] = []
        for product in PRODUCTS:
            # Category filter (case-insensitive exact match)
            if category and product["category"].lower() != category.lower():
                continue

            # Price filter
            if max_price is not None and product["price"] > max_price:
                continue

            # Relevance scoring — count token hits across searchable fields
            haystack = " ".join(
                [
                    product["name"].lower(),
                    product["description"].lower(),
                    " ".join(product["tags"]),
                    product["category"].lower(),
                ]
            )
            score = sum(1 for tok in tokens if tok in haystack)
            if score > 0:
                scored.append((score, product))

        # Sort by relevance score descending, then by rating for ties
        scored.sort(key=lambda x: (x[0], x[1]["rating"]), reverse=True)
        results = [p for _, p in scored]

        return {
            "query": query,
            "filters": {"category": category, "max_price": max_price},
            "total_found": len(results),
            "products": results,
        }

    def _tool_get_product_details(self, product_id: str) -> dict:
        """Return full details for a single product."""
        for product in PRODUCTS:
            if product["id"] == product_id:
                return {"found": True, "product": product}
        return {
            "found": False,
            "product": None,
            "error": f"Product '{product_id}' not found",
        }

    def _tool_check_inventory(self, product_id: str) -> dict:
        """Return stock level and a descriptive availability label."""
        for product in PRODUCTS:
            if product["id"] == product_id:
                stock = product["stock"]
                if stock == 0:
                    availability = "Out of Stock"
                elif stock <= 5:
                    availability = "Low Stock — only a few left!"
                elif stock <= 15:
                    availability = "Limited Stock"
                else:
                    availability = "In Stock"

                return {
                    "product_id": product_id,
                    "product_name": product["name"],
                    "stock": stock,
                    "availability": availability,
                    "in_stock": stock > 0,
                }

        return {
            "product_id": product_id,
            "stock": 0,
            "availability": "Product Not Found",
            "in_stock": False,
        }

    def _tool_get_user_preferences(self, user_id: str) -> dict:
        """Return a user's full preference profile."""
        prefs = USER_PREFS.get(user_id)
        if prefs is None:
            # Return a safe default for unknown users
            return {
                "found": False,
                "user_id": user_id,
                "preferences": {
                    "budget_max": None,
                    "preferred_brands": [],
                    "preferred_categories": [],
                    "past_purchases": [],
                    "wishlist": [],
                    "notes": "No profile found — using defaults",
                },
            }
        return {"found": True, "user_id": user_id, "preferences": prefs}

    def _tool_compare_products(self, product_ids: list[str]) -> dict:
        """
        Build a side-by-side comparison of 2–4 products.

        The returned dict contains:
          • products    – list of full product objects
          • comparison  – field-by-field comparison matrix
          • summary     – quick-pick recommendation
        """
        if len(product_ids) < 2:
            return {"error": "Please provide at least 2 product IDs to compare"}
        if len(product_ids) > 4:
            return {"error": "Comparison limited to 4 products at a time"}

        found: list[dict] = []
        missing: list[str] = []
        for pid in product_ids:
            match = next((p for p in PRODUCTS if p["id"] == pid), None)
            if match:
                found.append(match)
            else:
                missing.append(pid)

        if missing:
            return {"error": f"Products not found: {missing}"}

        # Build comparison matrix
        comparison = {
            "price": {p["id"]: f"${p['price']:.2f}" for p in found},
            "rating": {p["id"]: p["rating"] for p in found},
            "stock": {p["id"]: p["stock"] for p in found},
            "category": {p["id"]: p["category"] for p in found},
            "tags": {p["id"]: p["tags"] for p in found},
        }

        # Cheapest and highest-rated picks
        cheapest = min(found, key=lambda p: p["price"])
        best_rated = max(found, key=lambda p: p["rating"])

        # Value score: rating / (price / 100) — higher is better value
        for p in found:
            p["_value_score"] = round(p["rating"] / (p["price"] / 100), 4)
        best_value = max(found, key=lambda p: p["_value_score"])
        # Clean up temporary key
        for p in found:
            del p["_value_score"]

        return {
            "products": found,
            "comparison": comparison,
            "summary": {
                "cheapest": {
                    "id": cheapest["id"],
                    "name": cheapest["name"],
                    "price": cheapest["price"],
                },
                "best_rated": {
                    "id": best_rated["id"],
                    "name": best_rated["name"],
                    "rating": best_rated["rating"],
                },
                "best_value": {"id": best_value["id"], "name": best_value["name"]},
            },
        }

    def _tool_get_recommendations(self, user_id: str, query: str) -> dict:
        """
        Return the top 3 products recommended for a user given a query.

        Scoring model (simple but transparent):
          • +3  if a preferred brand appears in the product name
          • +2  if a preferred category matches the product category
          • +2  relevance score from query token matching (capped at 2)
          • +1  if the product is in the user's wishlist
          • -10 if the product is already owned (past purchase)
          • -5  if the product price exceeds the user's budget_max
          •      rating is used as a tiebreaker
        """
        tokens = re.findall(r"\w+", query.lower())

        prefs_result = self._tool_get_user_preferences(user_id)
        prefs = prefs_result["preferences"]

        budget_max = prefs.get("budget_max")
        preferred_brands = [b.lower() for b in prefs.get("preferred_brands", [])]
        preferred_categories = [
            c.lower() for c in prefs.get("preferred_categories", [])
        ]
        past_purchases = set(prefs.get("past_purchases", []))
        wishlist = set(prefs.get("wishlist", []))

        scored: list[tuple[float, dict, str]] = []  # (score, product, reason)
        for product in PRODUCTS:
            score = 0.0
            reasons: list[str] = []

            # Brand affinity
            for brand in preferred_brands:
                if brand in product["name"].lower():
                    score += 3
                    reasons.append(f"matches preferred brand '{brand.title()}'")
                    break

            # Category affinity
            if product["category"].lower() in preferred_categories:
                score += 2
                reasons.append(f"preferred category '{product['category']}'")

            # Query relevance (score capped at 2)
            haystack = " ".join(
                [
                    product["name"].lower(),
                    " ".join(product["tags"]),
                    product["description"].lower(),
                ]
            )
            relevance = min(sum(1 for tok in tokens if tok in haystack), 2)
            score += relevance
            if relevance > 0:
                reasons.append("matches your search")

            # Wishlist bonus
            if product["id"] in wishlist:
                score += 1
                reasons.append("on your wishlist")

            # Already purchased — deprioritise strongly
            if product["id"] in past_purchases:
                score -= 10
                reasons.append("already purchased")

            # Over budget — penalise
            if budget_max is not None and product["price"] > budget_max:
                score -= 5
                reasons.append(f"over your ${budget_max} budget")

            reason_str = "; ".join(reasons) if reasons else "general match"
            scored.append((score, product, reason_str))

        # Sort by score desc, rating as tiebreaker
        scored.sort(key=lambda x: (x[0], x[1]["rating"]), reverse=True)

        top3 = scored[:3]
        recommendations = [
            {
                "rank": idx + 1,
                "product": product,
                "score": round(score, 2),
                "reason": reason,
            }
            for idx, (score, product, reason) in enumerate(top3)
        ]

        return {
            "user_id": user_id,
            "query": query,
            "budget_max": budget_max,
            "recommendations": recommendations,
        }
