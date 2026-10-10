"""Policy: the intent table, the confidence score, and routing rules R1-R9.

Everything here is a pure function of (message, rule signals, regex entities, model output).
The model's labels are an input; the route is decided here, in code. A model that has been
talked into something by the message it is reading can mislabel, but it cannot authorise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import BRANDS, FALLBACK_QUEUE, PRIORITIES, PROMPT_VERSION, SHARED_QUEUES, SLA
from .llm import LLMResult
from .models import (
    Confidence, EntityOut, InboundMessage, IntentOut, LLMEntity, LLMIntent, Meta, Routing,
    TriageResult,
)
from .rules import alnum, is_grounded

AUTOMATABLE, HUMAN_REQUIRED, SPECIALIST, UNCLEAR = "automatable", "human_required", "specialist", "unclear"
AUTOMATION_THRESHOLD = 0.8


@dataclass(frozen=True)
class Policy:
    policy_class: str
    role: str                               # queue role, resolved per brand (see queue_for)
    base_priority: str
    step: str                               # next-action template
    auto_action: str | None = None
    requires: tuple[str, ...] = ()          # grounded entity types needed before automation may act
    brands: tuple[str, ...] | None = None   # None = valid for every brand


VW, HS, VT = ("vitalis-wellness",), ("hair-studio",), ("voyage-travel",)

POLICIES: dict[str, Policy] = {
    # automatable - read-only or reversible; automation may act when R7-R9 allow it
    "order_status": Policy(
        AUTOMATABLE, "support", "P3",
        "Look up tracking for {order_id}; if there has been no carrier scan for over 72h, open a "
        "carrier trace and send the customer a holding update.",
        auto_action="order_tracking_lookup", requires=("order_id",), brands=VW),
    "subscription_cancel": Policy(
        AUTOMATABLE, "support", "P3",
        "Send the self-serve cancellation link (the customer confirms); record the reason: {detail}.",
        auto_action="send_cancellation_link", brands=VW),
    "marketing_unsubscribe": Policy(
        AUTOMATABLE, "support", "P4",
        "Remove the sender from marketing lists and confirm. Do not touch paid subscriptions; "
        "include a link in case they meant one.",
        auto_action="marketing_opt_out"),
    "faq": Policy(
        AUTOMATABLE, "support", "P3",
        "Answer from approved content only ({detail}); if the topic is not covered, hand off to staff.",
        auto_action="answer_from_approved_content"),
    "appointment_book": Policy(
        AUTOMATABLE, "support", "P3",
        "Send the booking link with available slots ({detail}).",
        auto_action="send_booking_link", brands=HS),
    "appointment_reschedule": Policy(
        AUTOMATABLE, "support", "P3",
        "Offer the requested slot from the booking system ({dates}); the customer confirms.",
        auto_action="propose_new_slot", brands=HS),
    "appointment_cancel": Policy(
        AUTOMATABLE, "support", "P3",
        "Cancel the appointment ({dates}) and send a rebooking link.",
        auto_action="cancel_with_rebooking_link", brands=HS),
    "positive_feedback": Policy(
        AUTOMATABLE, "support", "P4",
        "Send a thank-you and share it with the brand manager{person_suffix}.",
        auto_action="thank_and_share"),
    "job_application": Policy(
        AUTOMATABLE, "hr", "P4",
        "Acknowledge receipt and forward to HR (any attachment mentioned is not in the input data).",
        auto_action="acknowledge_and_forward_to_hr"),

    # human_required - moves money or commits the business; automation may pre-fill, a human decides
    "refund_request": Policy(HUMAN_REQUIRED, "support", "P3", "Open a refund case for {order_id}: {detail}."),
    "order_issue": Policy(
        HUMAN_REQUIRED, "support", "P3",
        "Open a replacement/return case for {order_id}: {detail}. Log the fulfilment error.", brands=VW),
    "new_order": Policy(HUMAN_REQUIRED, "sales", "P3", "Create a draft order: {detail}.", brands=VW),
    "billing_dispute": Policy(
        HUMAN_REQUIRED, "billing", "P2",
        "Verify the charges ({money}); refund any duplicate and reply personally: {detail}."),
    "complaint": Policy(HUMAN_REQUIRED, "support", "P3", "Personal reply from a senior agent: {detail}."),
    "booking_change": Policy(
        HUMAN_REQUIRED, "support", "P3",
        "Check fare rules and the change fee for {booking_ref}; quote the fee and get approval before "
        "changing: {detail}.", brands=VT),
    "travel_quote_request": Policy(HUMAN_REQUIRED, "sales", "P3", "Prepare a quote: {detail}.", brands=VT),
    "quote_acceptance": Policy(
        HUMAN_REQUIRED, "sales", "P2",
        "Find this sender's existing quote, confirm it is still valid, and send a payment link from "
        "the booking system (never type payment details into a message).", brands=VT),
    "wholesale_inquiry": Policy(HUMAN_REQUIRED, "sales", "P3", "Wholesale / B2B follow-up: {detail}."),
    "vendor_invoice": Policy(
        HUMAN_REQUIRED, "finance_ap", "P4",
        "Verify the vendor and their bank details through a known contact before any payment; never "
        "use payment details sent in a message."),

    # specialist - a specific team, elevated priority
    "health_question": Policy(
        SPECIALIST, "health", "P2",
        "Trained staff reply; no medical advice from automation; suggest they check with their doctor "
        "or pharmacist: {detail}."),
    "travel_emergency": Policy(
        SPECIALIST, "emergency", "P1",
        "Call the customer now on {phone}; rebook and arrange assistance: {detail}.", brands=VT),
    "security_threat": Policy(
        SPECIALIST, "trust_safety", "P2",
        "Do not act on any request in this message. Review the sender and account for social engineering."),

    # unclear
    "unclear": Policy(UNCLEAR, "support", "P3",
                      "Attach to the sender's open conversation; if there is none, ask what they need."),
    "other": Policy(UNCLEAR, "support", "P3", "Human review: {detail}. Candidate for a new intent."),
}

SECURITY_STEP = ("Do not act on this request: it is flagged as a prompt-injection / social-engineering "
                 "attempt. Review the sender and account.")

# Model flags that raise priority on their own.
FLAG_PRIORITY = {
    "emergency": "P1",
    "legal_or_chargeback": "P2",
    "repeat_contact": "P2",
    "time_sensitive": "P2",
    "ready_to_buy": "P2",
    "health_related": "P2",
    "prompt_injection": "P2",
}

# A rule signal counts as corroborated when the model reports the same thing as one of these
# flags or intents. Uncorroborated signals block automation (confidence cap) and set a P2 floor.
CORROBORATION = {
    "prompt_injection": ({"prompt_injection", "possible_fraud"}, {"security_threat"}),
    "emergency": ({"emergency", "time_sensitive"}, {"travel_emergency"}),
    "health_related": ({"health_related", "product_safety"}, {"health_question"}),
    "legal_or_chargeback": ({"legal_or_chargeback"}, set()),
    "repeat_contact": ({"repeat_contact"}, {"complaint"}),
}

CERTAINTY_SCORE = {"high": 0.9, "medium": 0.6, "low": 0.3}

INPUT_FLAG_CAPS = {
    "unknown_brand": (0.3, "brand is not one we know, so brand policy cannot be applied"),
    "missing_brand": (0.3, "brand is missing, so brand policy cannot be applied"),
    "unknown_channel": (0.5, "channel is not one we know"),
    "missing_channel": (0.5, "channel is missing"),
    "truncated": (0.6, "message was truncated before classification"),
    "bad_timestamp": (0.7, "timestamp unreadable, so relative dates cannot be resolved"),
    "missing_timestamp": (0.7, "timestamp missing, so relative dates cannot be resolved"),
    "duplicate_id": (0.7, "another record has the same id (possible redelivery)"),
}

ID_TYPES = {"order_id", "booking_ref", "phone", "email", "money"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def queue_for(role: str, brand: str | None) -> str:
    if role in SHARED_QUEUES:
        return role
    cfg = BRANDS.get(brand or "")
    if not cfg:
        return FALLBACK_QUEUE
    return cfg["queues"].get(role) or cfg["queues"]["support"]


def _most_urgent(*priorities: str) -> str:
    return min(priorities, key=PRIORITIES.index)


def _band(score: float) -> str:
    return "high" if score >= AUTOMATION_THRESHOLD else "medium" if score >= 0.5 else "low"


def _digits(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")


class _Defaults(dict):
    def __missing__(self, key: str) -> str:
        return "(not given)"


def _render(step: str, entities: list[EntityOut], detail: str | None) -> str:
    grounded = [e for e in entities if e.grounded]

    def values(etype: str) -> list[str]:
        return list(dict.fromkeys(e.normalized or e.text for e in grounded if e.type == etype))

    dates = [f"{e.normalized} ('{e.text}')" if e.normalized else e.text
             for e in grounded if e.type in ("date", "datetime")]
    persons = values("person")
    ctx = _Defaults(
        order_id=", ".join(values("order_id")) or "the order (no order ID given)",
        booking_ref=", ".join(values("booking_ref")) or "the booking (no reference given)",
        phone=", ".join(values("phone")) or "the number on file",
        money=", ".join(values("money")) or "amounts not stated",
        dates="; ".join(dict.fromkeys(dates)) or "date not stated",
        detail=(detail or "see message").strip(),
        person_suffix=f" and {', '.join(persons)}" if persons else "",
    )
    return step.format_map(ctx)


def _merge_entities(llm_entities: list[LLMEntity], regex_entities: list[EntityOut],
                    text: str) -> tuple[list[EntityOut], list[str]]:
    """LLM entities checked against the text, merged with the regex ones.
    Returns (entities, descriptions of ungrounded ID-like entities)."""
    out: list[EntityOut] = []
    for e in llm_entities:
        exact = e.type in ID_TYPES
        grounded = is_grounded(e.text, text, fuzzy=not exact)
        normalized, note = e.normalized, e.note
        if grounded and e.type in ("order_id", "booking_ref") and alnum(normalized) != alnum(e.text):
            normalized, note = e.text.upper(), "model's normalised value did not match the text; using the text"
        out.append(EntityOut(type=e.type, text=e.text, normalized=normalized, note=note,
                             source="llm", grounded=grounded))

    for r in regex_entities:
        match = next(
            (o for o in out if o.type == r.type and (
                alnum(o.text) == alnum(r.text) or alnum(o.normalized) == alnum(r.normalized)
                or (r.type == "phone" and _digits(o.text) and _digits(o.text) == _digits(r.text)))),
            None)
        if match is None:
            out.append(r)
            continue
        match.source = "llm+rules"
        match.grounded = True
        if r.type == "phone" and match.normalized != r.normalized:
            match.normalized = r.normalized   # libphonenumber is authoritative for phone formats
            match.note = r.note or "normalised by libphonenumber"
    # Judged after the merge: an ID the model wrote slightly differently but the regex found is fine.
    ungrounded = [f"{e.type} '{e.text}'" for e in out if e.type in ID_TYPES and not e.grounded]
    return out, ungrounded


def _base_meta(msg: InboundMessage, signals: dict[str, list[str]], llm: LLMResult | None,
               mode: str) -> Meta:
    meta = Meta(mode=mode, input_flags=list(msg.input_flags), rule_signals=signals)
    if llm is not None:
        meta.model = llm.model
        meta.prompt_version = PROMPT_VERSION
        meta.llm_calls = llm.calls
        meta.latency_ms = llm.latency_ms
        meta.input_tokens = llm.input_tokens
        meta.cached_input_tokens = llm.cached_input_tokens
        meta.output_tokens = llm.output_tokens
        meta.errors = list(llm.errors)
        if llm.triage is not None:
            meta.llm_flags = list(llm.triage.flags)
    return meta


def _result(msg: InboundMessage, **kw) -> TriageResult:
    return TriageResult(
        message_id=msg.message_id,
        brand=msg.brand,
        channel=msg.channel,
        received_at=msg.received_at.isoformat() if msg.received_at else msg.received_at_raw,
        text=msg.text,
        **kw,
    )


# ---------------------------------------------------------------------------
# the decision
# ---------------------------------------------------------------------------

def decide(msg: InboundMessage, signals: dict[str, list[str]], regex_entities: list[EntityOut],
           llm: LLMResult | None, mode: str,
           extra_caps: tuple[tuple[float, str], ...] = ()) -> TriageResult:
    meta = _base_meta(msg, signals, llm, mode)
    support = queue_for("support", msg.brand)

    # R1 - nothing readable to classify
    if msg.text is None:
        why = ", ".join(msg.input_flags) or "no text"
        return _result(
            msg, status="invalid_input", sender_type="unknown", language=None, summary=None,
            intents=[], entities=[], flags=sorted(set(msg.input_flags)), missing_info=[], sentiment=None,
            confidence=Confidence(score=0.0, band="none",
                                  reasons=[f"nothing to classify ({why}); on WhatsApp a null text is "
                                           "usually a voice note, image or sticker"]),
            routing=Routing(decision="human", handled_by=support, priority="P3", sla=SLA["P3"], rule="R1",
                            explanation="No readable text. A person needs to open the original message."),
            meta=meta)

    # R2 - no usable model output (rules-only mode, outage, invalid output). Rules still set the
    # queue and priority, so an "URGENT ... call me immediately" is P1 even with the model down.
    if llm is None or llm.triage is None:
        priority = "P3"
        if "emergency" in signals:
            priority = "P1"
        elif signals:
            priority = "P2"
        if "prompt_injection" in signals:
            owner = "trust_safety"
        elif "emergency" in signals:
            owner = queue_for("emergency", msg.brand)
        else:
            owner = support
        reason = (llm.error if llm else None) or "rules-only mode (no API key, or --no-llm)"
        return _result(
            msg, status="degraded", sender_type="unknown", language=None, summary=None,
            intents=[], entities=regex_entities,
            flags=sorted(set(signals) | set(msg.input_flags)), missing_info=[], sentiment=None,
            confidence=Confidence(score=0.0, band="none", reasons=[f"no model result: {reason}"]),
            routing=Routing(
                decision="human", handled_by=owner, priority=priority, sla=SLA[priority], rule="R2",
                explanation="No model result, so nothing is automated. Queue and priority come from "
                            f"keyword rules ({', '.join(sorted(signals)) or 'none matched'})."),
            meta=meta)

    t = llm.triage
    caps: list[tuple[float, str]] = list(extra_caps)

    entities, ungrounded = _merge_entities(t.entities, regex_entities, msg.text)
    caps += [(0.5, f"model-extracted {d} is not in the message") for d in ungrounded]

    intents = list(t.intents)
    if not intents:
        intents = [LLMIntent(evidence="", intent="unclear", detail="model returned no intents", certainty="low")]
        caps.append((0.3, "model returned no intents"))

    # per-intent checks
    checked = []
    for it in intents:
        policy = POLICIES.get(it.intent, POLICIES["other"])
        evidence_ok = is_grounded(it.evidence, msg.text)
        if not evidence_ok and it.evidence:
            caps.append((0.4, f"evidence quoted for '{it.intent}' is not in the message"))
        if policy.brands and msg.brand in BRANDS and msg.brand not in policy.brands:
            caps.append((0.3, f"'{it.intent}' is not an intent {msg.brand} handles"))
        checked.append((it, policy, evidence_ok))

    weakest = min(intents, key=lambda i: CERTAINTY_SCORE[i.certainty])
    base = CERTAINTY_SCORE[weakest.certainty]
    reasons = [] if weakest.certainty == "high" else [
        f"model certainty is '{weakest.certainty}' for '{weakest.intent}'"]

    llm_flags = set(t.flags)
    names = {i.intent for i in intents}
    for sig, hits in signals.items():
        flag_ok, intent_ok = CORROBORATION.get(sig, (set(), set()))
        if not (llm_flags & flag_ok or names & intent_ok):
            caps.append((0.3, f"keyword rules found '{sig}' ({hits[0]!r}) but the model did not"))
    if "needs_context" in llm_flags:
        caps.append((0.4, "depends on an earlier conversation"))
    if llm.repaired:
        caps.append((0.7, "model output needed a repair retry"))
    caps += [INPUT_FLAG_CAPS[f] for f in msg.input_flags if f in INPUT_FLAG_CAPS]

    score = min([base] + [c for c, _ in caps])
    reasons += list(dict.fromkeys(r for _, r in caps))
    confidence = Confidence(score=round(score, 2), band=_band(score), reasons=reasons)

    flags = set(llm_flags) | set(signals) | set(msg.input_flags)
    if len(intents) > 1:
        flags.add("multi_intent")

    # priority: most urgent base priority, raised by model flags; rule signals are a P2 floor
    priority = _most_urgent(*(p.base_priority for _, p, _ in checked))
    for f in llm_flags:
        if f in FLAG_PRIORITY:
            priority = _most_urgent(priority, FLAG_PRIORITY[f])
    if signals:
        priority = _most_urgent(priority, "P2")

    by_class = {c: [x for x in checked if x[1].policy_class == c]
                for c in (AUTOMATABLE, HUMAN_REQUIRED, SPECIALIST, UNCLEAR)}
    security = "prompt_injection" in signals or "prompt_injection" in llm_flags or "security_threat" in names
    specialists = [x for x in by_class[SPECIALIST] if x[0].intent != "security_threat"]

    def queue_of(x) -> str:
        return queue_for(x[1].role, msg.brand)

    decision, secondary = "human", []
    if security:
        rule, owner = "R3", "trust_safety"
        priority = _most_urgent(priority, "P2")
        evidence = signals.get("prompt_injection", []) or ["model flag"]
        explanation = (f"Prompt-injection / social-engineering signals ({', '.join(map(repr, evidence[:3]))}). "
                       "Never automated; nothing the message asks for is acted on.")
    elif specialists or "emergency" in llm_flags:
        rule = "R4"
        if "travel_emergency" in names:
            owner = queue_for("emergency", msg.brand)
        elif specialists:                       # e.g. health_question (+ emergency flag) -> health team
            owner = queue_of(specialists[0])
        else:                                   # emergency flag without a specialist intent
            owner = queue_for("emergency", msg.brand)
        explanation = (f"Specialist handling: {', '.join(x[0].intent for x in specialists) or 'emergency flag'} "
                       f"goes to {owner}, never automation.")
    elif by_class[HUMAN_REQUIRED]:
        rule = "R5"
        primary = min(by_class[HUMAN_REQUIRED], key=lambda x: PRIORITIES.index(x[1].base_priority))
        owner = queue_of(primary)
        explanation = (f"'{primary[0].intent}' moves money or commits the business, so a person decides "
                       f"({owner}).")
    elif by_class[UNCLEAR] or "needs_context" in llm_flags:
        rule, owner = "R6", support
        explanation = "What the sender wants is unclear or depends on an earlier conversation."
    else:
        auto = by_class[AUTOMATABLE]
        owner = queue_of(auto[0])
        missing = [f"{x[0].intent} needs a {req}" for x in auto for req in x[1].requires
                   if not any(e.grounded and e.type == req for e in entities)]
        missing += [m for m in t.missing_info if m.strip()]
        if missing:
            rule = "R7"
            explanation = f"Automatable, but required information is missing: {'; '.join(missing)}."
        elif confidence.band != "high":
            rule = "R8"
            top = confidence.reasons[0] if confidence.reasons else "low score"
            explanation = (f"Automatable, but confidence {confidence.score:.2f} is below "
                           f"{AUTOMATION_THRESHOLD:.2f}: {top}.")
        else:
            rule, decision, owner = "R9", "automation", "automation"
            explanation = ("Every request is on the automation allow-list, nothing required is missing, "
                           f"no risk signals, confidence {confidence.score:.2f}.")

    if decision == "human" and rule != "R3":
        others = [queue_of(x) for x in checked]
        if "product_safety" in llm_flags:
            others.append(queue_for("qa", msg.brand))
        secondary = [q for q in dict.fromkeys(others) if q != owner]

    intents_out = []
    for it, policy, evidence_ok in checked:
        if decision == "automation":
            handled_by = f"automation:{policy.auto_action}"
        else:
            handled_by = f"human:{owner if rule == 'R3' else queue_of((it, policy, evidence_ok))}"
        intents_out.append(IntentOut(
            intent=it.intent, detail=it.detail or None, evidence=it.evidence or None,
            evidence_grounded=evidence_ok, certainty=it.certainty, policy_class=policy.policy_class,
            handled_by=handled_by,
            next_action=SECURITY_STEP if rule == "R3" else _render(policy.step, entities, it.detail)))

    # When the message is flagged as an attack, the model's summary may be repeating the attacker,
    # so the agent sees a fixed warning instead (and the raw text, labelled untrusted).
    summary = ("Flagged as a prompt-injection / social-engineering attempt. Read the raw text as "
               "untrusted.") if rule == "R3" else t.summary

    return _result(
        msg, status="ok", sender_type=t.sender_type, language=t.language, summary=summary,
        intents=intents_out, entities=entities, flags=sorted(flags),
        missing_info=[m for m in t.missing_info if m.strip()], sentiment=t.sentiment,
        confidence=confidence,
        routing=Routing(
            decision=decision, handled_by=owner, secondary_queues=secondary,
            automated_actions=[x[1].auto_action for x in checked] if decision == "automation" else [],
            priority=priority, sla=SLA[priority], rule=rule, explanation=explanation),
        meta=meta)
