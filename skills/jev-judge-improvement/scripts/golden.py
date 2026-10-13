#!/usr/bin/env python3
"""Backtest Jev questions on every golden set, so no rewording or new question regresses.

  pull     read-only: every human golden-set answer, the reviewers' missing-question
           notes, and the transcripts the jury needs. Prints what production got
           wrong on today's set. --call-review adds every call-review finding.
  run      ask the seated jury the full question batch as worded in an abita_s2s
           checkout, once, through its own evaluator, exactly as production does, on
           every golden call (and any extra calls). Prints false alarms and misses per
           question. Run with the abita_s2s virtualenv.
  compare  gate one question of a candidate run against a baseline run.

Usage:
  golden.py pull --out DIR [--today YYYY-MM-DD] [--call-review FILE] [--evidence ID,ID]
  <s2s>/.venv/bin/python golden.py run --out DIR --s2s PATH --name NAME
  golden.py compare --out DIR --question KEY --baseline NAME --candidate NAME
  golden.py compare --out DIR --question KEY --candidate NAME --new

A "no" flags a failure. A false alarm is the jury saying no where the humans said
yes; a miss is the jury saying yes where the humans said no. Golden answers
measure both. call-review findings measure misses only: each one is a call
where its question should have said no.

"Today" is the Pacific calendar day whose reviews are judged; it defaults to
yesterday, because the nightly run starts at midnight Pacific. DIR must be
outside any git repository. Output names calls by the first 8 characters of
their interaction id and never prints transcript text or notes.

The OpenRouter key comes from OPENROUTER_API_KEY or, failing that, the macOS
keychain item "acuity-openrouter". It is never written or printed.
"""

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "call-review" / "scripts"))
import pull_calls  # noqa: E402

PACIFIC = ZoneInfo("America/Los_Angeles")
UNJUDGEABLE = {"error:no_transcript"}
NEW_QUESTION_MIN_EVIDENCE = 10
FACT_QUESTIONS = {"booking_requested", "new_booking_requested", "time_offered"}  # v6 and v7 names
NOT_APPLICABLE_MEANS_NO = {"time_offered"}
GOLDEN_EXISTS = "select to_regclass('public.ai_call_reviews') is not null"
GOLDEN_QUERY = """
select json_build_object('interaction_id', r.interaction_id, 'question', r.question, 'reviewer', r.reviewer,
  'answer', r.answer, 'review_date', r.review_date, 'reviewed_at', r.reviewed_at, 'judge_answer', r.judge_answer)
from ai_call_reviews r
order by r.reviewed_at, r.interaction_id, r.question
"""
NOTES_QUERY = """
select json_build_object('interaction_id', a.interaction_id, 'review_date', a.review_date,
  'reviewer', a.reviewer, 'question_idea', a.question_idea)
from ai_call_review_assignments a
where a.question_idea <> ''
order by a.review_date, a.interaction_id
"""
TRANSCRIPT_QUERY = """
select json_build_object('id', i.id, 'transcript', i.transcript)
from ai_interactions i where i.id in ({ids})
"""


