"""Cost per 1,000 messages on Groq from counted tokens (no API key needed).

    python eval/estimate_cost.py [messages.json]

Input tokens are counted with o200k_base, the tokenizer gpt-oss uses (its chat format adds a few
special tokens), over the real system prompt + JSON schema + user message for every message in
the file. Output tokens are the JSON, counted over hand-written outputs for representative
messages (simple / typical / multi-intent), plus gpt-oss's hidden reasoning, which is billed as
output. After a real run, output/<name>/run_stats.json gives *measured* usage instead.
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

import tiktoken
from openai.lib._parsing._completions import type_to_response_format_param

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from triage.config import PRICES, PRICES_CHECKED_ON  # noqa: E402
from triage.models import LLMEntity, LLMIntent, LLMTriage  # noqa: E402
from triage.pipeline import normalize_record  # noqa: E402
from triage.prompt import SYSTEM_PROMPT, build_user_message  # noqa: E402

CHAT_OVERHEAD = 10          # role/message framing tokens per request (approximate)
RETRY_RATE = 0.03           # assumed share of billed calls repeated (5xx, invalid output); 429s aren't billed
REASONING_TOKENS = 480      # measured on the Groq run: 641 output tokens per call, ~160 of them the JSON
CANDIDATES = ["openai/gpt-oss-20b", "openai/gpt-oss-120b"]


def _triage(intents, entities=(), flags=(), missing=(), summary="", sender="customer", lang="en", sentiment="neutral"):
    return LLMTriage(
        summary=summary, sender_type=sender, language=lang, sentiment=sentiment,
        intents=[LLMIntent(evidence=e, intent=i, detail=d, certainty=c) for i, e, d, c in intents],
        entities=[LLMEntity(type=t, text=x, normalized=n, note=note) for t, x, n, note in entities],
        flags=list(flags), missing_info=list(missing))


# Representative model outputs, written the way the prompt asks for them.
REPRESENTATIVE_OUTPUTS = {
    "simple (MSG-017)": _triage(
        [("marketing_unsubscribe", "Unsubscribe", "wants to stop marketing emails", "high")],
        summary="Customer asks to unsubscribe; no mention of a product subscription."),
    "typical (MSG-001)": _triage(
        [("order_status", "Tracking hasn't moved in four days. Can you check?",
          "tracking stalled for 4 days", "high")],
        entities=[("order_id", "VW-48812", "VW-48812", None),
                  ("product", "magnesium capsules", "magnesium capsules", None),
                  ("date", "the 22nd", "2026-09-22", "day of month in the past; assumed September")],
        summary="Order VW-48812 (magnesium capsules, ordered 22 Sep) has had no tracking update for four days; "
                "customer asks for a check."),
    "multi-intent (MSG-008)": _triage(
        [("refund_request", "I want a refund on order VW-48190, the seal was broken when it arrived",
          "seal broken on arrival", "high"),
         ("new_order", "I'd still like to order two tubs of the protein in chocolate",
          "2 tubs chocolate protein, better packaging requested", "high")],
        entities=[("order_id", "VW-48190", "VW-48190", None),
                  ("product", "protein in chocolate", "protein, chocolate", None),
                  ("quantity", "two tubs", "2", None)],
        flags=["product_safety"],
        summary="Wants a refund for VW-48190 because the seal was broken on arrival; also wants to order "
                "two tubs of chocolate protein with better packaging."),
    "three intents (MSG-014)": _triage(
        [("appointment_reschedule", "Can I move my Thursday appointment?", "new date not given", "high"),
         ("appointment_book", "my sister wants to book the same day if there's a slot", "booking for sister, same day", "high"),
         ("faq", "does the membership cover her too or is it per person?", "does membership cover a second person", "high")],
        entities=[("date", "Thursday", "2026-10-01", "next Thursday after received_at"),
                  ("person", "my sister", "sister", None)],
        missing=["new date/time for the Thursday appointment"],
        summary="Wants to move their Thursday appointment (new time not given), book their sister the same "
                "day, and asks whether the membership covers her."),
}


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "messages.json")
    enc = tiktoken.get_encoding("o200k_base")
    schema = json.dumps(type_to_response_format_param(LLMTriage)["json_schema"])
    static = len(enc.encode(SYSTEM_PROMPT)) + len(enc.encode(schema)) + CHAT_OVERHEAD

    records = json.loads(path.read_text(encoding="utf-8"))
    seen: set[str] = set()
    msgs = [m for m in (normalize_record(r, i, seen) for i, r in enumerate(records)) if m.text]
    per_msg = [static + len(enc.encode(build_user_message(m))) for m in msgs]
    t_in = statistics.mean(per_msg)

    outs = {k: len(enc.encode(v.model_dump_json())) for k, v in REPRESENTATIVE_OUTPUTS.items()}
    t_json = statistics.mean(outs.values())
    t_out = t_json + REASONING_TOKENS

    print(f"Input tokens per call (counted, {len(msgs)} messages): mean {t_in:.0f}, min {min(per_msg)}, "
          f"max {max(per_msg)}; of which static prefix (prompt + schema) {static}")
    for k, v in outs.items():
        print(f"Output JSON tokens, {k}: {v}")
    print(f"Output tokens per call: {t_json:.0f} JSON (mean of representatives) + {REASONING_TOKENS} reasoning "
          f"= {t_out:.0f}")

    # Second opinion runs only for messages about to be automated: 10 of the 24 with text on the Groq run.
    second_opinion_rate = 10 / 24
    calls = (1 + second_opinion_rate) * (1 + RETRY_RATE)
    cached_share = static / t_in            # best case: Groq caches the identical prefix (best effort, 50% off)
    print(f"\nCalls per message: (1 + {second_opinion_rate:.2f} second opinion) x (1 + {RETRY_RATE} retries) "
          f"= {calls:.2f}; cacheable share of input: {cached_share:.0%}")
    print(f"Groq list prices per 1M tokens, checked {PRICES_CHECKED_ON}\n")
    print(f"{'model':<20} {'in/cached/out $':<18} {'per 1k (no cache)':>18} {'per 1k (cached)':>16} {'per day @10k':>13}")
    for model in CANDIDATES:
        p_in, p_cached, p_out = PRICES[model]
        no_cache = (t_in * p_in + t_out * p_out) / 1e6 * calls * 1000
        cached = (t_in * (1 - cached_share) * p_in + t_in * cached_share * p_cached + t_out * p_out) / 1e6 * calls * 1000
        print(f"{model:<20} {f'{p_in}/{p_cached}/{p_out}':<18} {f'${no_cache:.2f}':>18} {f'${cached:.2f}':>16} "
              f"{f'${cached * 10:.2f}':>13}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
