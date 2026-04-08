"""
main.py — TravelMind Demo Runner
==================================
Runs three end-to-end scenarios that demonstrate the full TravelMind
Agent2Agent (A2A) + Agent Payments Protocol (AP2) pipeline.

Scenario 1 — Interactive User Purchase
    User says: "Book me a trip to Paris, 3 nights, budget $2000, Sep 15"
    Shows the complete 10-step flow:
      AP2  → Intent Mandate creation
      A2A  → FlightAgent, HotelAgent, CarRentalAgent discovery & task dispatch
      AP2  → Cart Mandate creation + verify_mandate_chain()
      User → Interactive cart review & approval
      A2A  → PaymentAgent processes payment
      AP2  → Transaction receipt + mandate fulfilment

Scenario 2 — Delegated / Autonomous Purchase
    A pre-authorised Intent Mandate for a London trip is created up front.
    The orchestrator books autonomously without pausing for user interaction.
    Demonstrates the AP2 "delegated purchase" pattern where an agent acts
    within pre-signed spending constraints with no live human in the loop.

Scenario 3 — Budget Exceeded (AP2 Mandate Rejection)
    User requests Paris with only $800 budget.
    Cheapest available Paris flight + hotel totals more than $800, so the
    AP2 verify_mandate_chain() check blocks the transaction.
    Demonstrates how the AP2 spending cap acts as a hard guardrail.

Usage
-----
    cd Agent/project3_travelmind
    python main.py

No external dependencies required — uses Python standard library only.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Windows UTF-8 fix — box-drawing and emoji characters require UTF-8 output.
# On Windows the default console codec is cp1252 which cannot encode them.
# ---------------------------------------------------------------------------
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        # Python < 3.7 fallback
        import io

        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

# ---------------------------------------------------------------------------
# Path bootstrapping
# ---------------------------------------------------------------------------
# Insert the project root onto sys.path so that the a2a, ap2, and agents
# packages resolve correctly when main.py is executed from any working
# directory.
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Imports (after path bootstrap)
# ---------------------------------------------------------------------------
from a2a.a2a_protocol import get_registry
from agents.car_rental_agent import CarRentalAgent
from agents.flight_agent import FlightAgent
from agents.hotel_agent import HotelAgent
from agents.orchestrator_agent import OrchestratorAgent
from agents.payment_agent import PaymentAgent
from ap2.ap2_protocol import AP2Protocol

# ============================================================================
# Helpers
# ============================================================================


def _banner(title: str, width: int = 72) -> None:
    """Print a prominent section banner to stdout."""
    bar = "#" * width
    pad = " " * ((width - len(title) - 2) // 2)
    print(f"\n{bar}")
    print(f"#{pad} {title} {pad}#")
    print(f"{bar}\n")


def _divider(char: str = "─", width: int = 72) -> None:
    """Print a thin horizontal divider."""
    print(char * width)


def _print_result(result: dict, scenario_num: int) -> None:
    """Print a structured final result block for a completed scenario."""
    _divider("=")
    status = "[OK]  SUCCESS" if result.get("success") else "[!!] FAILED"
    print(f"  SCENARIO {scenario_num} RESULT -- {status}")
    _divider("-")

    # Core fields
    print(f"  User ID         : {result.get('user_id', 'N/A')}")
    print(f"  Destination     : {result.get('destination', 'N/A')}")
    print(f"  Nights          : {result.get('nights', 'N/A')}")
    print(f"  Budget          : ${result.get('budget', 0):.2f}")
    print(f"  Total Cost      : ${result.get('total_cost', 0):.2f}")
    print(f"  Budget Headroom : ${result.get('budget_headroom', 0):.2f}")

    # Booking components
    if result.get("flight"):
        fl = result["flight"]
        print(
            f"  [Flight]        : {fl.get('airline')} "
            f"{fl.get('from', 'NYC')} -> {fl.get('to', '?')} "
            f"on {fl.get('date', '?')} -- ${fl.get('price', 0):.2f}"
        )
    if result.get("hotel"):
        ht = result["hotel"]
        print(
            f"  [Hotel]         : {ht.get('name')} ({ht.get('stars')} stars) -- "
            f"${ht.get('total_cost', 0):.2f} total"
        )
    if result.get("car"):
        cr = result["car"]
        print(
            f"  [Car]           : {cr.get('company')} {cr.get('type')} -- "
            f"${cr.get('total_cost', 0):.2f} total"
        )

    # AP2 mandate IDs
    if result.get("intent_mandate_id"):
        print(f"  [IntentMandate] : {result['intent_mandate_id']}")
    if result.get("cart_mandate_id"):
        print(f"  [CartMandate]   : {result['cart_mandate_id']}")

    # Payment receipt
    if result.get("transaction_id"):
        print(f"  [TXN ID]        : {result['transaction_id']}")
    if result.get("receipt_id"):
        print(f"  [Receipt ID]    : {result['receipt_id']}")

    # Error (if any)
    if result.get("error"):
        print(f"\n  [FAIL] Error    : {result['error']}")

    # Steps completed
    steps = result.get("steps_completed", [])
    print(f"\n  Steps Completed ({len(steps)}):")
    for i, step in enumerate(steps, 1):
        icon = "[FAIL]" if "FAILED" in step else "[OK]  "
        print(f"    {icon} [{i:02d}] {step}")

    _divider("=")


def _reset_registry() -> None:
    """
    Clear the shared A2A registry between scenarios so each scenario starts
    with a fresh registry state.  Agents self-register when instantiated, so
    re-creating the agents in each scenario repopulates the registry cleanly.
    """
    registry = get_registry()
    # Unregister all currently registered agents
    for card in list(registry.list_all()):
        registry.unregister(card.agent_id)


def _create_agents(verbose: bool = True):
    """
    Instantiate all specialist agents (they self-register in the shared
    A2ARegistry on creation) and return the OrchestratorAgent.

    Parameters
    ----------
    verbose : bool
        Pass False to suppress agent startup log lines (useful for cleaner
        demo output in scenarios where the startup chatter is distracting).

    Returns
    -------
    OrchestratorAgent
    """
    FlightAgent(verbose=verbose)
    HotelAgent(verbose=verbose)
    CarRentalAgent(verbose=verbose)
    PaymentAgent(verbose=verbose)
    orchestrator = OrchestratorAgent(verbose=True)
    return orchestrator


# ============================================================================
# Scenario 1 — Interactive User Purchase (Paris, 3 nights, $2000 budget)
# ============================================================================


def run_scenario_1() -> dict:
    """
    Scenario 1: Full interactive booking flow.

    User request: "Book me a trip to Paris, 3 nights, budget $2000, Sep 15"

    Demonstrates
    ------------
    • [AP2]  Intent Mandate created FIRST with $2000 spending cap.
    • [A2A]  OrchestratorAgent discovers FlightAgent, HotelAgent, CarRentalAgent
             via the A2A Registry (no hard-coded agent references).
    • [A2A]  AUTH + TASK dispatched to each specialist agent in sequence.
    • [AP2]  Cart Mandate assembled from selected options.
    • [AP2]  verify_mandate_chain() confirms total < $2000 ✓
    • [USER] Cart is displayed and user interactively approves.
    • [A2A]  PaymentAgent receives the approved CartMandate.
    • [AP2]  PaymentProcessor charges the transaction.
    • [AP2]  IntentMandate marked FULFILLED; receipt returned.

    Expected outcome: SUCCESS — Norse Atlantic flight ($480) + Hotel Le Marais
    ($540 for 3 nights) = $1020 total, well within the $2000 budget.
    """
    _banner("SCENARIO 1 — Interactive User Purchase")
    print('  Request : "Book me a trip to Paris, 3 nights, budget $2000, Sep 15"')
    print("  Mode    : Interactive (user reviews & approves cart)")
    print("  Expected: SUCCESS — Paris trip booked, payment processed")
    print()

    _reset_registry()
    orchestrator = _create_agents(verbose=True)

    print()
    _divider("─")
    print("  [SCENARIO 1] Starting plan_trip() ...\n")

    result = orchestrator.plan_trip(
        user_id="alice@travelmind.example",
        destination="Paris",
        nights=3,
        budget=2000.00,
        travel_date="2025-09-15",
        include_car=False,
        delegated=False,  # interactive: show cart and simulate approval
        origin="NYC",
        constraints={
            "min_hotel_stars": 3,  # require at least 3-star hotel
            "prefer_direct_flights": True,
        },
    )

    _print_result(result, scenario_num=1)
    return result


# ============================================================================
# Scenario 2 — Delegated / Autonomous Purchase (London, 2 nights, $1500)
# ============================================================================


def run_scenario_2() -> dict:
    """
    Scenario 2: Fully autonomous (delegated) purchase.

    A corporate travel manager has pre-authorised a London trip via a signed
    AP2 Intent Mandate.  The OrchestratorAgent books the trip autonomously —
    no live user approval interaction is needed.

    Demonstrates
    ------------
    • [AP2]  Intent Mandate is created with delegated=True flag, representing
             a pre-authorised travel policy grant.
    • [A2A]  Full discovery → auth → task dispatch for FlightAgent, HotelAgent,
             and CarRentalAgent (car rental is included this time).
    • [AP2]  verify_mandate_chain() runs as normal — the AP2 contract still
             enforces the spending cap and destination even when there is no
             live human present.
    • [AP2]  user_approve_cart() uses "delegated" approval method — the signed
             IntentMandate itself acts as the pre-authorisation.
    • [A2A]  PaymentAgent processes the charge autonomously.
    • [AP2]  Full receipt returned; agent operated within its mandate ✓

    Expected outcome: SUCCESS — Virgin Atlantic flight ($380) + Premier Inn
    ($220 for 2 nights) + Enterprise Economy car ($80 for 2 days) = $680 total,
    well within the $1500 budget.

    This scenario illustrates how AP2 enables safe autonomous spending: the
    agent has freedom to choose within the pre-agreed constraints, but cannot
    exceed the spending cap even if it wanted to.
    """
    _banner("SCENARIO 2 — Delegated / Autonomous Purchase")
    print('  Request : "Pre-authorised London business trip, 2 nights, budget $1500"')
    print("  Mode    : Delegated (agent books autonomously — no user interaction)")
    print("  Includes: Car rental add-on")
    print("  Expected: SUCCESS — London trip + car booked autonomously")
    print()

    _reset_registry()
    orchestrator = _create_agents(verbose=True)

    print()
    _divider("─")
    print("  [SCENARIO 2] Starting plan_trip() ...\n")

    result = orchestrator.plan_trip(
        user_id="bob@corp-travel.example",
        destination="London",
        nights=2,
        budget=1500.00,
        travel_date="2025-09-15",
        include_car=True,  # add car rental to the trip
        delegated=True,  # autonomous: no live user approval needed
        origin="NYC",
        constraints={
            "min_hotel_stars": 3,
            "include_car_rental": True,
        },
    )

    _print_result(result, scenario_num=2)
    return result


# ============================================================================
# Scenario 3 — Budget Exceeded (AP2 Mandate Blocks Overspend)
# ============================================================================


def run_scenario_3() -> dict:
    """
    Scenario 3: AP2 spending cap enforcement.

    User requests a Paris trip but sets an unrealistically low budget of $800.
    The cheapest available Paris option is:
      Norse Atlantic flight: $480
      Ibis Paris Centre (3 nights): $285
      Total: $765

    Wait — $765 < $800, so this should actually succeed!
    Let's make it truly fail by using a budget of $500 so that no
    flight + hotel combination is possible within the cap.

    Actually looking at the data:
      Cheapest Paris flight: $480 (Norse Atlantic)
      Cheapest Paris hotel (3 nights): 95 * 3 = $285 (Ibis)
      Total: $765

    With budget $700, $765 > $700 so the combination fails.

    Demonstrates
    ------------
    • [AP2]  Intent Mandate is created with a $700 spending cap.
    • [A2A]  FlightAgent and HotelAgent are queried normally.
    • [ORCH] Budget selection logic tries every flight × hotel combination.
             All combinations exceed $700 — cheapest is $765.
    • [AP2]  The orchestrator refuses to create a CartMandate that would
             violate the Intent Mandate's spending cap.
    • No payment is attempted; a clear error is returned describing the
             shortfall.

    This shows that AP2's Intent Mandate acts as a hard, upfront commitment
    that prevents agents from spending more than the user authorised —
    even if they could technically find options above that price.
    """
    _banner("SCENARIO 3 — Budget Exceeded (AP2 Mandate Enforcement)")
    print('  Request : "Book me a trip to Paris, 3 nights, budget $700"')
    print("  Mode    : Interactive (will be rejected before payment)")
    print("  Expected: FAILURE — cheapest Paris trip is $765, exceeds $700 cap")
    print("  AP2 Role: Intent Mandate spending_cap=$700 blocks overspend")
    print()

    _reset_registry()
    orchestrator = _create_agents(verbose=True)

    print()
    _divider("─")
    print("  [SCENARIO 3] Starting plan_trip() ...\n")

    result = orchestrator.plan_trip(
        user_id="carol@travelmind.example",
        destination="Paris",
        nights=3,
        budget=700.00,  # intentionally too low — cheapest combo = $765
        travel_date="2025-09-15",
        include_car=False,
        delegated=False,
        origin="NYC",
        constraints={},
    )

    _print_result(result, scenario_num=3)
    return result


# ============================================================================
# Architecture overview printout
# ============================================================================


def _print_architecture() -> None:
    """Print the ASCII architecture diagram from the README."""
    print("""
