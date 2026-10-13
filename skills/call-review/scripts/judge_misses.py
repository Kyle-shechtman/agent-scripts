#!/usr/bin/env python3
"""Count, per judge question, the findings it should have caught but said yes on.

Usage: judge_misses.py OUT

Reads OUT/judge_questions.json (the failure questions and evaluator version from
abita_s2s), OUT/index.json (each call's stored jury verdicts and their version), and
OUT/all_findings.json (each finding's `should_flag`: the question whose "no" should
have caught it, or null). A verdict from another evaluator version is not judged:
its wording differs. Adds `judge_verdict` to every finding and writes
OUT/judge_misses.json:

  by_question  question -> findings, missed (the jury said yes: a judge miss),
               caught (it said no), not_judged (no stored verdict), as call ids
  unassigned   findings no question fits, for the new-question theme ledger

Ids are interaction ids; the file holds no transcript text.
"""

import json
import sys
from pathlib import Path

def tally(judges, index, findings):
    questions = sorted(judges["questions"])
    calls = {row["call"]: row for row in index}
    by_question, unassigned = {}, []
    for finding in findings:
        if "should_flag" not in finding:
            sys.exit(f"finding on call {finding.get('call')} has no should_flag")
        question = finding["should_flag"]
        call = calls[finding["call"]]
        if question is None:
            finding["judge_verdict"] = None
            unassigned.append({"call": call["call"], "id": call["id"]})
            continue
        if question not in questions:
            sys.exit(f"finding on call {finding['call']} names {question!r}; should_flag must be one of {', '.join(questions)} or null")
        current = call.get("judge_version") == judges["version"]
        verdict = call.get("judges", {}).get(question, "not_judged") if current else "not_judged"
        finding["judge_verdict"] = verdict
        row = by_question.setdefault(question, {"findings": 0, "missed": [], "caught": [], "not_judged": []})
        row["findings"] += 1
        bucket = "missed" if verdict is True else "caught" if verdict is False else "not_judged"
        if call["id"] not in row[bucket]:
            row[bucket].append(call["id"])
    return {"by_question": dict(sorted(by_question.items())), "unassigned": unassigned}


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        sys.exit(__doc__)
    out = Path(args[0])
    judges = json.loads((out / "judge_questions.json").read_text())
    index = json.loads((out / "index.json").read_text())
    findings = json.loads((out / "all_findings.json").read_text())
    result = tally(judges, index, findings)
    (out / "all_findings.json").write_text(json.dumps(findings, indent=2))
    (out / "judge_misses.json").write_text(json.dumps(result, indent=2))
    summary = {q: {k: v if k == "findings" else len(v) for k, v in row.items()} for q, row in result["by_question"].items()}
    print(json.dumps({"by_question": summary, "unassigned": len(result["unassigned"])}, indent=2))


if __name__ == "__main__":
    main()
