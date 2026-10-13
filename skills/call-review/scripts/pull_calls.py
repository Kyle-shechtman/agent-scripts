#!/usr/bin/env python3
"""Pull production Abita calls read-only and render them for review.

Usage: pull_calls.py --date YYYY-MM-DD [--days N] --out DIR [--s2s PATH]

Writes to DIR (which must be empty and outside any git repository):
  calls/call-NNN.md   one file per call: metadata, judges, full transcript
  index.json          call number -> interaction id, start, prompt tag, jury verdicts
  stats.json          cheap stats to read before any transcript
  batches.json        size-balanced batches of call files for reviewers
  prompts/<tag>/      speaker.md and thinker.md as they were at each tag
  judge_questions.json  the --s2s checkout's evaluator version and failure questions
                      (judges whose docstring names a "Fix for a no:"), for should_flag

The database credential stays in this process's memory and is never written.
"""

import argparse
import ast
import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

PROJECT = "acuity-health-prod"
INSTANCE = "acuity-health-prod:us-east1:acuity-production"
# Least-privileged role with SELECT on ai_interactions and work_tasks; there is
# no read-only role, so every session also forces read-only transactions.
SECRET = "acuity-product-east1-portal-database-url"
PORT = 54329
ZONE = ZoneInfo("America/New_York")
BATCHES = 6
JUDGES = "src/abita_s2s/observability"
PROMPTS = {"speaker.md": "src/abita_s2s/prompts/speaker.md", "thinker.md": "src/abita_s2s/prompts/thinker.md"}

QUERY = """
select json_build_object(
  'id', i.id,
  'practice_id', i.practice_id,
  'service_subject', i.service_subject,
  'started_at', i.started_at,
  'ended_at', i.ended_at,
  'status', i.status,
  'appointment_outcome', i.appointment_outcome,
  'version_agent', i.version_agent,
  'version_prompts', i.version_prompts,
  'closeout', i.closeout_payload,
  'transcript', i.transcript,
  'work_tasks', (
    select coalesce(json_agg(json_build_object(
      'origin', t.origin, 'category', t.category, 'state', t.state,
      'title', t.title, 'source_message', t.source_message)), '[]'::json)
    from work_tasks t
    where t.practice_id = i.practice_id and t.source_call_id = i.source_call_id)
)
from ai_interactions i
where i.started_at >= '{start}' and i.started_at < '{end}'
order by i.started_at
"""


def day_window(day, days):
    """Return the UTC bounds of `days` New York calendar days from `day`.

    Bounds are computed here, not in SQL: `date ... at time zone` in Postgres
    shifts the window by the server's timezone (incident 2026-10-07).
    """
    first = date.fromisoformat(day)
    last = first + timedelta(days=days)
    start = datetime(first.year, first.month, first.day, tzinfo=ZONE)
    end = datetime(last.year, last.month, last.day, tzinfo=ZONE)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def connection_env(database_url, port):
    url = urlsplit(database_url)
    return {
        "PGHOST": "127.0.0.1",
        "PGPORT": str(port),
        "PGUSER": unquote(url.username or ""),
        "PGPASSWORD": unquote(url.password or ""),
        "PGDATABASE": url.path.lstrip("/"),
        "PGSSLMODE": "disable",  # the proxy encrypts the connection
        "PGOPTIONS": "-c default_transaction_read_only=on",
    }


def prompt_tag(c):
    if c.get("version_prompts"):
        return f"prompts-v{c['version_prompts']}"
    if c.get("version_agent"):
        return f"v{c['version_agent']}"
    return None


