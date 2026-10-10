"""Deterministic layer: text sanitising, regex entity extraction, risk signals, grounding.

Nothing here depends on the LLM, so it all still works when the model is down, the key is
missing, or the model has been talked into something by the message it is reading.
"""

from __future__ import annotations

import html
import re
import unicodedata

import phonenumbers

from .config import ID_PATTERNS, MAX_TEXT_CHARS
from .models import EntityOut

# ---------------------------------------------------------------------------
# Sanitising
# ---------------------------------------------------------------------------

# Zero-width, soft hyphen, bidi embedding/override/isolate controls. They are invisible to a
# human reviewer and can be used to hide "ignore previous instructions" from keyword checks.
_INVISIBLE = re.compile("[­​-‏‪-‮⁠-⁤⁦-⁩﻿]")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_LOOKS_LIKE_HTML = re.compile(r"</?(?:html|body|div|p|br|span|table|td|tr|b|i|a|strong|em)\b[^>]*>", re.I)
_TAG = re.compile(r"<[^>]{0,500}>")


def sanitize_text(value: object) -> tuple[str | None, list[str]]:
    """Return (clean text or None, flags). Never raises."""
    if value is None:
        return None, ["no_text"]
    if not isinstance(value, str):
        return None, [f"text_not_string:{type(value).__name__}"]

    flags: list[str] = []
    text = unicodedata.normalize("NFKC", value)
    if _INVISIBLE.search(text):
        text = _INVISIBLE.sub("", text)
        flags.append("invisible_chars_removed")
    if _ANSI.search(text) or _CONTROL.search(text):
        text = _CONTROL.sub(" ", _ANSI.sub("", text))
        flags.append("control_chars_removed")
    if _LOOKS_LIKE_HTML.search(text):
        text = html.unescape(_TAG.sub(" ", text))
        flags.append("html_stripped")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return None, flags + ["empty_text"]
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
        flags.append("truncated")
    return text, flags


# ---------------------------------------------------------------------------
# Regex entities
# ---------------------------------------------------------------------------

_MONEY = re.compile(
    r"(?P<cur>INR|Rs\.?|₹|CAD|C\$|USD|US\$|\$)\s?(?P<amt>\d[\d,]*(?:\.\d{1,2})?)"
    r"|(?P<amt2>\d[\d,]*(?:\.\d{1,2})?)\s?(?P<cur2>INR|rupees|CAD|dollars)\b",
    re.I,
)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_CURRENCY = {"inr": "INR", "rs": "INR", "rs.": "INR", "₹": "INR", "rupees": "INR",
             "cad": "CAD", "c$": "CAD", "usd": "USD", "us$": "USD"}


def extract_entities(text: str, phone_region: str | None) -> list[EntityOut]:
    found: list[EntityOut] = []

    for etype, pattern in ID_PATTERNS.items():
        for m in re.finditer(pattern, text, re.I):
            found.append(EntityOut(type=etype, text=m.group(0), normalized=m.group(0).upper(),
                                   source="rules", grounded=True))

    # VALID (not POSSIBLE): POSSIBLE also matches dates like 2026-09-28 and bare 8-digit numbers.
    for m in phonenumbers.PhoneNumberMatcher(text, phone_region or "IN",
                                             leniency=phonenumbers.Leniency.VALID):
        e164 = phonenumbers.format_number(m.number, phonenumbers.PhoneNumberFormat.E164)
        note = None
        if not m.raw_string.strip().startswith("+"):
            note = f"no country code; assumed {phone_region or 'IN'} from brand"
        found.append(EntityOut(type="phone", text=m.raw_string, normalized=e164, note=note,
                               source="rules", grounded=True))

    for m in _MONEY.finditer(text):
        cur = (m.group("cur") or m.group("cur2") or "").lower()
        amt = (m.group("amt") or m.group("amt2") or "").replace(",", "")
        code = _CURRENCY.get(cur)
        note = None
        if code is None:  # bare "$"
            code = "CAD" if phone_region == "CA" else "USD"
            note = "currency symbol '$' resolved from brand region"
        found.append(EntityOut(type="money", text=m.group(0), normalized=f"{code} {amt}", note=note,
                               source="rules", grounded=True))

    for m in _EMAIL.finditer(text):
        found.append(EntityOut(type="email", text=m.group(0), normalized=m.group(0).lower(),
                               source="rules", grounded=True))
    return found


