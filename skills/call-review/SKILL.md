---
name: call-review
description: "Review production Abita calls for hallucinations, failures, and prompt fixes, judged against the prompts that were live. Use when asked to review calls, find agent hallucinations or failures, or decide what prompt changes to make."
---

# Call Review

Find what Abita got wrong on real calls, prove it from the transcript, and turn
the worst of it into prompt and code fixes.

## Use when

- Reviewing a day or range of production calls.
- Deciding what prompt or tool changes to make from real calls.

## Not for

- Testing a change before release. Use the evals in `abita_s2s/evals/`.

## Steps

1. **Pull.** Run, with a New York calendar day:

   ```bash
   ~/acuity_business/skills/call-review/scripts/pull_calls.py --date YYYY-MM-DD [--days N] --out <scratchpad>/calls-YYYY-MM-DD --s2s <abita_s2s checkout>
   ```

   `--s2s` must hold the judge set that was live; `judge_questions.json` lists
   its failure questions for `should_flag`, and only verdicts from its
   evaluator version count in step 4.

   It opens a read-only session through the Cloud SQL proxy, renders one file
   per call with full tool results, and exports the prompts that were live for
   each call. It refuses an output directory inside a git repository. If gcloud
   auth fails, ask the user to run `gcloud auth login`.
   Done when: the script exits 0 and `prompts_missing` is empty, or the missing
   tags are reported as `UNKNOWN`.

2. **Read the stats** in `stats.json` before any transcript: calls with no
   caller speech, tool outcomes by tool (`failed`, `blocked`, `needs_input`),
   transfer rate and what ran before each transfer, staff drafts versus tasks
   staff received, and judge status. Read the calls behind odd numbers first.
   Done when: every odd number has a one-line note.

3. **Fan out.** Start one fresh subagent per batch in `batches.json`, in
   parallel, each with `references/reviewer-brief.md` filled in.
   Done when: every batch has a findings file, or its reviewer is reported as
   `FAILED`.

4. **Verify.** Open the transcript for every high-severity finding and confirm
   the quote, its timestamp, and the claimed missing evidence yourself.
   Reviewers misread tool results; drop or downgrade what does not hold, and
   reject fixes that remove a safety net. Check each `should_flag` names the
   question whose "no" would have caught it. Merge the verified findings into
   `all_findings.json`, then run `scripts/judge_misses.py <out>`: it adds the
   jury's stored verdict to each finding and writes `judge_misses.json`. A
   finding whose question said yes is a judge miss; findings with
   `should_flag` null are `unassigned` and feed the new-question themes in
   `jev-judge-improvement`.
   Done when: every high-severity finding is marked `verified` or `rejected`,
   and `judge_misses.py` exits 0.

5. **Report** using the output template. Group findings into patterns by
   cause, not by call.
   Done when: the report is written.

6. **Post** the report to Slack `#product` (`C0C0KP8707R`). First reread it for
   names, phone numbers, and dates of birth; there must be none. If it is over
   5,000 characters, post the title, hallucinations, and patterns, and put the
   rest in a thread reply. Keep `all_findings.json` in the scratchpad.
   Done when: the message link is in the reply to the user.

7. **Hand off.** For each pattern the user wants fixed, write a synthetic eval
   scenario in `abita_s2s/evals/scenarios/` that reproduces it, and hand the
   scenario and the prompt diff to the `build` skill in `abita_s2s`.
   Done when: the user has chosen what to fix, or chosen nothing.

## Output

```markdown
# Call review: <window>, <n> calls, prompts <tags>

## Hallucinations
| Call | At | Said | Truth | Cause | Should flag |

## Patterns
1. <pattern>: <n> calls (<call numbers>). Cause: <component>.
   Fix: <file: old sentence -> new sentence>

## Code fixes
- <tool or platform change>: <calls>

## Judges missed
| Question | Findings | Missed (said yes) | Caught | Not judged |
No question fits: <n> findings (to the theme ledger)

## Stats
<odd numbers from step 2, one line each>

## UNKNOWN
<what could not be verified and why>
```

## Guardrails

- Database access is read-only. The script forces it; never connect another way.
- Call data stays in the scratchpad. Reports, Slack posts, scenarios, PRs, and
  artifacts use call numbers, never names, phone numbers, or dates of birth.
- Eval scenarios are synthetic: they reproduce the failure, not the caller.
- A finding without a quoted transcript line is `UNKNOWN`, not a finding.
