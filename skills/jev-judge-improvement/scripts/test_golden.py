import argparse
import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile
from unittest import mock
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import candidates  # noqa: E402
import golden  # noqa: E402

A, B, C, D, E = (f"0000000{n}-0000-0000-0000-000000000000" for n in "abcde")


def row(call, question, answer, reviewer="kyle", judge=None, reviewed_at="2026-10-08T18:00:00+00:00"):
    return {"interaction_id": call, "question": question, "reviewer": reviewer, "answer": answer, "review_date": "2026-10-07", "reviewed_at": reviewed_at, "judge_answer": judge}


FAKE_S2S = {
    "livekit/__init__.py": "",
    "livekit/agents/__init__.py": "class ChatContext:\n    @staticmethod\n    def from_dict(data):\n        return data\n",
    "abita_s2s/__init__.py": "",
    "abita_s2s/observability/__init__.py": "",
    "abita_s2s/observability/judges/__init__.py": "JURORS = ('j',)\nQUESTIONS = {'right_help': {'type': 'noul'}, 'expressed_sentiment': {'type': 'score'}}\n",
    "abita_s2s/observability/evaluation.py": (
        "CALLS = []\n"
        "async def evaluate_judges(history, *, api_key, seconds):\n"
        "    CALLS.append({'api_key': api_key, 'seconds': seconds})\n"
        "    return {'results': {'right_help': {'verdict': False, 'probability': 0.2, 'votes': {'j': 0.2}, 'errors': {}},"
        " 'expressed_sentiment': {'score': 2, 'votes': {'j': 2}, 'errors': {}}}, 'errors': {}}\n"
    ),
}
EARLIER = "2026-10-01T18:00:00+00:00"


def v(probability):
    return {"verdict": probability > 0.40, "probability": probability, "votes": {"typesafe-ai/jev": probability}}