def prepare_out(out):
    out = out.resolve()
    probe = out if out.exists() else out.parent
    inside = subprocess.run(
        ["git", "-C", str(probe), "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True
    )
    if inside.stdout.strip() == "true":
        sys.exit(f"refusing {out}: it is inside a git repository; call data must stay out of repos")
    if out.exists() and any(out.iterdir()):
        sys.exit(f"refusing {out}: directory is not empty")
    (out / "calls").mkdir(parents=True, exist_ok=True)
    (out / "findings").mkdir()
    return out


def chat_items(transcript):
    transcript = transcript or {}
    for key in ("chat_history", "chatHistory"):
        if isinstance(transcript.get(key), dict) and transcript[key].get("items"):
            return transcript[key]["items"]
    return transcript.get("items") or []


def item_time(item):
    value = item.get("created_at", item.get("createdAt", item.get("occurredAt")))
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e12 else float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def message_text(item):
    if isinstance(item.get("text"), str):
        return item["text"]
    parts = []
    for part in item.get("content") or []:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict):
            parts.append(part.get("text") or part.get("transcript") or "")
    return " ".join(p for p in parts if p).strip()


def as_text(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def transcript_lines(c):
    items = chat_items(c.get("transcript"))
    times = [t for t in (item_time(i) for i in items) if t is not None]
    origin = min(times) if times else None
    names = {i.get("call_id"): i.get("name") for i in items if i.get("type") == "function_call"}
    lines = []
    for item in items:
        t = item_time(item)
        stamp = "--:--" if t is None or origin is None else "{:02d}:{:02d}".format(*divmod(int(t - origin), 60))
        kind = item.get("type") or "message"
        if kind == "message":
            who = {"user": "caller", "assistant": "agent"}.get(item.get("role"), item.get("role") or "unknown")
            if item.get("interrupted"):
                who += " (interrupted)"
            lines.append(f"[{stamp}] {who}: {message_text(item)}")
        elif kind == "function_call":
            lines.append(f"[{stamp}] tool call {item.get('name')}({as_text(item.get('arguments'))})")
        elif kind == "function_call_output":
            status = " ERROR" if item.get("is_error") else ""
            name = names.get(item.get("call_id"), "unknown")
            lines.append(f"[{stamp}] tool result {name}{status}: {as_text(item.get('output'))}")
        else:
            lines.append(f"[{stamp}] ({kind})")
    return lines


def render_call(number, c):
    closeout = c.get("closeout") or {}
    evaluation = closeout.get("evaluation") or {}
    started = datetime.fromisoformat(c["started_at"]).astimezone(ZONE)
    duration = "UNKNOWN"
    if c.get("ended_at"):
        duration = f"{int((datetime.fromisoformat(c['ended_at']) - datetime.fromisoformat(c['started_at'])).total_seconds())}s"
    out = [
        f"# Call {number:03d}",
        "",
        f"- Interaction: {c['id']}",
        f"- Started: {started:%Y-%m-%d %H:%M:%S %Z}, duration {duration}",
        f"- Service: {c.get('service_subject')}, prompts {prompt_tag(c) or 'UNKNOWN'}",
        f"- Status: {c.get('status')}, outcome {c.get('appointment_outcome')}",
        f"- Close: {closeout.get('closeReason')}, transfer {closeout.get('transferStatus')}",
        "",
        f"## Judges ({evaluation.get('status', 'missing')})",
        "",
    ]
    for judge, result in (evaluation.get("results") or {}).items():
        answer = dict((result.get("answers") or {}).get(judge) or result)
        answer.pop("probabilities", None)
        out.append(f"- {judge}: {as_text(answer)}")
    for judge, error in (evaluation.get("errors") or {}).items():
        out.append(f"- {judge}: ERROR {as_text(error)}")
    out += ["", "## Domain outcomes", ""]
    out += [f"- {as_text(o)}" for o in closeout.get("domainOutcomes") or []] or ["- none"]
    out += ["", "## Staff tasks", ""]
    out += [f"- {as_text(t)}" for t in c.get("work_tasks") or []] or ["- none"]
    out += ["", "## Transcript", ""]
    out += transcript_lines(c)
    return "\n".join(out) + "\n"


def judge_verdicts(c):
    """The jury's stored yes/no verdict per question: True, False, "not_applicable", or "error"."""
    evaluation = (c.get("closeout") or {}).get("evaluation") or {}
    verdicts = {}
    for question, result in (evaluation.get("results") or {}).items():
        if result.get("status") == "not_applicable":
            verdicts[question] = "not_applicable"
        elif isinstance(result.get("verdict"), bool):
            verdicts[question] = result["verdict"]
    for question in evaluation.get("errors") or {}:
        verdicts[question] = "error"
    return verdicts


def assigned(tree, name):
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return node.value
    sys.exit(f"{name} not found in the abita_s2s judges")


def judge_questions(s2s):
    """The evaluator version and every registered judge that names the fix its "no" points to."""
    root = s2s / JUDGES
    version = ast.literal_eval(assigned(ast.parse((root / "evaluation.py").read_text()), "EVALUATOR_VERSION"))
    keys = [ast.literal_eval(k) for k in assigned(ast.parse((root / "judges" / "__init__.py").read_text()), "QUESTIONS").keys]
    questions = {}
    for key in keys:
        module = ast.parse((root / "judges" / f"{key}.py").read_text())
        fix = re.search(r"Fix for a no: (.+)", ast.get_docstring(module) or "")
        if fix:
            questions[key] = {"fix": fix.group(1).strip(), "instructions": ast.literal_eval(assigned(module, "QUESTION"))["instructions"]}
    return {"version": version, "questions": questions}


def tool_outcome(item):
    """Abita tools answer "<outcome>: <answer>"; errors raised by the runtime set is_error."""
    if item.get("is_error"):
        return "error"
    match = re.match(r"([a-z_]+): ", as_text(item.get("output")))
    return match.group(1) if match else "other"


def draft_id(output_item, call_item):
    """Updates to one staff request reuse its draft ID; a cancel names it in its arguments."""
    match = re.search(r"Draft ID: (\S+)", as_text(output_item.get("output")))
    if match:
        return match.group(1)
    arguments = (call_item or {}).get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return None
    return (arguments or {}).get("draft_id") if isinstance(arguments, dict) else None


def compute_stats(calls):
    stats = {
        "calls": len(calls),
        "prompt_tags": {},
        "judge_status": {},
        "no_caller_speech": [],
        "tool_results": {},
        "transfers": {"rate": 0.0, "calls": []},
        "staff_tasks": {"drafts": 0, "work_tasks": 0, "mismatches": []},
    }
    for number, c in enumerate(calls, 1):
        tag = prompt_tag(c) or "UNKNOWN"
        stats["prompt_tags"][tag] = stats["prompt_tags"].get(tag, 0) + 1
        judged = ((c.get("closeout") or {}).get("evaluation") or {}).get("status", "missing")
        stats["judge_status"][judged] = stats["judge_status"].get(judged, 0) + 1

        items = chat_items(c.get("transcript"))
        caller = [i for i in items if (i.get("type") or "message") == "message" and i.get("role") == "user"]
        if not any(message_text(i) for i in caller):
            stats["no_caller_speech"].append({"call": number, "caller_items": len(caller)})

        calls_by_id = {i.get("call_id"): i for i in items if i.get("type") == "function_call"}
        tools_run, drafts, transferred = [], {}, False
        for item in items:
            if item.get("type") == "function_call":
                if item.get("name") == "transfer_call" and not transferred:
                    transferred = True
                    stats["transfers"]["calls"].append({"call": number, "tools_before": list(tools_run)})
                tools_run.append(item.get("name"))
            elif item.get("type") == "function_call_output":
                name = (calls_by_id.get(item.get("call_id")) or {}).get("name", "unknown")
                result = tool_outcome(item)
                counts = stats["tool_results"].setdefault(name, {})
                counts[result] = counts.get(result, 0) + 1
                if name == "save_staff_task":
                    draft = draft_id(item, calls_by_id.get(item.get("call_id")))
                    if draft and result in ("saved", "cancelled"):
                        drafts[draft] = result == "saved"

        pending = sum(drafts.values())
        delivered = sum(1 for t in c.get("work_tasks") or [] if t.get("origin") == "ABITA_AI")
        stats["staff_tasks"]["drafts"] += pending
        stats["staff_tasks"]["work_tasks"] += delivered
        if pending != delivered:
            stats["staff_tasks"]["mismatches"].append({"call": number, "drafts": pending, "work_tasks": delivered})
    if calls:
        stats["transfers"]["rate"] = round(len(stats["transfers"]["calls"]) / len(calls), 3)
    return stats


def balance(sizes, count):
    batches = [[] for _ in range(min(count, len(sizes)))]
    totals = [0] * len(batches)
    for name, size in sorted(sizes.items(), key=lambda kv: -kv[1]):
        smallest = totals.index(min(totals))
        batches[smallest].append(name)
        totals[smallest] += size
    return batches


def run(cmd, **kwargs):
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        sys.exit(f"{cmd[0]} failed: {result.stderr.strip()}")
    return result.stdout


def fetch_calls(start, end):
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", PORT)) == 0:
            sys.exit(f"port {PORT} is already in use; stop the other proxy first")
    proxy = subprocess.Popen(
        ["cloud-sql-proxy", "--gcloud-auth", "--address", "127.0.0.1", "--port", str(PORT), INSTANCE],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    try:
        for _ in range(30):
            if proxy.poll() is not None:
                sys.exit(f"cloud-sql-proxy exited: {proxy.stderr.read().strip()}")
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", PORT)) == 0:
                    break
            time.sleep(1)
        else:
            sys.exit("cloud-sql-proxy did not become ready in 30s")
        url = run(["gcloud", "secrets", "versions", "access", "latest", "--project", PROJECT, "--secret", SECRET]).strip()
        env = {**os.environ, **connection_env(url, PORT)}
        sql = QUERY.format(start=start.isoformat(), end=end.isoformat())
        output = run(["psql", "-X", "-q", "-t", "-A", "-v", "ON_ERROR_STOP=1"], input=sql, env=env)
    finally:
        proxy.terminate()
        proxy.wait(timeout=10)
    return [json.loads(line) for line in output.splitlines() if line.strip()]


def export_prompts(s2s, tags, out):
    missing = []
    for tag in sorted(tags):
        target = out / "prompts" / tag
        target.mkdir(parents=True, exist_ok=True)
        for filename, path in PROMPTS.items():
            shown = subprocess.run(["git", "-C", str(s2s), "show", f"{tag}:{path}"], capture_output=True, text=True)
            if shown.returncode != 0:
                missing.append(f"{tag}:{path}")
                continue
            (target / filename).write_text(shown.stdout)
    return missing


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", required=True, help="first New York calendar day, YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--s2s", type=Path, default=Path.home() / "abita_s2s")
    args = parser.parse_args()

    judges = judge_questions(args.s2s)
    out = prepare_out(args.out)
    (out / "judge_questions.json").write_text(json.dumps(judges, indent=2))
    start, end = day_window(args.date, args.days)
    calls = fetch_calls(start, end)
    if not calls:
        sys.exit(f"no calls between {start.isoformat()} and {end.isoformat()}")

    sizes, index = {}, []
    for number, c in enumerate(calls, 1):
        name = f"calls/call-{number:03d}.md"
        text = render_call(number, c)
        (out / name).write_text(text)
        sizes[name] = len(text)
        evaluation = (c.get("closeout") or {}).get("evaluation") or {}
        index.append({"call": number, "id": c["id"], "started_at": c["started_at"], "prompts": prompt_tag(c),
                      "judge_version": evaluation.get("evaluatorVersion"), "judges": judge_verdicts(c)})

    stats = compute_stats(calls)
    stats["window_utc"] = [start.isoformat(), end.isoformat()]
    stats["judge_questions"] = {"version": judges["version"], "failure_questions": sorted(judges["questions"]),
                                "calls_on_this_version": sum(1 for row in index if row["judge_version"] == judges["version"])}
    stats["prompts_missing"] = export_prompts(args.s2s, {t for t in (prompt_tag(c) for c in calls) if t}, out)
    (out / "index.json").write_text(json.dumps(index, indent=2))
    (out / "stats.json").write_text(json.dumps(stats, indent=2))
    (out / "batches.json").write_text(json.dumps(balance(sizes, BATCHES), indent=2))
    print(json.dumps(stats, indent=2))
    if stats["prompts_missing"]:
        print(f"WARNING: prompts missing for {stats['prompts_missing']}; run git -C {args.s2s} fetch --tags", file=sys.stderr)


if __name__ == "__main__":
    main()