+========================================================================+
|                   TravelMind -- System Architecture                    |
+========================================================================+
|                                                                        |
|  User Request ("Book Paris, 3 nights, $2000")                          |
|      |                                                                 |
|      v                                                                 |
|  OrchestratorAgent                                                     |
|      |                                                                 |
|      +--[AP2]--> Create Intent Mandate                                 |
|      |            spending_cap=$2000, dest=Paris, dates=Sep15-18       |
|      |            signature=sha256(...) [OK]                           |
|      |                                                                 |
|      +--[A2A DISCOVERY]--> A2A Registry                                |
|      |    finds: FlightAgent, HotelAgent, CarRentalAgent, PaymentAgent |
|      |                                                                 |
|      +--[A2A AUTH+TASK]--> FlightAgent                                 |
|      |    token  : eyJhbGc...                                          |
|      |    intent : find_flights {dest=Paris, date=2025-09-15}          |
|      |    response: [FL003=$480, FL001=$650, FL002=$820]               |
|      |                                                                 |
|      +--[A2A AUTH+TASK]--> HotelAgent                                  |
|      |    token  : eyJhbGc...                                          |
|      |    intent : find_hotels {city=Paris, nights=3}                  |
|      |    response: [HT001=$540, HT002=$285]                           |
|      |                                                                 |
|      +--[A2A AUTH+TASK]--> CarRentalAgent  (optional)                  |
|      |    token  : eyJhbGc...                                          |
|      |    intent : find_cars {city=Paris, days=3}                      |
|      |    response: [CR001=$135, CR002=$195]                           |
|      |                                                                 |
|      +--[AP2]--> Create Cart Mandate                                   |
|      |            FL003=$480 + HT001=$540 = $1020                      |
|      |            verify_mandate_chain() -> $1020 < $2000 [OK]         |
|      |            signature=sha256(cart_id|items|total|...) [OK]       |
|      |                                                                 |
|      +--[User Approval]--> Cart confirmed by user                      |
|      |    CartMandate.user_approved = True                             |
|      |    CartMandate.status = "approved"                              |
|      |                                                                 |
|      +--[A2A AUTH+TASK]--> PaymentAgent                                |
|             token  : eyJhbGc...                                        |
|             intent : process_payment {cart_mandate, intent_mandate}    |
|             |                                                          |
|             +--[AP2]--> verify_mandate_chain() (2nd independent check) |
|             |            [OK]                                          |
|             |                                                          |
|             +--[AP2]--> PaymentProcessor.process_payment(cart_mandate) |
|                          Pre-charge checks (5/5 passed):               |
|                            1. user_approved == True         [OK]       |
|                            2. status == "approved"          [OK]       |
|                            3. signature integrity           [OK]       |
|                            4. amount sanity ($0<$1020<$50k) [OK]       |
|                            5. item integrity (2 items)      [OK]       |
|                          Transaction TXN-XXXX: $1020.00 charged [OK]   |
|                          IntentMandate -> FULFILLED [OK]               |
+========================================================================+
""")


def _print_ap2_concepts() -> None:
    """Print a brief explanation of the AP2 two-mandate model."""
    print("""
