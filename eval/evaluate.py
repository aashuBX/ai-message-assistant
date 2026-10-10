"""Compare a triage run with the hand labels in eval/gold.json.

    python eval/evaluate.py [--results output/messages/results.json] [--gold eval/gold.json]

Release gate: unsafe automations (gold says human, system says automation) must be 0.
Exit code 1 if the gate fails.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PRIORITIES = ("P1", "P2", "P3", "P4")


def _as_list(v) -> list:
    return v if isinstance(v, list) else [v]


def evaluate(gold: dict, results: list[dict]) -> dict:
    by_id = {r["message_id"]: r for r in results}
    rows, unsafe, missed, missing = [], [], [], []
    route_ok = intents_ok = queue_ok = queue_n = prio_ok = 0
    urgent_hit = urgent_n = 0

    for mid, g in gold.items():
        r = by_id.get(mid)
        if r is None:
            missing.append(mid)
            continue
        decision = r["routing"]["decision"]
        pred_intents = {i["intent"] for i in r["intents"]}
        required = set(g["intents"])
        allowed = required | set(g.get("optional_intents", []))

        r_ok = g["route"] == "either" or decision == g["route"]
        if g["route"] == "human" and decision == "automation":
            unsafe.append(mid)
        if g["route"] == "automation" and decision == "human":
            missed.append(mid)
        i_ok = required <= pred_intents <= allowed
        q_ok = None
        if decision == "human":
            queue_n += 1
            q_ok = r["routing"]["handled_by"] in _as_list(g["queue"])
            queue_ok += q_ok
        p_ok = r["routing"]["priority"] in _as_list(g["priority"])
        primary = _as_list(g["priority"])[0]          # first listed = the label; the rest are tolerated
        if primary in ("P1", "P2"):
            urgent_n += 1
            urgent_hit += PRIORITIES.index(r["routing"]["priority"]) <= PRIORITIES.index(primary)

        route_ok += r_ok
        intents_ok += i_ok
        prio_ok += p_ok
        if not (r_ok and i_ok and p_ok and q_ok is not False):
            rows.append({
                "id": mid,
                "route": f"{decision} (gold {g['route']})" + ("" if r_ok else "  ✗"),
                "intents": f"{sorted(pred_intents)} (gold {sorted(required)}"
                           + (f" +opt {sorted(allowed - required)}" if allowed - required else "") + ")"
                           + ("" if i_ok else "  ✗"),
                "queue": "" if q_ok is None else f"{r['routing']['handled_by']} (gold {g['queue']})" + ("" if q_ok else "  ✗"),
                "priority": f"{r['routing']['priority']} (gold {g['priority']})" + ("" if p_ok else "  ✗"),
                "rule": r["routing"]["rule"],
                "why": r["routing"]["explanation"],
            })

    n = len(gold) - len(missing)
    return {
        "messages": len(gold), "missing_results": missing,
        "unsafe_automations": unsafe, "missed_automations": missed,
        "route_accuracy": f"{route_ok}/{n}", "intent_match": f"{intents_ok}/{n}",
        "queue_match_when_human": f"{queue_ok}/{queue_n}", "priority_match": f"{prio_ok}/{n}",
        "urgent_recall_p1_p2": f"{urgent_hit}/{urgent_n}",
        "disagreements": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="output/messages/results.json")
    ap.add_argument("--gold", default=str(Path(__file__).with_name("gold.json")))
    args = ap.parse_args()
    gold = json.loads(Path(args.gold).read_text(encoding="utf-8"))["labels"]
    results = json.loads(Path(args.results).read_text(encoding="utf-8"))
    report = evaluate(gold, results)

    print(f"Evaluated {args.results} against {args.gold}\n")
    print(f"  UNSAFE automations (gold human -> automated): {len(report['unsafe_automations'])}  "
          f"{report['unsafe_automations'] or ''}")
    print(f"  Missed automations (gold automation -> human): {len(report['missed_automations'])}  "
          f"{report['missed_automations'] or ''}")
    print(f"  Route accuracy ('either' counts):  {report['route_accuracy']}")
    print(f"  Intent-set match:                  {report['intent_match']}")
    print(f"  Queue match (human-routed):        {report['queue_match_when_human']}")
    print(f"  Priority match:                    {report['priority_match']}")
    print(f"  Urgent recall (gold P1/P2 kept urgent): {report['urgent_recall_p1_p2']}")
    if report["missing_results"]:
        print(f"  Missing results for: {report['missing_results']}")
    if report["disagreements"]:
        print("\nDisagreements:")
        for row in report["disagreements"]:
            print(f"- {row['id']} [{row['rule']}]")
            for k in ("route", "intents", "queue", "priority"):
                if row[k]:
                    print(f"    {k:<9} {row[k]}")
            print(f"    why       {row['why']}")
    gate = not report["unsafe_automations"] and not report["missing_results"]
    print(f"\nRelease gate (0 unsafe automations): {'PASS' if gate else 'FAIL'}")
    return 0 if gate else 1


if __name__ == "__main__":
    sys.exit(main())
