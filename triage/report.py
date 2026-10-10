"""Outputs: results.json (machine), summary.md (people), run_stats.json (cost and counts)."""

from __future__ import annotations

import json
import statistics
from collections import Counter
from datetime import datetime
from pathlib import Path

from .config import PRICES_CHECKED_ON, PRIORITIES, price_for
from .models import TriageResult


def compute_stats(results: list[TriageResult], input_name: str, mode: str, model: str | None,
                  elapsed_s: float, llm_disabled: str | None) -> dict:
    llm_results = [r for r in results if r.meta.llm_calls > 0]
    tokens_in = sum(r.meta.input_tokens for r in results)
    tokens_cached = sum(r.meta.cached_input_tokens for r in results)
    tokens_out = sum(r.meta.output_tokens for r in results)
    served_model = next((r.meta.model for r in llm_results if r.meta.model), None) or model

    cost = None
    price = price_for(served_model) or price_for(model)
    if price and llm_results:
        p_in, p_cached, p_out = price
        total = ((tokens_in - tokens_cached) * p_in + tokens_cached * p_cached + tokens_out * p_out) / 1e6
        per_message = total / len(llm_results)
        cost = {
            "run_usd": round(total, 5),
            "per_message_usd": round(per_message, 6),
            "per_1000_messages_usd": round(per_message * 1000, 3),
            "per_day_at_10k_usd": round(per_message * 10_000, 2),
            "price_per_1m_tokens_usd": {"input": p_in, "cached_input": p_cached, "output": p_out},
            "prices_checked_on": PRICES_CHECKED_ON,
            "basis": "measured token usage of this run (includes retries and second-opinion calls) "
                     "x list price",
        }

    latencies = [r.meta.latency_ms for r in llm_results if r.meta.latency_ms is not None]
    return {
        "input": input_name,
        "run_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": mode,
        "model": served_model,
        "llm_disabled_reason": llm_disabled,
        "elapsed_s": round(elapsed_s, 1),
        "messages": len(results),
        "by_decision": dict(Counter(r.routing.decision for r in results)),
        "by_status": dict(Counter(r.status for r in results)),
        "by_priority": {p: n for p in PRIORITIES if (n := sum(r.routing.priority == p for r in results))},
        "by_rule": dict(sorted(Counter(r.routing.rule for r in results).items())),
        "by_owner": dict(Counter(r.routing.handled_by for r in results).most_common()),
        "llm": {
            "messages_classified": len(llm_results),
            "calls": sum(r.meta.llm_calls for r in results),
            "input_tokens": tokens_in,
            "cached_input_tokens": tokens_cached,
            "output_tokens": tokens_out,
            "avg_input_tokens_per_message": round(tokens_in / len(llm_results)) if llm_results else None,
            "avg_output_tokens_per_message": round(tokens_out / len(llm_results)) if llm_results else None,
            "latency_ms_p50": int(statistics.median(latencies)) if latencies else None,
            "latency_ms_max": max(latencies) if latencies else None,
        },
        "cost": cost,
    }


def _cell(s: str | None, n: int = 80) -> str:
    s = " ".join((s or "").split()).replace("|", "\\|")
    return s if len(s) <= n else s[: n - 1] + "…"


