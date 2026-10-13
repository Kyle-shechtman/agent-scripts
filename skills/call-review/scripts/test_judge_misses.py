"""Tests for judge_misses.py. Synthetic calls only; no patient data."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import judge_misses
import pull_calls

A, B, C = (f"0000000{n}-0000-0000-0000-000000000000" for n in "abc")


def finding(call, should_flag):
    return {"call": call, "category": "failure", "severity": "high", "summary": "x", "evidence": [], "should_flag": should_flag}


class StoredVerdicts(unittest.TestCase):
    def test_reads_yes_no_skip_and_error_and_ignores_scores(self):
        closeout = {"evaluation": {"results": {
            "promises_backed": {"verdict": True, "probability": 0.9, "votes": {}},
            "office_rules_grounded": {"verdict": False, "probability": 0.1, "votes": {}},
            "appointment_datetime_correct": {"status": "not_applicable", "reason": "x"},
            "expressed_sentiment": {"score": 2, "votes": {}},
        }, "errors": {"ended_while_caller_active": {"cause": "no_quorum"}}}}
        self.assertEqual(pull_calls.judge_verdicts({"closeout": closeout}), {
            "promises_backed": True, "office_rules_grounded": False,
            "appointment_datetime_correct": "not_applicable", "ended_while_caller_active": "error",
        })
        self.assertEqual(pull_calls.judge_verdicts({"closeout": None}), {})


V7 = {"version": "typesafe-scorecard-v7", "questions": {"promises_backed": {"fix": "x", "instructions": "y"}}}


class JudgeQuestions(unittest.TestCase):
    def test_failure_questions_are_registered_judges_that_name_their_fix(self):
        files = {
            "evaluation.py": 'EVALUATOR_VERSION = "typesafe-scorecard-v7"\n',
            "judges/__init__.py": "from x import a\nQUESTIONS = {'promises_backed': a.QUESTION, 'time_offered': b.QUESTION}\n",
            "judges/promises_backed.py": '"""Promises backed.\n\nFix for a no: speaker rules.\n"""\nQUESTION = {"type": "noul", "instructions": "Did a tool back it?"}\n',
            "judges/time_offered.py": '"""Time offered.\n\nNot a failure: fact.\n"""\nQUESTION = {"type": "noul", "instructions": "Offered?"}\n',
            "judges/unregistered.py": '"""Fix for a no: never.\n"""\nQUESTION = {"instructions": "z"}\n',
        }
        with tempfile.TemporaryDirectory() as root:
            for path, text in files.items():
                target = Path(root) / pull_calls.JUDGES / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text)
            self.assertEqual(pull_calls.judge_questions(Path(root)), {
                "version": "typesafe-scorecard-v7",
                "questions": {"promises_backed": {"fix": "speaker rules.", "instructions": "Did a tool back it?"}},
            })


class Misses(unittest.TestCase):
    def test_a_finding_whose_question_said_yes_is_a_judge_miss(self):
        v7 = "typesafe-scorecard-v7"
        index = [
            {"call": 1, "id": A, "judge_version": v7, "judges": {"promises_backed": True}},
            {"call": 2, "id": B, "judge_version": v7, "judges": {"promises_backed": False}},
            {"call": 3, "id": C, "judge_version": v7, "judges": {}},
        ]
        findings = [finding(1, "promises_backed"), finding(1, "promises_backed"), finding(2, "promises_backed"),
                    finding(3, "promises_backed"), finding(3, None)]
        result = judge_misses.tally(V7, index, findings)
        self.assertEqual([f["judge_verdict"] for f in findings], [True, True, False, "not_judged", None])
        self.assertEqual(result["by_question"], {"promises_backed": {"findings": 4, "missed": [A], "caught": [B], "not_judged": [C]}})
        self.assertEqual(result["unassigned"], [{"call": 3, "id": C}])

    def test_a_verdict_from_another_evaluator_version_is_not_judged(self):
        index = [{"call": 1, "id": A, "judge_version": "typesafe-scorecard-v6", "judges": {"promises_backed": True}}]
        result = judge_misses.tally(V7, index, [finding(1, "promises_backed")])
        self.assertEqual(result["by_question"]["promises_backed"]["not_judged"], [A])

    def test_refuses_a_question_key_that_is_not_a_judge(self):
        with self.assertRaises(SystemExit):
            judge_misses.tally(V7, [{"call": 1, "id": A, "judges": {}}], [finding(1, "right_hlp")])
        with self.assertRaises(SystemExit):
            judge_misses.tally(V7, [{"call": 1, "id": A, "judges": {}}], [{"call": 1}])

    def test_writes_verdicts_into_findings_and_the_summary_file(self):
        with tempfile.TemporaryDirectory() as root:
            out = Path(root)
            (out / "judge_questions.json").write_text(json.dumps(V7))
            (out / "index.json").write_text(json.dumps([{"call": 1, "id": A, "judge_version": V7["version"], "judges": {"promises_backed": True}}]))
            (out / "all_findings.json").write_text(json.dumps([finding(1, "promises_backed")]))
            with contextlib.redirect_stdout(io.StringIO()):
                judge_misses.main([str(out)])
            self.assertEqual(json.loads((out / "all_findings.json").read_text())[0]["judge_verdict"], True)
            self.assertEqual(json.loads((out / "judge_misses.json").read_text())["by_question"]["promises_backed"]["missed"], [A])


if __name__ == "__main__":
    unittest.main()