class GoldenTest(unittest.TestCase):
    def golden(self):
        rows = [
            row(A, "right_help", False, judge=True),
            row(B, "right_help", True, judge=True, reviewed_at=EARLIER),
            row(C, "right_help", False, judge=False, reviewed_at=EARLIER),
            row(D, "right_help", True, judge=True),
            row(D, "right_help", False, reviewer="chase"),
            row(B, "booking_requested", False, reviewed_at=EARLIER),
        ]
        return golden.build_golden(rows, [{"interaction_id": C, "question_idea": "x"}], "2026-10-08")

    def test_groups_sets_by_pacific_review_day_and_drops_reviewer_splits(self):
        calls = self.golden()
        self.assertEqual((calls[A]["reviewed_day"], calls[A]["today"]), ("2026-10-08", True))
        self.assertEqual((calls[B]["reviewed_day"], calls[B]["today"]), ("2026-10-01", False))
        self.assertEqual((calls[D]["truth"], calls[D]["split"]), ({}, ["right_help"]))
        summary = golden.today_summary(calls)
        self.assertEqual(summary["jev_misses_today"], {"right_help": [{"call": A[:8], "human": False, "judge": True}]})
        self.assertEqual(summary["reviewers_split_today"], {"right_help": [D[:8]]})
        self.assertEqual(summary["golden_sets"], ["2026-10-01", "2026-10-08"])

    def test_reword_passes_only_when_it_fixes_today_without_regressing_any_set(self):
        calls = self.golden()
        baseline = {A: {"right_help": v(0.9)}, B: {"right_help": v(0.9)}, C: {"right_help": v(0.2)}}
        fixed = {A: {"right_help": v(0.2)}, B: {"right_help": v(0.9)}, C: {"right_help": v(0.1)}}
        result = golden.gate_reword(calls, baseline, fixed, "right_help")
        self.assertTrue(result["pass"])
        self.assertEqual(result["fixed_today"], [A[:8]])
        self.assertEqual(result["sets"]["2026-10-01"], {"compared": 2, "before": 2, "after": 2})
        regressed = {A: {"right_help": v(0.2)}, B: {"right_help": v(0.3)}, C: {"right_help": v(0.1)}}
        result = golden.gate_reword(calls, baseline, regressed, "right_help")
        self.assertFalse(result["pass"])
        self.assertEqual(result["regressions"], [{"call": B[:8], "set": "2026-10-01"}])
        broken = {A: {"right_help": v(0.2)}, B: {"right_help": "error:Timeout"}, C: {"right_help": v(0.1)}}
        self.assertEqual(golden.gate_reword(calls, baseline, broken, "right_help")["errors"], [B[:8]])

    def test_calls_without_a_transcript_are_listed_not_counted_as_failures(self):
        calls = self.golden()
        baseline = {A: {"right_help": v(0.9)}, B: {"right_help": "error:no_transcript"}, C: {"right_help": v(0.2)}}
        candidate = {A: {"right_help": v(0.2)}, B: {"right_help": "error:no_transcript"}, C: {"right_help": v(0.1)}}
        result = golden.gate_reword(calls, baseline, candidate, "right_help")
        self.assertTrue(result["pass"], result)
        self.assertEqual((result["errors"], result["unjudged"]), ([], [B[:8]]))

    def test_new_question_needs_ten_caught_calls_and_no_alarm_on_clean_golden_calls(self):
        calls = self.golden()
        evidence = [f"{n:08x}-1111-0000-0000-000000000000" for n in range(10)]
        answers = {i: {"new_q": v(0.1)} for i in evidence}
        answers.update({A: {"new_q": v(0.1)}, B: {"new_q": v(0.9)}, C: {"new_q": v(0.1)}, D: {"new_q": v(0.1)}})
        result = golden.gate_new(calls, evidence, answers, "new_q")
        self.assertEqual(result["clean_golden_calls"], 1)
        self.assertTrue(result["pass"], result)
        answers[B] = {"new_q": v(0.2)}
        self.assertEqual(golden.gate_new(calls, evidence, answers, "new_q")["false_alarms"], [B[:8]])
        self.assertFalse(golden.gate_new(calls, evidence[:9], answers, "new_q")["pass"])

    def test_gate_skips_are_misses_on_evidence_and_quiet_on_golden_calls(self):
        calls = self.golden()
        evidence = [f"{n:08x}-1111-0000-0000-000000000000" for n in range(10)]
        answers = {i: {"new_q": v(0.1)} for i in evidence}
        answers[evidence[0]] = {"new_q": "not_applicable"}
        answers.update({A: {"new_q": v(0.9)}, B: {"new_q": "not_applicable"}, C: {"new_q": v(0.9)}, D: {"new_q": "error:Timeout"}})
        result = golden.gate_new(calls, evidence, answers, "new_q")
        self.assertEqual((result["missed"], result["false_alarms"], result["errors"]), ([evidence[0][:8]], [], [D[:8]]))

    def test_a_second_review_today_brings_an_earlier_call_back_into_today(self):
        rows = [row(B, "right_help", True, reviewed_at=EARLIER), row(B, "right_help", False, reviewer="chase")]
        call = golden.build_golden(rows, [], "2026-10-08")[B]
        self.assertEqual((call["reviewed_day"], call["today"], call["split"]), ("2026-10-01", True, ["right_help"]))

    def test_refuses_output_inside_a_repo_even_when_parents_are_missing(self):
        with self.assertRaises(SystemExit):
            golden.ensure_out(Path(__file__).resolve().parent / "missing" / "deeper", fresh=True)

    def test_false_alarms_and_misses_by_question_with_call_review_misses_only(self):
        calls = self.golden()
        answers = {A: {"right_help": v(0.9)}, B: {"right_help": v(0.1)}, C: {"right_help": v(0.9)}, E: {"right_help": v(0.9)}}
        self.assertEqual(golden.disagreements(calls, answers, {"right_help": [E]}), {"right_help": {"misses": [A[:8]], "call_review_misses": [E[:8]]}})
        self.assertEqual(golden.counts(calls, answers), {"right_help": {"agreed": 0, "false_alarms": 1, "misses": 2}})

    def test_a_reword_that_fixes_a_call_review_miss_passes_and_breaking_one_is_a_regression(self):
        calls = self.golden()
        baseline = {A: {"right_help": v(0.2)}, B: {"right_help": v(0.9)}, C: {"right_help": v(0.2)}, E: {"right_help": v(0.9)}}
        fixed = {**baseline, E: {"right_help": v(0.1)}}
        result = golden.gate_reword(calls, baseline, fixed, "right_help", [E])
        self.assertEqual((result["pass"], result["fixed_today"]), (True, [E[:8]]))
        self.assertEqual(result["sets"]["call-review"], {"compared": 1, "before": 0, "after": 1})
        broken = golden.gate_reword(calls, fixed, baseline, "right_help", [E])
        self.assertEqual(broken["regressions"], [{"call": E[:8], "set": "call-review"}])

    def test_run_asks_the_full_batch_once_like_production_and_keeps_yes_no_answers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path, text in FAKE_S2S.items():
                (root / "s2s" / "src" / path).parent.mkdir(parents=True, exist_ok=True)
                (root / "s2s" / "src" / path).write_text(text)
            out = root / "out"
            out.mkdir()
            calls = self.golden()
            (out / "golden.json").write_text(json.dumps({"today": "2026-10-08", "evidence": [], "call_review": {"right_help": [E]}, "calls": calls}))
            (out / "transcripts.json").write_text(json.dumps({A: {"items": []}, B: {"items": []}, C: {"items": []}, E: {"items": []}}))
            args = argparse.Namespace(s2s=root / "s2s", out=out, name="t")
            with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test"}), contextlib.redirect_stdout(io.StringIO()):
                asyncio.run(golden.run_jev(args))
            from abita_s2s.observability import evaluation
            self.assertEqual(evaluation.CALLS, [{"api_key": "test", "seconds": 120}] * 4)
            answers = json.loads((out / "runs" / "t.json").read_text())["answers"]
            self.assertEqual(answers[A], {"right_help": {"verdict": False, "probability": 0.2, "votes": {"j": 0.2}}})
            self.assertEqual(answers[D], {"right_help": "error:no_transcript"})
            self.assertIn(E, answers)

    def test_every_call_review_finding_reaches_the_gate_not_only_the_misses(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "judge_misses.json"
            path.write_text(json.dumps({"by_question": {"promises_backed": {"findings": 4, "missed": [A], "caught": [B, A], "not_judged": [C]}}, "unassigned": []}))
            self.assertEqual(golden.call_review_calls(path), {"promises_backed": [A, B, C]})

    def test_answer_of_reads_the_jury_result(self):
        judged = {"results": {"a": {"verdict": False, "probability": 0.2, "votes": {"j": 0.2}, "errors": {}}, "g": {"status": "not_applicable", "reason": "x"}}, "errors": {"b": {"cause": "no_quorum"}}}
        self.assertEqual([golden.answer_of(judged, q) for q in "abgz"], [{"verdict": False, "probability": 0.2, "votes": {"j": 0.2}}, "error:no_quorum", "not_applicable", "error:missing"])

    def test_not_applicable_is_a_no_only_for_time_offered(self):
        self.assertTrue(golden.answered_no("not_applicable", "time_offered"))
        self.assertIsNone(golden.answered_no("not_applicable", "appointment_datetime_correct"))
        self.assertIsNone(golden.answered_no("error:Timeout", "right_help"))
        self.assertTrue(golden.answered_no(v(0.40), "right_help"))


class DatabaseTest(unittest.TestCase):
    def test_local_database_must_be_local_and_read_only(self):
        with self.assertRaises(SystemExit):
            golden.local_env("postgres://user:pw@10.0.0.5/prod")
        self.assertEqual(golden.local_env("postgres://127.0.0.1/acuity_local")["PGOPTIONS"], "-c default_transaction_read_only=on")


class CandidatesTest(unittest.TestCase):
    def test_theme_is_ready_at_ten_distinct_calls_and_only_then_approvable(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = {"themes": {}}
            for n in range(9):
                candidates.add(ledger, "callback_promised", f"{n:08x}-0000-0000-0000-000000000000", "note", "2026-10-08")
            theme = candidates.add(ledger, "callback_promised", f"{0:08x}-0000-0000-0000-000000000000", "review", "2026-10-09")
            self.assertEqual((len(theme["calls"]), candidates.state(theme)), (9, "watching"))
            candidates.add(ledger, "callback_promised", f"{9:08x}-0000-0000-0000-000000000000", "review", "2026-10-09", "Did the agent promise a callback it could not schedule?")
            self.assertEqual(candidates.rows(ledger)[0]["state"], "ready")
            path = Path(directory) / "candidates.json"
            candidates.save(path, ledger)
            self.assertEqual(candidates.load(path), ledger)

    def test_rejects_free_text_where_ids_belong(self):
        with self.assertRaises(SystemExit):
            candidates.add({"themes": {}}, "theme", "Jane Doe 555-0100", "note", "2026-10-08")
        with self.assertRaises(SystemExit):
            candidates.add({"themes": {}}, "Bad Key", A, "note", "2026-10-08")


if __name__ == "__main__":
    unittest.main()
