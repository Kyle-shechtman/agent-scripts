#!/usr/bin/env python3
"""Track new-question candidates until a theme has enough calls to backtest.

A theme is a failure no judge question covers. Each call that shows it is counted
once: a reviewer's missing-question note, a call-review finding no question should
have caught, or a false alarm that really shows it. A theme is ready at 10 calls;
it is added only after Kyle or Chase approve it.

Usage:
  candidates.py show
  candidates.py add KEY --call ID --source note|call-review|review --date YYYY-MM-DD [--question TEXT]
  candidates.py evidence KEY
  candidates.py status KEY approved|shipped|rejected|watching [--pr URL]

The ledger holds theme keys, draft questions, and interaction ids only; never
transcript text, notes, names, or phone numbers. It lives at
~/.acuity/jev-judge-improvement/candidates.json unless --ledger is given.
"""

import argparse
import json
import re
import sys
from pathlib import Path

READY_AT = 10
STATUSES = ("watching", "approved", "shipped", "rejected")
DEFAULT_LEDGER = Path.home() / ".acuity" / "jev-judge-improvement" / "candidates.json"


def load(path):
    return json.loads(path.read_text()) if path.exists() else {"themes": {}}


def save(path, ledger):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=1, sort_keys=True) + "\n")


def add(ledger, key, call, source, day, question=None):
    if not re.fullmatch(r"[a-z][a-z_]{0,59}", key):
        sys.exit(f"theme key must be snake_case: {key}")
    if not re.fullmatch(r"[0-9a-f-]{36}", call):
        sys.exit(f"not an interaction id: {call}")
    theme = ledger["themes"].setdefault(key, {"question": "", "status": "watching", "calls": {}, "pr": ""})
    if question:
        theme["question"] = question
    theme["calls"].setdefault(call, {"source": source, "date": day})
    return theme


def state(theme):
    if theme["status"] == "watching" and len(theme["calls"]) >= READY_AT:
        return "ready"
    return theme["status"]


def rows(ledger):
    return [
        {"theme": key, "state": state(theme), "calls": len(theme["calls"]), "needs": max(0, READY_AT - len(theme["calls"])), "question": theme["question"], "pr": theme["pr"]}
        for key, theme in sorted(ledger["themes"].items(), key=lambda item: -len(item[1]["calls"]))
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show")
    a = sub.add_parser("add")
    a.add_argument("key")
    a.add_argument("--call", required=True)
    a.add_argument("--source", required=True, choices=("note", "call-review", "review"))
    a.add_argument("--date", required=True)
    a.add_argument("--question")
    e = sub.add_parser("evidence")
    e.add_argument("key")
    s = sub.add_parser("status")
    s.add_argument("key")
    s.add_argument("status", choices=STATUSES)
    s.add_argument("--pr", default="")
    args = parser.parse_args()

    ledger = load(args.ledger)
    if args.command == "show":
        print(json.dumps(rows(ledger), indent=1))
        return
    if args.command == "add":
        theme = add(ledger, args.key, args.call, args.source, args.date, args.question)
        save(args.ledger, ledger)
        print(json.dumps({"theme": args.key, "state": state(theme), "calls": len(theme["calls"])}))
        return
    theme = ledger["themes"].get(args.key)
    if theme is None:
        sys.exit(f"unknown theme {args.key}")
    if args.command == "evidence":
        print(",".join(sorted(theme["calls"])))
        return
    if args.status == "approved" and len(theme["calls"]) < READY_AT:
        sys.exit(f"{args.key} has {len(theme['calls'])} calls; a new question needs {READY_AT} before it can be approved")
    theme["status"] = args.status
    theme["pr"] = args.pr or theme["pr"]
    save(args.ledger, ledger)
    print(json.dumps({"theme": args.key, "state": state(theme)}))


if __name__ == "__main__":
    main()
