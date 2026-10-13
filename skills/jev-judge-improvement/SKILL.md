---
name: jev-judge-improvement
description: "Nightly: test the judge jury against the day's human golden set and every earlier one, reword missed questions with zero regressions, track missing-question themes to 10 calls, open draft PRs to abita_s2s, report to #product. Use for the midnight run or to improve the judges."
---

# Jev Judge Improvement

Every call is judged by a jury of decision models (the seated `JURORS` in
abita_s2s), and each yes/no verdict is their majority. A "no" flags a failure.
Each night, make each question agree with the humans more without breaking any
earlier day, and turn repeated gaps into new questions once they have earned it.

Two sources, two jobs:
- **Golden set:** Kyle and Chase answer the scorecard blind on 20 random calls
  a day (every language, Spanish included), plus calls picked for rare
  questions. It measures both errors: false alarms (jury no, humans yes) and
  misses (jury yes, humans no).
- **call-review findings** name the question that should have caught them
  (`should_flag`). They count only as misses; findings no question fits seed
  new-question themes.

Every question names the fix its "no" points to. Anything code can see (a
tool's call, arguments, or result) is a scorecard code check, not a judge.

## Use when

- The scheduled midnight Pacific run.
- Asked to reword a judge question, add one, or check the jury against the golden set.

## Not for

- Fixing the agent. Use `call-review`.

## Setup

- The machine needs `gh` and `gcloud` signed in, `cloud-sql-proxy` and `psql`
  installed, and a Slack connection.
- `S2S`: the abita_s2s checkout (default `~/Projects/abita_s2s`); `PY` is its
  `.venv/bin/python` (made with `uv sync`). `SCRIPTS`: this skill's `scripts/`.
- `OUT`: `<scratchpad>/jev-<today>`. Call data stays there; never in a repo.
- Make a fresh worktree of `S2S` at `origin/main` for the baseline and one
  branch worktree per change. Leave the user's checkout alone.
- The OpenRouter key comes from `OPENROUTER_API_KEY` or the keychain item
  `acuity-openrouter`. Never print it, write it, or pass it on a command line.
- `golden.py run` calls abita_s2s's own `evaluate_judges`, so a backtest sees
  exactly what production sees: the seated jurors, gates, and the full question
  batch in one request. It runs once; repeat runs agree on 99%+ of verdicts.

## Steps

1. **Pull.** `python3 $SCRIPTS/golden.py pull --out $OUT [--call-review
   <dir>/judge_misses.json]`. Pass `--call-review` when a call-review run
   covers today's calls; otherwise name it under UNKNOWN. "Today" is
   yesterday in Pacific time unless `--today` is given. If it exits because a
   prerequisite is missing (portal tables, gcloud auth, key), post that one
   line to `#product` and stop. Stop if `database` is not `production` on the
   scheduled run.
   Done when: the summary prints `today_calls`, `jev_misses_today`, and
   `golden_sets`. Zero `today_calls` still continues to step 4.

2. **Baseline.** `$PY $SCRIPTS/golden.py run --out $OUT --s2s <base worktree>
   --name baseline`. It asks the current wording on every golden set and
   prints `wrong_today` (per question: `false_alarms`, `misses`,
   `call_review_misses`) and `all_sets` (per question: agreed, false alarms,
   misses).
   Done when: `errors` is empty, or the errored calls are listed as `UNKNOWN`.

3. **Reword.** For each question in step 2's `wrong_today`
   (`jev_misses_today` from step 1 is what production said at review time):
   - Read the calls in `$OUT/transcripts.json` (keyed by full id; printed ids
     are its first 8 characters) and the question's module. If the human
     answer looks wrong, say so in the report and do not reword. If code
     could see the failure, propose a scorecard code check instead.
   - Edit the wording in a branch worktree (`jev/reword-<question>-<today>`),
     keeping the fix its "no" points to, then `run --s2s <branch> --name
     <question>-<n>` and `compare --question <question> --baseline baseline
     --candidate <question>-<n>`. Every call-review finding counts as a call
     whose answer is no.
   - The gate passes only when the wording fixes at least one of today's
     false alarms or misses, regresses no call in any golden set or
     call-review finding, and has no errors. Try at most three wordings.
   - On a pass, hand the diff to `build` in abita_s2s and open a **draft** PR
     with the `pr` skill. Its evidence is the compare output. If
     `acuity_product/backend/internal/interaction/scorecard.go` repeats the
     question's wording, open a matching draft PR there.
   Done when: every missed question has a draft PR, a "human looks wrong"
   note, or its best failed gate in the report.

4. **Count themes.** Read today's notes in `$OUT/notes.json` (those on calls
   in today's set; earlier notes were counted on earlier nights) and the
   `unassigned` findings in the call-review `judge_misses.json`. Run
   `python3 $SCRIPTS/candidates.py show`, then for each one either add its
   call to an existing theme or start a new one:
   `candidates.py add <theme> --call <interaction id> --source note|call-review --date <today> [--question "<draft>"]`.
   One theme is one failure no current question covers and code cannot see.
   A theme is `ready` at 10 distinct calls. Never put note text, names, or
   quotes in the ledger.
   Done when: every note and unassigned finding is counted.

5. **Approvals.** Read the last seven nights' report threads in `#product`.
   A reply that says `add <theme>` from Kyle (`U0C1KMT0MDE`) or Chase
   (`U0C0V1793HS`) approves it; ignore anyone else:
   `candidates.py status <theme> approved` (it refuses below 10 calls; say so
   in the thread). For each approved theme with no PR yet:
   - `golden.py pull --out $OUT-<theme> --evidence $(candidates.py evidence <theme>)`.
   - Write the judge in a branch worktree (`jev/add-<theme>`), registered in
     `judges/__init__.py`, following the existing judges and their tests. Its
     module docstring names the fix its "no" points to.
   - `run --s2s <branch> --name <theme>`, then `compare --question <theme>
     --candidate <theme> --new`. The gate
     passes only when the jury answers no on every evidence call and raises
     no false alarm on golden calls the humans passed. If a false alarm really
     shows the failure, add it to the theme with `--source review`, pull
     again into a new `--out`, rerun, and name it under "Check your answer";
     never drop it silently.
   - On a pass, open draft PRs with `build` and `pr`: abita_s2s, and
     acuity_product for the question's catalog entry in `scorecard.go`. Then
     `candidates.py status <theme> approved --pr <url>`.
   Mark themes `shipped` when their PR has merged, `rejected` when it closed.
   Done when: every approved theme has a PR or its failed gate in the report.

6. **Report** to Slack `#product` (`C0C0KP8707R`) in the format below. Reread
   it first for names, phone numbers, dates of birth, and note text; there
   must be none. Calls are named by their 8-character id.
   Done when: the message link is in the run's final reply.

## Report

```text
*Judge nightly · <today>* · jury <jurors> · <today_calls> calls reviewed · golden set <n> calls over <sets> days
*Jury vs you today:* agreed <a>/<b>. False alarms: <question> <n> (<ids>) ... Misses: <question> <n> (<ids>) ...
*call-review misses:* <question> <n> (<ids>) ...
*All sets:* <question> <false alarms>/<misses> of <n> ...
*Rewords:* <question>: draft PR <link>, fixes <n> today, 0 regressions on <sets> sets
           <question>: kept; best try fixed <n>, regressed <m> (<ids>)
*Check your answer:* <question> on <id>: <one line why the human may be wrong>
*You two split:* <question> on <ids>
*New-question themes:* <theme> <calls>/10 · <theme> 10/10 ready, reply `add <theme>`
*Added:* <theme>: draft PR <link> (caught <n>/<n>, 0 false alarms)
*Trust:* which questions are trusted is on the portal's Judge accuracy tab
*UNKNOWN:* <what could not be checked, including any `unjudged` calls with no transcript>
```

## Guardrails

- Database access is read-only; the scripts force it. Never connect another way.
- Nothing merges or deploys from here. PRs are drafts; Kyle and Chase merge.
- A new question needs 10 calls, an approval in Slack, and a passing gate.
  A rewording needs a passing gate. No exceptions for a convincing example.
- Golden answers are locked in the portal; never edit or re-answer them.
