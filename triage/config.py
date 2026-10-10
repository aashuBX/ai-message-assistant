"""Static configuration: brands, queues, SLAs, limits and model prices.

Everything a business owner might reasonably want to change without touching
logic lives here or in the policy table in policy.py.
"""

from __future__ import annotations

PROMPT_VERSION = "v1"

# LLM provider: Groq, on a free API key (practice project, not production).
DEFAULT_MODEL = "openai/gpt-oss-120b"
DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"

MAX_TEXT_CHARS = 4000          # longer messages are truncated (and flagged) before the LLM call
LLM_TIMEOUT_S = 30.0
LLM_MAX_ATTEMPTS = 3           # per message, for transient errors (429 / 5xx / timeout)
DEFAULT_CONCURRENCY = 8        # Groq's free plan (8k tokens a minute) needs --concurrency 1

# Brand facts below are inferred from the sample data (INR, Hinglish, Diwali, +91 numbers for
# Vitalis and Hair Studio; Pearson, Air Canada, 647 numbers for Voyage) and stated in the README.
BRANDS = {
    "vitalis-wellness": {
        "description": (
            "supplements and wellness products sold online in India (INR). Order IDs look like "
            "VW-48812. Sells monthly product subscriptions; also sells wholesale."
        ),
        "phone_region": "IN",
        "queues": {
            "support": "vitalis_support",
            "billing": "vitalis_billing",
            "sales": "vitalis_sales",
            "health": "vitalis_health",
            "qa": "vitalis_qa",
        },
    },
    "hair-studio": {
        "description": (
            "hair salon in India: appointments, colour, treatments, memberships. Customers often "
            "write Hinglish (Hindi in Latin script mixed with English)."
        ),
        "phone_region": "IN",
        "queues": {"support": "hair_frontdesk"},
    },
    "voyage-travel": {
        "description": (
            "travel agency; customers are mostly in Canada (Toronto) travelling to and from India. "
            "Booking refs look like VYG9K2M."
        ),
        "phone_region": "CA",
        "queues": {
            "support": "voyage_agents",
            "sales": "voyage_sales",
            "emergency": "voyage_emergency",
        },
    },
}

KNOWN_CHANNELS = {"whatsapp", "email", "instagram"}

FALLBACK_QUEUE = "general_support"   # unknown or missing brand
SHARED_QUEUES = {"trust_safety", "finance_ap", "hr"}

PRIORITIES = ("P1", "P2", "P3", "P4")
SLA = {
    "P1": "15 minutes, 24/7",
    "P2": "2 hours",
    "P3": "24 hours (inside the WhatsApp/Instagram reply window)",
    "P4": "3 business days",
}

# Format patterns for IDs the brands use. High precision on purpose: the LLM extracts
# everything; these are an independent cross-check, not the primary extractor.
ID_PATTERNS = {
    "order_id": r"\bVW-?\d{4,6}\b",
    "booking_ref": r"\bVYG[A-Z0-9]{4}\b",
}

# USD per 1M tokens (input, cached input, output): Groq paid list prices, used only to
# estimate production cost; the free key this project runs on costs nothing.
# Source: https://console.groq.com/docs/model/openai/gpt-oss-120b (and .../gpt-oss-20b)
PRICES_CHECKED_ON = "2026-10-10"
PRICES = {
    "openai/gpt-oss-120b": (0.15, 0.075, 0.60),
    "openai/gpt-oss-20b": (0.075, 0.037, 0.30),
}


def price_for(model: str | None):
    """Longest configured prefix match, so a suffixed model ID (a dated snapshot) still resolves."""
    if not model:
        return None
    matches = [k for k in PRICES if model == k or model.startswith(k + "-")]
    return PRICES[max(matches, key=len)] if matches else None