def refuse_repo(out):
    """Call data must stay out of repos; check the nearest folder that exists."""
    probe = out
    while not probe.exists():
        probe = probe.parent
    inside = subprocess.run(["git", "-C", str(probe), "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True)
    if inside.stdout.strip() == "true":
        sys.exit(f"refusing {out}: it is inside a git repository; call data must stay out of repos")


def database_label():
    return "local" if os.environ.get("CALL_REVIEW_DATABASE_URL") else "production"


def local_env(database_url):
    url = urlsplit(database_url)
    if url.hostname not in ("127.0.0.1", "localhost", "::1"):
        sys.exit("CALL_REVIEW_DATABASE_URL must point at a local database")
    return {
        "PGHOST": url.hostname, "PGPORT": str(url.port or 5432), "PGUSER": unquote(url.username or os.environ.get("USER", "")),
        "PGPASSWORD": unquote(url.password or ""), "PGDATABASE": url.path.lstrip("/"), "PGSSLMODE": "disable",
        "PGOPTIONS": "-c default_transaction_read_only=on",
    }


@contextlib.contextmanager
def database():
    """Yield query(sql) -> output lines over one read-only session: production through call-review's
    Cloud SQL proxy settings, or a local database from CALL_REVIEW_DATABASE_URL."""
    def query_with(env):
        return lambda sql: [line for line in pull_calls.run(["psql", "-X", "-q", "-t", "-A", "-v", "ON_ERROR_STOP=1"], input=sql, env=env).splitlines() if line.strip()]

    local = os.environ.get("CALL_REVIEW_DATABASE_URL")
    if local:
        print("reading the LOCAL database from CALL_REVIEW_DATABASE_URL, not production", file=sys.stderr)
        yield query_with({**os.environ, **local_env(local)})
        return
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", pull_calls.PORT)) == 0:
            sys.exit(f"port {pull_calls.PORT} is already in use; stop the other proxy first")
    proxy = subprocess.Popen(
        ["cloud-sql-proxy", "--gcloud-auth", "--address", "127.0.0.1", "--port", str(pull_calls.PORT), pull_calls.INSTANCE],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    try:
        for _ in range(30):
            if proxy.poll() is not None:
                sys.exit(f"cloud-sql-proxy exited: {proxy.stderr.read().strip()}")
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", pull_calls.PORT)) == 0:
                    break
            time.sleep(1)
        else:
            sys.exit("cloud-sql-proxy did not become ready in 30s")
        url = pull_calls.run(["gcloud", "secrets", "versions", "access", "latest", "--project", pull_calls.PROJECT, "--secret", pull_calls.SECRET]).strip()
        yield query_with({**os.environ, **pull_calls.connection_env(url, pull_calls.PORT)})
    finally:
        proxy.terminate()
        proxy.wait(timeout=10)


def short(interaction_id):
    return interaction_id[:8]


def unjudgeable(value):
    return isinstance(value, str) and value in UNJUDGEABLE


def is_error(value):
    return isinstance(value, str) and value.startswith("error")


def answered_no(value, question):
    if value == "not_applicable":
        return True if question in NOT_APPLICABLE_MEANS_NO else None
    return not value["verdict"] if isinstance(value, dict) else None


def pacific_day(moment):
    return datetime.fromisoformat(moment).astimezone(PACIFIC).date().isoformat()


def build_golden(rows, notes, today):
    """Group review rows by call. A question two reviewers answered differently has no truth.

    A call belongs to the golden set of the Pacific day it was first reviewed. It
    also counts as today's when any review of it was saved today, so a second
    reviewer's answer or split is checked the night it arrives.
    """
    calls = {}
    for row in rows:
        day = pacific_day(row["reviewed_at"])
        call = calls.setdefault(row["interaction_id"], {"review_date": row["review_date"], "reviewed_day": day, "days": set(), "answers": {}, "judge": {}})
        call["reviewed_day"] = min(call["reviewed_day"], day)
        call["days"].add(day)
        call["answers"].setdefault(row["question"], {})[row["reviewer"]] = row["answer"]
        if row.get("judge_answer") is not None:
            call["judge"][row["question"]] = row["judge_answer"]
    noted = {note["interaction_id"] for note in notes}
    for interaction_id, call in calls.items():
        call["today"] = today in call.pop("days")
        call["noted"] = interaction_id in noted
        call["truth"] = {q: next(iter(set(a.values()))) for q, a in call["answers"].items() if len(set(a.values())) == 1}
        call["split"] = sorted(q for q, a in call["answers"].items() if len(set(a.values())) > 1)
    return calls


def today_summary(golden):
    misses, splits = {}, {}
    for interaction_id, call in sorted(golden.items()):
        if not call["today"]:
            continue
        for question, human in call["truth"].items():
            judge = call["judge"].get(question)
            if judge is not None and judge != human:
                misses.setdefault(question, []).append({"call": short(interaction_id), "human": human, "judge": judge})
        for question in call["split"]:
            splits.setdefault(question, []).append(short(interaction_id))
    return {
        "today_calls": sum(call["today"] for call in golden.values()),
        "golden_calls": len(golden),
        "golden_sets": sorted({call["reviewed_day"] for call in golden.values()}),
        "jev_misses_today": misses,
        "reviewers_split_today": splits,
    }


def disagreements(calls, answers, call_review):
    """Per question: today's golden calls where this run said no but the humans said yes
    (false alarms) or yes where they said no (misses), and call-review findings it still
    says yes on."""
    result = {}
    for interaction_id, call in sorted(calls.items()):
        if not call["today"]:
            continue
        for question, human in call["truth"].items():
            no = answered_no(answers.get(interaction_id, {}).get(question), question)
            if no is None or (not no) == human:
                continue
            kind = "false_alarms" if no else "misses"
            result.setdefault(question, {}).setdefault(kind, []).append(short(interaction_id))
    for question, ids in sorted(call_review.items()):
        missed = [short(i) for i in ids if answered_no(answers.get(i, {}).get(question), question) is False]
        if missed:
            result.setdefault(question, {})["call_review_misses"] = missed
    return result


def counts(calls, answers):
    """Per question across every golden set: agreed, false alarms, misses."""
    table = {}
    for interaction_id, call in calls.items():
        for question, human in call["truth"].items():
            no = answered_no(answers.get(interaction_id, {}).get(question), question)
            if no is None:
                continue
            row = table.setdefault(question, {"agreed": 0, "false_alarms": 0, "misses": 0})
            row["agreed" if (not no) == human else "false_alarms" if no else "misses"] += 1
    return dict(sorted(table.items()))


def gate_reword(golden, baseline, candidate, question, call_review=()):
    """A rewording ships only if it fixes a disagreement and no call that agreed before now disagrees.

    call-review findings for the question count as today's calls whose truth is no.
    """
    sets, fixed, regressions, errors, unjudged = {}, [], [], [], []
    calls = {i: {"truth": {question: False}, "reviewed_day": "call-review", "today": True} for i in call_review}
    calls.update(golden)
    for interaction_id, call in sorted(calls.items()):
        truth = call["truth"].get(question)
        if truth is None:
            continue
        raw = baseline.get(interaction_id, {}).get(question), candidate.get(interaction_id, {}).get(question)
        if any(unjudgeable(value) for value in raw):
            unjudged.append(short(interaction_id))
            continue
        before, after = (answered_no(value, question) for value in raw)
        if before is None or after is None:
            if any(is_error(value) for value in raw):
                errors.append(short(interaction_id))
            continue
        row = sets.setdefault(call["reviewed_day"], {"compared": 0, "before": 0, "after": 0})
        row["compared"] += 1
        agreed_before = (not before) == truth
        agreed_after = (not after) == truth
        row["before"] += agreed_before
        row["after"] += agreed_after
        if agreed_after and not agreed_before:
            fixed.append({"call": short(interaction_id), "today": call["today"]})
        if agreed_before and not agreed_after:
            regressions.append({"call": short(interaction_id), "set": call["reviewed_day"]})
    fixed_today = [f["call"] for f in fixed if f["today"]]
    return {
        "question": question,
        "sets": sets,
        "fixed_today": fixed_today,
        "fixed_earlier": [f["call"] for f in fixed if not f["today"]],
        "regressions": regressions,
        "errors": errors,
        "unjudged": unjudged,
        "pass": bool(fixed_today) and not regressions and not errors,
    }


def clean(call):
    """A golden call humans passed: no failure answered no and no missing-question note."""
    return not call["noted"] and not call["split"] and all(answer for q, answer in call["truth"].items() if q not in FACT_QUESTIONS)


def gate_new(golden, evidence, candidate, question):
    """A new question ships only if it catches every evidence call and raises no alarm on clean golden calls.

    A call the question's gate skips is a miss on evidence and no alarm on golden calls.
    """
    raw = {i: candidate.get(i, {}).get(question) for i in set(evidence) | set(golden)}
    unjudged = sorted(short(i) for i, value in raw.items() if unjudgeable(value))
    raw = {i: value for i, value in raw.items() if not unjudgeable(value)}
    evidence = [i for i in evidence if i in raw]
    golden = {i: call for i, call in golden.items() if i in raw}
    errors = sorted(short(i) for i, value in raw.items() if value is None or is_error(value))
    answers = {i: answered_no(value, question) is True for i, value in raw.items()}
    caught = sorted(short(i) for i in evidence if answers[i])
    missed = sorted(short(i) for i in evidence if not answers[i] and not is_error(raw[i]) and raw[i] is not None)
    clean_calls = [i for i, call in golden.items() if clean(call) and i not in evidence]
    alarms = sorted(short(i) for i in clean_calls if answers[i] is True)
    return {
        "question": question,
        "evidence": len(evidence),
        "caught": caught,
        "missed": missed,
        "clean_golden_calls": len(clean_calls),
        "false_alarms": alarms,
        "errors": errors,
        "unjudged": unjudged,
        "pass": len(evidence) >= NEW_QUESTION_MIN_EVIDENCE and not missed and not alarms and not errors,
    }


def openrouter_key():
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    found = subprocess.run(["security", "find-generic-password", "-s", "acuity-openrouter", "-w"], capture_output=True, text=True)
    if found.returncode != 0 or not found.stdout.strip():
        sys.exit("no OpenRouter key: set OPENROUTER_API_KEY or add the keychain item acuity-openrouter")
    return found.stdout.strip()


def ensure_out(out, fresh):
    out = out.resolve()
    refuse_repo(out)
    if fresh and out.exists() and any(out.iterdir()):
        sys.exit(f"refusing {out}: directory is not empty")
    if not fresh and not (out / "golden.json").exists():
        sys.exit(f"{out} has no golden.json; run pull first")
    out.mkdir(parents=True, exist_ok=True)
    return out


def call_review_calls(path):
    """Every finding a question should have flagged is a call whose answer is no, whatever the jury said then."""
    by_question = json.loads(path.read_text())["by_question"]
    return {q: sorted({*row["missed"], *row["caught"], *row["not_judged"]}) for q, row in by_question.items()}


def pull(args):
    out = ensure_out(args.out, fresh=True)
    today = args.today or (datetime.now(PACIFIC).date() - timedelta(days=1)).isoformat()
    call_review = call_review_calls(args.call_review) if args.call_review else {}
    evidence = [i for i in args.evidence.split(",") if i]
    with database() as query:
        if query(GOLDEN_EXISTS) != ["t"]:
            sys.exit("ai_call_reviews is not in this database yet: ship the portal review tables first")
        rows = [json.loads(line) for line in query(GOLDEN_QUERY)]
        notes = [json.loads(line) for line in query(NOTES_QUERY)]
        ids = sorted({row["interaction_id"] for row in rows} | set(evidence) | {i for c in call_review.values() for i in c})
        bad = [i for i in ids if not re.fullmatch(r"[0-9a-f-]{36}", i)]
        if bad:
            sys.exit(f"not interaction ids: {bad}")
        transcripts = {}
        for start in range(0, len(ids), 200):
            chunk = ",".join(f"'{i}'::uuid" for i in ids[start:start + 200])
            for line in query(TRANSCRIPT_QUERY.format(ids=chunk)):
                call = json.loads(line)
                transcripts[call["id"]] = call["transcript"]
    golden = build_golden(rows, notes, today)
    (out / "golden.json").write_text(json.dumps({"today": today, "evidence": evidence, "call_review": call_review, "calls": golden}, indent=1))
    (out / "notes.json").write_text(json.dumps(notes, indent=1))
    (out / "transcripts.json").write_text(json.dumps(transcripts))
    summary = today_summary(golden)
    summary["today"] = today
    summary["database"] = database_label()
    summary["today_review_dates"] = sorted({call["review_date"] for call in golden.values() if call["today"]})
    summary["call_review_findings"] = {q: len(c) for q, c in call_review.items()}
    summary["question_notes"] = {"total": len(notes), "today": sum(1 for n in notes if golden.get(n["interaction_id"], {}).get("today"))}
    summary["missing_transcripts"] = [short(i) for i in ids if i not in transcripts]
    print(json.dumps(summary, indent=1))


def answer_of(judged, question):
    if question in judged["errors"]:
        return f"error:{judged['errors'][question].get('cause', 'unknown')}"
    result = judged["results"].get(question)
    if result is None:
        return "error:missing"
    if result.get("status") == "not_applicable":
        return "not_applicable"
    return {"verdict": result["verdict"], "probability": result["probability"], "votes": result["votes"]}


def wording(question):
    return hashlib.sha256(json.dumps(question, sort_keys=True).encode()).hexdigest()[:12]


async def run_jev(args):
    sys.path.insert(0, str(args.s2s.resolve() / "src"))
    from livekit.agents import ChatContext
    from abita_s2s.observability.evaluation import evaluate_judges
    from abita_s2s.observability.judges import JURORS, QUESTIONS

    out = ensure_out(args.out, fresh=False)
    data = json.loads((out / "golden.json").read_text())
    transcripts = json.loads((out / "transcripts.json").read_text())
    keys = [k for k, q in QUESTIONS.items() if q["type"] == "noul"]
    key = openrouter_key()
    targets = sorted(set(data["calls"]) | set(data["evidence"]) | {i for c in data.get("call_review", {}).values() for i in c})
    semaphore = asyncio.Semaphore(4)

    async def one(interaction_id):
        transcript = transcripts.get(interaction_id)
        if not transcript:
            return interaction_id, {q: "error:no_transcript" for q in keys}
        history = ChatContext.from_dict(transcript.get("chat_history") or transcript.get("chatHistory") or transcript)
        async with semaphore:
            # The whole batch in one request, as production asks it; repeat runs agree on 99%+ of verdicts.
            judged = await evaluate_judges(history, api_key=key, seconds=120)
        return interaction_id, {q: answer_of(judged, q) for q in keys}

    answers = dict(await asyncio.gather(*(one(i) for i in targets)))
    runs = out / "runs"
    runs.mkdir(exist_ok=True)
    (runs / f"{args.name}.json").write_text(json.dumps({"s2s": str(args.s2s), "jurors": list(JURORS), "wording": {k: wording(QUESTIONS[k]) for k in keys}, "answers": answers}, indent=1))
    errors = sorted({v for a in answers.values() for v in a.values() if is_error(v)})
    print(json.dumps({
        "run": args.name, "jurors": list(JURORS), "calls": len(answers), "questions": keys, "errors": errors,
        "wrong_today": disagreements(data["calls"], answers, data.get("call_review", {})),
        "all_sets": counts(data["calls"], answers),
    }, indent=1))


def compare(args):
    out = ensure_out(args.out, fresh=False)
    data = json.loads((out / "golden.json").read_text())
    candidate = json.loads((out / "runs" / f"{args.candidate}.json").read_text())["answers"]
    if not any(args.question in a for a in candidate.values()):
        sys.exit(f"{args.candidate} has no answers for {args.question}")
    if args.new:
        result = gate_new(data["calls"], data["evidence"], candidate, args.question)
    else:
        if not args.baseline:
            sys.exit("--baseline is required unless --new")
        baseline = json.loads((out / "runs" / f"{args.baseline}.json").read_text())["answers"]
        result = gate_reword(data["calls"], baseline, candidate, args.question, data.get("call_review", {}).get(args.question, []))
    print(json.dumps(result, indent=1))
    sys.exit(0 if result["pass"] else 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("pull")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--today", type=lambda v: date.fromisoformat(v).isoformat())
    p.add_argument("--call-review", type=Path, help="judge_misses.json from a call-review run; each finding is a call whose answer is no")
    p.add_argument("--evidence", default="", help="interaction ids a new question must answer no on (for --new)")
    r = sub.add_parser("run")
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--s2s", type=Path, required=True)
    r.add_argument("--name", required=True)
    c = sub.add_parser("compare")
    c.add_argument("--out", type=Path, required=True)
    c.add_argument("--question", required=True)
    c.add_argument("--candidate", required=True)
    c.add_argument("--baseline")
    c.add_argument("--new", action="store_true")
    args = parser.parse_args()
    if args.command == "pull":
        pull(args)
    elif args.command == "run":
        asyncio.run(run_jev(args))
    else:
        compare(args)


if __name__ == "__main__":
    main()
