"""Orchestration: lenient record parsing, per-message isolation, concurrency, second opinion.

Invariant: exactly one TriageResult per input record, in input order, whatever the records
contain and whatever the model does.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import BRANDS, DEFAULT_CONCURRENCY, KNOWN_CHANNELS, SLA
from .llm import Classifier, LLMResult
from .models import Confidence, InboundMessage, LLMTriage, Meta, Routing, TriageResult
from .policy import decide, queue_for
from .rules import alnum, detect_signals, extract_entities, sanitize_text

IST = timezone(timedelta(hours=5, minutes=30))   # assumed for timestamps that carry no offset


# ---------------------------------------------------------------------------
# record -> InboundMessage (never raises)
# ---------------------------------------------------------------------------

def _label(value: Any) -> str | None:
    return value.strip().lower() or None if isinstance(value, str) else None


def _timestamp(value: Any) -> tuple[datetime | None, str | None, str | None]:
    if value is None:
        return None, None, "missing_timestamp"
    if not isinstance(value, str):
        return None, str(value)[:50], "bad_timestamp"
    raw = value.strip()
    try:
        dt = datetime.fromisoformat(raw[:-1] + "+00:00" if raw[-1:] in ("Z", "z") else raw)
    except ValueError:
        return None, raw[:50], "bad_timestamp"
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST), raw, "timestamp_without_timezone"
    return dt, raw, None


def normalize_record(record: Any, index: int, seen_ids: set[str]) -> InboundMessage:
    row_id = f"ROW-{index + 1:04d}"
    if not isinstance(record, dict):
        return InboundMessage(index=index, message_id=row_id, brand=None, channel=None, received_at=None,
                              received_at_raw=None, text=None,
                              input_flags=[f"record_not_object:{type(record).__name__}"])
    flags: list[str] = []

    raw_id = record.get("id")
    if isinstance(raw_id, str) and raw_id.strip():
        message_id = raw_id.strip()[:100]
    elif isinstance(raw_id, int) and not isinstance(raw_id, bool):
        message_id = str(raw_id)
        flags.append("id_not_string")
    else:
        message_id = row_id
        flags.append("missing_id")
    if message_id in seen_ids:
        flags.append("duplicate_id")
    seen_ids.add(message_id)

    brand = _label(record.get("brand"))
    if brand is None:
        flags.append("missing_brand")
    elif brand not in BRANDS:
        flags.append("unknown_brand")

    channel = _label(record.get("channel"))
    if channel is None:
        flags.append("missing_channel")
    elif channel not in KNOWN_CHANNELS:
        flags.append("unknown_channel")

    received_at, received_raw, ts_flag = _timestamp(record.get("received_at"))
    if ts_flag:
        flags.append(ts_flag)

    if "text" not in record:
        flags.append("missing_text_field")
    text, text_flags = sanitize_text(record.get("text"))
    flags += [f for f in text_flags if not (f == "no_text" and "missing_text_field" in flags)]

    return InboundMessage(index=index, message_id=message_id, brand=brand, channel=channel,
                          received_at=received_at, received_at_raw=received_raw, text=text,
                          input_flags=flags)


# ---------------------------------------------------------------------------
# second opinion
# ---------------------------------------------------------------------------

_RISK_FLAGS = {"prompt_injection", "emergency", "health_related", "legal_or_chargeback", "needs_context"}


def _disagreement(a: LLMTriage, b: LLMTriage | None) -> str | None:
    if b is None:
        return "second opinion unavailable"
    ia, ib = {i.intent for i in a.intents}, {i.intent for i in b.intents}
    if ia != ib:
        return f"intents {sorted(ia)} vs {sorted(ib)}"
    ids = lambda t: {(e.type, alnum(e.text)) for e in t.entities if e.type in ("order_id", "booking_ref")}
    if ids(a) != ids(b):
        return "extracted IDs differ"
    if (set(a.flags) ^ set(b.flags)) & _RISK_FLAGS:
        return f"risk flags differ: {sorted((set(a.flags) ^ set(b.flags)) & _RISK_FLAGS)}"
    return None


# ---------------------------------------------------------------------------
# one message
# ---------------------------------------------------------------------------

async def triage_message(msg: InboundMessage, classifier: Classifier | None, verify: bool = True) -> TriageResult:
    mode = "llm" if classifier else "rules_only"
    if msg.text is None:
        return decide(msg, {}, [], None, mode)

    region = BRANDS.get(msg.brand or "", {}).get("phone_region")
    signals = detect_signals(msg.text)
    regex_entities = extract_entities(msg.text, region)
    if classifier is None:
        return decide(msg, signals, regex_entities, None, mode)

    first = await classifier.classify(msg)
    result = decide(msg, signals, regex_entities, first, mode)

    # Before anything is automated, ask again at a higher temperature. If the two readings
    # disagree on intents, IDs or risk flags, the message goes to a person instead.
    if verify and result.routing.decision == "automation" and first.triage is not None:
        second = await classifier.classify(msg, temperature=0.7)
        problem = _disagreement(first.triage, second.triage)
        if problem:
            result = decide(msg, signals, regex_entities, first, mode,
                            extra_caps=((0.5, f"second opinion disagreed: {problem}"),))
        m = result.meta
        m.llm_calls += second.calls
        m.input_tokens += second.input_tokens
        m.cached_input_tokens += second.cached_input_tokens
        m.output_tokens += second.output_tokens
        m.latency_ms = (m.latency_ms or 0) + second.latency_ms
        m.errors += [f"second opinion: {e}" for e in second.errors]
    return result


def _last_resort(msg: InboundMessage, error: str, mode: str) -> TriageResult:
    """Used only if the normal path raised: still a valid, human-routed result."""
    support = queue_for("support", msg.brand)
    return TriageResult(
        message_id=msg.message_id, brand=msg.brand, channel=msg.channel,
        received_at=msg.received_at_raw, text=msg.text, status="degraded", sender_type="unknown",
        language=None, summary=None, intents=[], entities=[], flags=["internal_error"], missing_info=[],
        sentiment=None, confidence=Confidence(score=0.0, band="none", reasons=[error]),
        routing=Routing(decision="human", handled_by=support, priority="P3", sla=SLA["P3"], rule="R2",
                        explanation="Internal error while triaging; routed to a person."),
        meta=Meta(mode=mode, input_flags=list(msg.input_flags), errors=[error]))


async def triage_records(records: list[Any], classifier: Classifier | None,
                         concurrency: int = DEFAULT_CONCURRENCY, verify: bool = True) -> list[TriageResult]:
    seen: set[str] = set()
    messages = [normalize_record(r, i, seen) for i, r in enumerate(records)]
    semaphore = asyncio.Semaphore(max(1, concurrency))
    mode = "llm" if classifier else "rules_only"

    async def run(msg: InboundMessage) -> TriageResult:
        async with semaphore:
            try:
                return await triage_message(msg, classifier, verify)
            except Exception as e:   # one bad message must never take the batch down
                error = f"internal_error: {type(e).__name__}: {e}"
                try:
                    return decide(msg, {}, [], LLMResult(error=error), mode)
                except Exception:
                    return _last_resort(msg, error, mode)

    results = await asyncio.gather(*(run(m) for m in messages))
    assert len(results) == len(records), "every input record must produce exactly one result"
    return list(results)