# ---------------------------------------------------------------------------
# Risk signals (independent of the model)
# ---------------------------------------------------------------------------

SIGNAL_PATTERNS: dict[str, list[str]] = {
    "prompt_injection": [
        r"\bignore\b.{0,30}\b(previous|prior|above|earlier|all)\b.{0,20}\b(instructions?|prompts?|rules)\b",
        r"\bdisregard\b.{0,40}\b(instructions?|rules|polic(y|ies))\b",
        r"\bsystem (notice|prompt|message|override|instruction)",
        r"\byou are now\b",
        r"\b(administrator|admin|developer|god|debug) mode\b",
        r"\bdo not (escalate|forward|route|involve)\b",
        r"\b(don'?t|never) (escalate|involve a human|send (this )?to a human)\b",
        r"</?(system|assistant|instructions?|message)>",
        r"(^|[\n\"'}\]>])\s*(system|assistant|developer)\s*:",       # fake role prefixes
        r"\b(classify|label|categori[sz]e) (this|it|me) as\b",           # steering the classifier
        r"\broute (this |it |me )?to (automation|a bot|the bot)\b",
        r"\b(certainty|confidence)\s*[:=]?\s*(high|1(\.0+)?|100%?)\b",
        r"\bmark (this|me|my account|this customer|this account) as (vip|verified|approved)\b",
    ],
    "emergency": [
        r"\burgent\b", r"\bemergenc", r"\bstranded\b", r"\bimmediately\b", r"\basap\b",
        r"\bright now\b",
    ],
    "health_related": [
        r"\bmedication", r"\bmedicine", r"\bdoctor\b", r"\bprescri", r"\bpregnan", r"\bbreastfeed",
        r"\ballerg", r"\bside[- ]?effects?\b", r"\bsafe to (take|use|consume|have)\b",
        r"\bblood pressure\b", r"\bdiabet", r"\binteract", r"\bhospital\b",
    ],
    "legal_or_chargeback": [
        r"\bchargeback\b", r"\bwith my bank\b", r"\bconsumer (court|forum)\b", r"\blawyer\b",
        r"\blegal action\b", r"\bsue\b", r"\bcharged twice\b", r"\bdouble[- ]?charged\b",
    ],
    "repeat_contact": [
        r"\b(second|third|fourth|fifth|\d+(st|nd|rd|th)) time\b",
        r"\b(nobody|no one|no-one) (has )?(responded|replied|answered|got back)\b",
        r"\bstill (waiting|no (reply|response|answer))\b",
    ],
}
_COMPILED = {k: [re.compile(p, re.I | re.S) for p in v] for k, v in SIGNAL_PATTERNS.items()}


def detect_signals(text: str) -> dict[str, list[str]]:
    """signal -> matched snippets. Only signals that fired are returned."""
    out: dict[str, list[str]] = {}
    for name, patterns in _COMPILED.items():
        hits = [m.group(0) for p in patterns for m in [p.search(text)] if m]
        if hits:
            out[name] = hits
    return out


# ---------------------------------------------------------------------------
# Grounding: is what the model quoted actually in the message?
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", s).split())


def is_grounded(span: str | None, text: str, fuzzy: bool = True) -> bool:
    """Exact (normalised) substring match; optionally token overlap >= 80% for longer quotes."""
    if not span or not text:
        return False
    ns, nt = _norm(span), _norm(text)
    if not ns:
        return False
    if ns in nt:
        return True
    if not fuzzy:
        return False
    tokens = ns.split()
    if len(tokens) < 3:
        return False
    vocab = set(nt.split())
    return sum(t in vocab for t in tokens) / len(tokens) >= 0.8


def alnum(s: str | None) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", s or "").upper()
