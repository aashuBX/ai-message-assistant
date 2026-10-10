"""Data shapes.

Three layers, deliberately separate:
  * InboundMessage - a lenient view of one input record (never fails to construct).
  * LLMTriage      - the contract the model must fill in. Labels only: no routing, no
                     numeric confidence, no actions. Sent to Groq as a strict JSON schema,
                     so: no defaults, nullable instead of optional, enums as Literals.
  * TriageResult   - the final output record, one per input record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Model contract
# ---------------------------------------------------------------------------

IntentName = Literal[
    "order_status",
    "order_issue",
    "refund_request",
    "subscription_cancel",
    "marketing_unsubscribe",
    "new_order",
    "billing_dispute",
    "complaint",
    "faq",
    "appointment_book",
    "appointment_reschedule",
    "appointment_cancel",
    "booking_change",
    "travel_quote_request",
    "quote_acceptance",
    "travel_emergency",
    "health_question",
    "wholesale_inquiry",
    "vendor_invoice",
    "job_application",
    "positive_feedback",
    "security_threat",
    "unclear",
    "other",
]
Certainty = Literal["high", "medium", "low"]
EntityType = Literal[
    "order_id", "booking_ref", "phone", "email", "date", "datetime", "money",
    "product", "service", "person", "place", "airline", "quantity", "other",
]
Flag = Literal[
    "prompt_injection",
    "emergency",
    "vulnerable_person",
    "legal_or_chargeback",
    "repeat_contact",
    "time_sensitive",
    "ready_to_buy",
    "needs_context",
    "possible_fraud",
    "product_safety",
    "health_related",
]
SenderType = Literal["customer", "prospect", "business", "vendor", "job_applicant", "unknown"]
Sentiment = Literal["positive", "neutral", "negative", "angry"]


class LLMIntent(BaseModel):
    evidence: str            # verbatim quote; written first so the label is grounded in the text
    intent: IntentName
    detail: str
    certainty: Certainty


class LLMEntity(BaseModel):
    type: EntityType
    text: str                # verbatim span
    normalized: str | None
    note: str | None         # any assumption made while normalizing


class LLMTriage(BaseModel):
    summary: str
    sender_type: SenderType
    language: str
    intents: list[LLMIntent]
    entities: list[LLMEntity]
    flags: list[Flag]
    missing_info: list[str]
    sentiment: Sentiment


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------

@dataclass
class InboundMessage:
    index: int
    message_id: str
    brand: str | None
    channel: str | None
    received_at: datetime | None
    received_at_raw: str | None
    text: str | None                      # sanitized; None when unusable
    input_flags: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

class EntityOut(BaseModel):
    type: str
    text: str
    normalized: str | None = None
    note: str | None = None
    source: Literal["llm", "rules", "llm+rules"]
    grounded: bool


class IntentOut(BaseModel):
    intent: str
    detail: str | None = None
    evidence: str | None = None
    evidence_grounded: bool | None = None
    certainty: str | None = None
    policy_class: str
    handled_by: str          # "automation:<action>" or "human:<queue>"
    next_action: str


class Confidence(BaseModel):
    score: float
    band: Literal["high", "medium", "low", "none"]
    reasons: list[str] = []


class Routing(BaseModel):
    decision: Literal["automation", "human"]
    handled_by: str                          # "automation" or the owning queue
    secondary_queues: list[str] = []
    automated_actions: list[str] = []
    priority: Literal["P1", "P2", "P3", "P4"]
    sla: str
    rule: str                                # which of R1..R9 decided
    explanation: str


class Meta(BaseModel):
    mode: Literal["llm", "rules_only"]
    model: str | None = None
    prompt_version: str | None = None
    llm_calls: int = 0
    latency_ms: int | None = None
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    input_flags: list[str] = []
    rule_signals: dict[str, list[str]] = {}
    llm_flags: list[str] = []
    errors: list[str] = []


class TriageResult(BaseModel):
    message_id: str
    brand: str | None
    channel: str | None
    received_at: str | None
    text: str | None
    status: Literal["ok", "invalid_input", "degraded"]
    sender_type: str
    language: str | None
    summary: str | None
    intents: list[IntentOut]
    entities: list[EntityOut]
    flags: list[str]
    missing_info: list[str]
    sentiment: str | None
    confidence: Confidence
    routing: Routing
    meta: Meta