+--------------------------------------------------------------------------+
|                     AP2 Two-Mandate Model                                |
+--------------------------------------------------------------------------+
|                                                                          |
|  1. IntentMandate  (created FIRST, before any search begins)             |
|     +------------------------------------------------------------------+ |
|     |  mandate_id      : INTENT-A1B2C3D4E5F6                          | |
|     |  user_id         : alice@travelmind.example                      | |
|     |  intent          : "Book round trip to Paris, 3 nights, <$2k"   | |
|     |  spending_cap    : $2000.00  <- HARD LIMIT, cannot be changed    | |
|     |  destination     : Paris                                         | |
|     |  travel_dates    : {outbound: 2025-09-15, return: 2025-09-18}   | |
|     |  constraints     : {min_hotel_stars: 3}                          | |
|     |  signature       : sha256(mandate_id|user_id|cap|dest|...)       | |
|     |  status          : active -> fulfilled (after payment)           | |
|     +------------------------------------------------------------------+ |
|                              |                                           |
|                    ... agents search for options ...                     |
|                              |                                           |
|  2. CartMandate  (created AFTER agents return concrete options)          |
|     +------------------------------------------------------------------+ |
|     |  cart_id         : CART-9F8E7D6C5B4A                            | |
|     |  intent_mandate_id: INTENT-A1B2C3D4E5F6  <- links to intent     | |
|     |  items:                                                          | |
|     |    [0] FLIGHT  Norse Atlantic NYC->Paris   $480.00               | |
|     |    [1] HOTEL   Hotel Le Marais (4*) 3 nts  $540.00               | |
|     |  total_amount   : $1020.00                                       | |
|     |  signature      : sha256(cart_id|items|total|...)                | |
|     |  user_approved  : True (after user review)                       | |
|     |  status         : pending_approval -> approved -> charged        | |
|     +------------------------------------------------------------------+ |
|                              |                                           |
|            verify_mandate_chain() checks:                                |
|              [OK] cart.intent_mandate_id == intent.mandate_id           |
|              [OK] cart.user_id == intent.user_id                        |
|              [OK] cart.total ($1020) <= intent.spending_cap ($2000)     |
|              [OK] destination "paris" found in cart items               |
|              [OK] intent.status == "active"                              |
|              [OK] cart signature integrity                               |
|              [OK] min_hotel_stars constraint satisfied                   |
+--------------------------------------------------------------------------+
""")


# ============================================================================
# Main entry point
# ============================================================================


def main() -> None:
    """
    Run all three TravelMind demo scenarios in sequence.

    Each scenario:
      1. Resets the A2A registry (clean slate).
      2. Instantiates all agents (they self-register in the registry).
      3. Calls OrchestratorAgent.plan_trip() with scenario-specific params.
      4. Prints the structured result.

    A final summary table is printed at the end showing pass/fail for each
    scenario and the key figures (cost, transaction ID, etc.).
    """
    # ----------------------------------------------------------------
    # Welcome banner
    # ----------------------------------------------------------------
    print("\n" + "#" * 72)
    print("#" + " " * 70 + "#")
    print("#" + "  TravelMind - Autonomous Travel Booking Agent".center(70) + "#")
    print(
        "#"
        + "  A2A (Agent2Agent) + AP2 (Agent Payments Protocol) Demo".center(70)
        + "#"
    )
    print("#" + " " * 70 + "#")
    print("#" * 72 + "\n")

    print("  TravelMind demonstrates an autonomous travel booking system where")
    print("  multiple AI agents collaborate using two cutting-edge protocols:\n")
    print("  • A2A  (Agent2Agent Protocol)")
    print("    Agents discover each other via a registry, authenticate with")
    print("    JWT-like tokens, and exchange structured Task/Response messages.\n")
    print("  • AP2  (Agent Payments Protocol)")
    print("    A two-mandate model (IntentMandate + CartMandate) ensures agents")
    print("    can never overspend or substitute items without a verifiable,")
    print("    cryptographically-signed audit trail.\n")

    _print_architecture()
    _print_ap2_concepts()

    print("  Running 3 scenarios:\n")
    print("    Scenario 1 — Interactive purchase: Paris, 3 nights, $2000 budget")
    print("    Scenario 2 — Delegated purchase:  London, 2 nights, $1500 + car")
    print("    Scenario 3 — Budget exceeded:     Paris, 3 nights, $700 (BLOCKED)\n")
    _divider("═" * 72)

    # ----------------------------------------------------------------
    # Run scenarios
    # ----------------------------------------------------------------
    results = {}

    try:
        input("\n  Press ENTER to start Scenario 1 (or Ctrl+C to skip)...")
    except (EOFError, KeyboardInterrupt):
        print()  # newline after ^C

    results[1] = run_scenario_1()

    try:
        input("\n  Press ENTER to start Scenario 2 (or Ctrl+C to skip)...")
    except (EOFError, KeyboardInterrupt):
        print()

    results[2] = run_scenario_2()

    try:
        input("\n  Press ENTER to start Scenario 3 (or Ctrl+C to skip)...")
    except (EOFError, KeyboardInterrupt):
        print()

    results[3] = run_scenario_3()

    # ----------------------------------------------------------------
    # Final summary
    # ----------------------------------------------------------------
    _banner("FINAL SUMMARY — All Scenarios")

    header = f"  {'#':<4} {'Scenario':<38} {'Result':<12} {'Cost':>8}  {'TXN ID'}"
    _divider("-")
    print(header)
    _divider("-")

    scenario_labels = {
        1: "Paris, 3 nights, $2000 (interactive)",
        2: "London, 2 nights, $1500 + car (delegated)",
        3: "Paris, 3 nights, $700  (budget exceeded)",
    }

    all_passed = True
    for num in (1, 2, 3):
        r = results[num]
        ok = r.get("success", False)
        status = "[OK]  " if ok else "[FAIL]"
        cost = f"${r.get('total_cost', 0):.2f}"
        txn = r.get("transaction_id") or r.get("error", "N/A")
        txn_short = (txn[:24] + "...") if txn and len(txn) > 27 else (txn or "N/A")
        print(
            f"  {num:<4} {scenario_labels[num]:<38} {status:<12} {cost:>8}  {txn_short}"
        )

        # Scenario 3 is *expected* to fail — don't count it against all_passed
        if num != 3 and not ok:
            all_passed = False

    # Scenario 3 should have failed
    if results[3].get("success"):
        print("\n  [!] WARNING: Scenario 3 was expected to FAIL but returned SUCCESS.")
        print("      This may indicate that the budget constraint is not enforced.")
        all_passed = False
    else:
        print(
            "\n  [OK] Scenario 3 correctly blocked the overspend (AP2 mandate enforced)."
        )

    _divider("-")

    if all_passed:
        print("\n  [OK] All scenarios behaved as expected!")
        print("       A2A protocol: agent discovery, auth, and task dispatch [OK]")
        print("       AP2 protocol: mandate creation, chain verification, payment [OK]")
        print("       Budget guard: spending cap enforcement working correctly [OK]")
    else:
        print("\n  [!] One or more scenarios did not behave as expected.")
        print("      Check the output above for details.")

    print()
    _divider("═")
    print()


if __name__ == "__main__":
    main()
