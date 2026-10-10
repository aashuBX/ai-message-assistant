"""CLI:  python -m triage [messages.json] [--no-llm] [--model M] [--out DIR] [--concurrency N] [--no-verify] [--quiet]

Exit codes: 0 = every record triaged (some may be routed to humans because of bad data or an LLM
outage, which is the intended behaviour); 2 = the input file itself could not be read as JSON.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from .config import DEFAULT_CONCURRENCY, DEFAULT_MODEL
from .llm import GroqClassifier
from .pipeline import triage_records
from .report import compute_stats, print_console, write_outputs


class InputFileError(Exception):
    pass


def load_records(path: str) -> list:
    try:
        raw = Path(path).read_bytes()
    except OSError as e:
        raise InputFileError(f"cannot read {path}: {e.strerror or e}") from e
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        print(f"warning: {path} is not valid UTF-8; undecodable bytes replaced", file=sys.stderr)
        text = raw.decode("utf-8", errors="replace")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise InputFileError(f"{path} is not valid JSON (line {e.lineno}, column {e.colno}: {e.msg})") from e
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("messages", "data", "items", "records"):
            if isinstance(data.get(key), list):
                return data[key]
        if "text" in data or "id" in data:
            return [data]
    raise InputFileError(f"{path}: expected a JSON array of messages, got {type(data).__name__}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m triage", description="Triage inbound customer messages.")
    parser.add_argument("input", nargs="?", default="messages.json", help="JSON array of messages")
    parser.add_argument("--out", help="output directory (default: output/<input file name>)")
    parser.add_argument("--no-llm", action="store_true",
                        help="rules only, no API calls: every message is routed to a human with a priority")
    parser.add_argument("--model", help=f"Groq model ID (default: $GROQ_MODEL or {DEFAULT_MODEL})")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                        help=f"parallel model calls (default {DEFAULT_CONCURRENCY}; use 1 on Groq's free plan)")
    parser.add_argument("--no-verify", action="store_true",
                        help="skip the second-opinion call on messages about to be automated")
    parser.add_argument("--quiet", action="store_true", help="no per-message table on stdout")
    args = parser.parse_args(argv)

    load_dotenv(find_dotenv(usecwd=True))
    try:
        records = load_records(args.input)
    except InputFileError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    classifier = None
    model = args.model or os.environ.get("GROQ_MODEL") or DEFAULT_MODEL
    if not args.no_llm:
        key = (os.environ.get("GROQ_API_KEY") or "").strip()
        if key:
            classifier = GroqClassifier(api_key=key, model=model,
                                        base_url=os.environ.get("GROQ_BASE_URL") or None,
                                        reasoning_effort=os.environ.get("GROQ_REASONING_EFFORT") or None)
        else:
            print("warning: GROQ_API_KEY is not set, so running rules-only: nothing is automated and every "
                  "message goes to a human queue with a rule-based priority.", file=sys.stderr)

    started = time.monotonic()
    results = asyncio.run(triage_records(records, classifier, args.concurrency, verify=not args.no_verify))
    disabled = classifier.disabled_reason if classifier else None
    if disabled:
        print(f"warning: LLM disabled during the run ({disabled}); affected messages were routed to humans.",
              file=sys.stderr)

    stats = compute_stats(results, input_name=args.input, mode="llm" if classifier else "rules_only",
                          model=model if classifier else None, elapsed_s=time.monotonic() - started,
                          llm_disabled=disabled)
    out_dir = Path(args.out) if args.out else Path("output") / Path(args.input).stem
    write_outputs(results, stats, out_dir)
    if not args.quiet:
        print_console(results, stats)
    print(f"\nWrote {out_dir}/results.json, summary.md, run_stats.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
