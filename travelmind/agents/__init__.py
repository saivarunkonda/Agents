"""
Agents Package
==============
Contains all autonomous agents that participate in the TravelMind A2A + AP2 system.

Agents
------
OrchestratorAgent  : Master coordinator — receives user trip requests, creates
                     Intent Mandates (AP2), discovers and dispatches tasks to
                     specialist agents via A2A, assembles the cart, obtains
                     user approval, and delegates payment to PaymentAgent.

FlightAgent        : A2A server — searches mock flight data by destination,
                     date, and budget; returns ranked flight options; confirms
                     bookings by flight ID.

HotelAgent         : A2A server — searches mock hotel data by city, budget,
                     availability, and star rating; returns ranked hotel options;
                     confirms bookings by hotel ID.

CarRentalAgent     : A2A server — searches mock car rental data by city and
                     budget; returns available car options; confirms rentals
                     by car ID.  Acts as an optional add-on to the trip.

PaymentAgent       : A2A server + AP2 client — receives a CartMandate from the
                     orchestrator, runs verify_mandate_chain(), applies user
                     approval, invokes PaymentProcessor, and returns the
                     transaction receipt.

All agents self-register in the shared A2A registry on instantiation so the
OrchestratorAgent can discover them via capability strings without needing
hard-coded references.
"""

from .car_rental_agent import CarRentalAgent
from .flight_agent import FlightAgent
from .hotel_agent import HotelAgent
from .orchestrator_agent import OrchestratorAgent
from .payment_agent import PaymentAgent

__all__ = [
    "OrchestratorAgent",
    "FlightAgent",
    "HotelAgent",
    "CarRentalAgent",
    "PaymentAgent",
]