def render_summary(results: list[TriageResult], stats: dict) -> str:
    d, llm, cost = stats["by_decision"], stats["llm"], stats["cost"]
    lines = [
        f"# Triage results: {stats['input']}",
        "",
        f"- Run: {stats['run_at']} · mode `{stats['mode']}` · model `{stats['model'] or '-'}` · "
        f"{stats['messages']} messages in {stats['elapsed_s']}s",
        f"- Decisions: **{d.get('automation', 0)} automation**, **{d.get('human', 0)} human** · "
        f"status: {', '.join(f'{k} {v}' for k, v in stats['by_status'].items())}",
        f"- Priority: {' · '.join(f'{k} {v}' for k, v in stats['by_priority'].items())} · "
        f"rules fired: {', '.join(f'{k}×{v}' for k, v in stats['by_rule'].items())}",
    ]
    if llm["calls"]:
        lines.append(f"- LLM: {llm['calls']} calls · {llm['input_tokens']:,} input tokens "
                     f"({llm['cached_input_tokens']:,} cached) · {llm['output_tokens']:,} output tokens")
    if cost:
        lines.append(f"- Cost: ${cost['run_usd']:.4f} this run → **${cost['per_1000_messages_usd']:.2f} per 1,000 "
                     f"messages** (${cost['per_day_at_10k_usd']:.2f}/day at 10,000/day; list prices checked "
                     f"{cost['prices_checked_on']})")
    if stats.get("llm_disabled_reason"):
        lines.append(f"- ⚠️ LLM disabled mid-run: {stats['llm_disabled_reason']}")

    lines += ["", "| ID | Brand | Channel | Intents | Decision → owner | Prio | Conf | Rule |",
              "|---|---|---|---|---|---|---|---|"]
    for r in results:
        intents = ", ".join(i.intent for i in r.intents) or f"({r.status})"
        owner = r.routing.handled_by + (f" (+{', '.join(r.routing.secondary_queues)})"
                                        if r.routing.secondary_queues else "")
        lines.append(f"| {r.message_id} | {r.brand or '-'} | {r.channel or '-'} | {intents} | "
                     f"{r.routing.decision} → {owner} | {r.routing.priority} | "
                     f"{r.confidence.score:.2f} {r.confidence.band} | {r.routing.rule} |")

    lines += ["", "## Per message", ""]
    for r in results:
        lines.append(f"### {r.message_id}: {r.routing.decision} → {r.routing.handled_by} "
                     f"({r.routing.priority}, {r.routing.rule})")
        lines.append(f"> {_cell(r.text, 300) or '(no text)'}")
        lines.append("")
        if r.summary:
            lines.append(f"- **Summary (AI, unverified):** {_cell(r.summary, 300)}")
        for i in r.intents:
            lines.append(f"- **{i.intent}** ({i.certainty}) → `{i.handled_by}`: {_cell(i.next_action, 300)}")
        ents = [f"{e.type}={e.normalized or e.text}" + ("" if e.grounded else " ⚠️ungrounded")
                for e in r.entities]
        if ents:
            lines.append(f"- **Entities:** {_cell(', '.join(ents), 400)}")
        if r.flags:
            lines.append(f"- **Flags:** {', '.join(r.flags)}")
        if r.missing_info:
            lines.append(f"- **Missing:** {'; '.join(r.missing_info)}")
        lines.append(f"- **Why:** {r.routing.explanation}")
        if r.confidence.reasons:
            lines.append(f"- **Confidence {r.confidence.score:.2f}:** {'; '.join(r.confidence.reasons)}")
        lines.append("")
    return "\n".join(lines)


def write_outputs(results: list[TriageResult], stats: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(
        json.dumps([r.model_dump(mode="json") for r in results], indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    (out_dir / "run_stats.json").write_text(json.dumps(stats, indent=2, ensure_ascii=False) + "\n",
                                            encoding="utf-8")
    (out_dir / "summary.md").write_text(render_summary(results, stats) + "\n", encoding="utf-8")


def print_console(results: list[TriageResult], stats: dict) -> None:
    print(f"{'ID':<10} {'DECISION':<10} {'OWNER':<18} {'PRIO':<4} {'CONF':<5} {'RULE':<4} INTENTS")
    for r in results:
        intents = ",".join(i.intent for i in r.intents) or f"({r.status})"
        print(f"{r.message_id[:10]:<10} {r.routing.decision:<10} {r.routing.handled_by[:18]:<18} "
              f"{r.routing.priority:<4} {r.confidence.score:<5.2f} {r.routing.rule:<4} {intents}")
    d, llm, cost = stats["by_decision"], stats["llm"], stats["cost"]
    print(f"\n{stats['messages']} messages · {d.get('automation', 0)} automation · {d.get('human', 0)} human · "
          f"{' '.join(f'{k}:{v}' for k, v in stats['by_priority'].items())} · status {stats['by_status']}")
    if llm["calls"]:
        print(f"LLM {stats['model']}: {llm['calls']} calls · {llm['input_tokens']:,} in "
              f"({llm['cached_input_tokens']:,} cached) · {llm['output_tokens']:,} out")
    if cost:
        print(f"Cost: ${cost['run_usd']:.4f} this run · ${cost['per_1000_messages_usd']:.2f} per 1,000 messages "
              f"· ${cost['per_day_at_10k_usd']:.2f}/day at 10k")
