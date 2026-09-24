from __future__ import annotations

import json
import sqlite3
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from robot_trials.api import JsonApplication
from robot_trials.clock import FrozenClock
from robot_trials.comparison import bootstrap_difference_interval, compare
from robot_trials.contracts import ComparisonSpec
from robot_trials.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from robot_trials.jsonio import load_json, load_observations, load_protocol
from robot_trials.service import TrialService


ROOT = Path(__file__).resolve().parents[1]


def _improved_rows(rows: list[dict]) -> list[dict]:
    candidate = []
    for row in rows:
        updated = deepcopy(row)
        updated["metrics"]["completed"] = 1
        updated["metrics"]["completion_seconds"] = format(
            Decimal(str(row["metrics"]["completion_seconds"])) - Decimal(5), "f"
        )
        updated["metrics"]["interventions"] = 0
        candidate.append(updated)
    return candidate


class ComparisonServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = TrialService(self.connection, self.clock)
        for user_id, role in (
            ("operator", "operator"),
            ("stat", "statistician"),
            ("approver", "approver"),
            ("auditor", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.protocol = load_json(ROOT / "fixtures" / "demo_protocol.json")
        self.rows = [
            json.loads(line)
            for line in (ROOT / "fixtures" / "demo_observations.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.service.register_robot("operator", "robot-a", "A 型", "厂商")
        self.service.register_robot("operator", "robot-b", "B 型", "厂商")
        self.service.register_build("operator", "build-a1", "robot-a", "1.0", "a" * 64)
        self.service.register_build("operator", "build-a2", "robot-a", "1.1", "b" * 64)
        self.service.register_build("operator", "build-b1", "robot-b", "1.0", "d" * 64)
        self.service.publish_protocol("stat", self.protocol)
        self.rules = {
            "rules": [
                {"metric": "completed", "rule": "non_inferior", "margin": "0.05"},
                {"metric": "completion_seconds", "rule": "non_inferior", "margin": "2"},
                {"metric": "interventions", "rule": "non_inferior", "margin": "0.5"},
            ]
        }

    def tearDown(self) -> None:
        self.connection.close()

    def _run_batch(self, batch_id: str, build_id: str, rows: list[dict], key: str) -> int:
        self.service.create_batch("operator", batch_id, "demo-delivery-v1", 1, build_id)
        self.service.start_batch("operator", batch_id, 1)
        self.service.import_observations("operator", batch_id, key, rows)
        self.service.seal_batch("stat", batch_id, 2)
        job = self.service.claim_job("worker", 30)
        analysis = self.service.complete_job("worker", job["job_id"], "stat")
        return analysis["analysis_id"]

    def _baseline_and_candidate(self) -> tuple[int, int]:
        baseline_analysis = self._run_batch("batch-base", "build-a1", self.rows, "key-base")
        candidate_analysis = self._run_batch("batch-cand", "build-a2", _improved_rows(self.rows), "key-cand")
        return baseline_analysis, candidate_analysis

    def _compare(self, baseline_analysis: int, candidate_analysis: int, rules: dict | None = None) -> dict:
        return self.service.compare_batches(
            "stat", "batch-base", "batch-cand", baseline_analysis, candidate_analysis,
            rules if rules is not None else self.rules,
        )

    def test_compare_reports_metric_statistics_and_rule_conclusions(self) -> None:
        baseline_analysis, candidate_analysis = self._baseline_and_candidate()
        outcome = self._compare(baseline_analysis, candidate_analysis)
        self.assertTrue(outcome["created"])
        result = outcome["result"]
        self.assertEqual(result["algorithm_version"], "robot-trials-comparison/1")
        self.assertEqual(result["conclusion"], "pass")
        self.assertEqual(result["insufficient"], [])

        completed = result["metrics"]["completed"]
        self.assertEqual(completed["kind"], "binary")
        self.assertTrue(completed["available"])
        self.assertGreater(Decimal(completed["difference"]), Decimal(0))
        self.assertLessEqual(Decimal(completed["ci_lower"]), Decimal(completed["difference"]))
        self.assertGreaterEqual(Decimal(completed["ci_upper"]), Decimal(completed["difference"]))
        stratum = completed["strata"]["cross-traffic"]
        self.assertEqual(stratum["baseline"], {"trials": 3, "successes": 2})
        self.assertEqual(stratum["candidate"], {"trials": 3, "successes": 3})
        self.assertLessEqual(stratum["newcombe_lower"], stratum["difference"])
        self.assertGreaterEqual(stratum["newcombe_upper"], stratum["difference"])

        seconds = result["metrics"]["completion_seconds"]
        self.assertEqual(seconds["kind"], "continuous")
        self.assertIsNotNone(seconds["effect_size"])
        # 完成用时方向为 lower，候选更快时取向差值为正
        self.assertGreater(Decimal(seconds["oriented_difference"]), Decimal(0))
        self.assertEqual(
            Decimal(seconds["oriented_difference"]), -Decimal(seconds["difference"])
        )

        interventions = result["metrics"]["interventions"]
        self.assertEqual(interventions["kind"], "count")
        self.assertIsNotNone(interventions["effect_size"])

        for rule in result["rules"]:
            self.assertTrue(rule["passed"])
            self.assertEqual(rule["conclusion"], "non_inferior")

    def test_compare_is_deterministic_and_idempotent(self) -> None:
        baseline_analysis, candidate_analysis = self._baseline_and_candidate()
        first = self._compare(baseline_analysis, candidate_analysis)
        second = self._compare(baseline_analysis, candidate_analysis)
        self.assertEqual(first["comparison_id"], second["comparison_id"])
        self.assertFalse(second["created"])
        self.assertEqual(first["result"], second["result"])
        count = self.connection.execute("SELECT count(*) FROM comparisons").fetchone()[0]
        self.assertEqual(count, 1)

    def test_compare_function_is_deterministic(self) -> None:
        protocol = load_protocol(ROOT / "fixtures" / "demo_protocol.json")
        spec = ComparisonSpec.from_dict(self.rules, protocol)
        baseline_rows = load_observations(ROOT / "fixtures" / "demo_observations.jsonl", protocol)
        candidate_rows = load_observations(ROOT / "fixtures" / "demo_observations.jsonl", protocol)
        first = compare(protocol, baseline_rows, candidate_rows, spec)
        second = compare(protocol, baseline_rows, candidate_rows, spec)
        self.assertEqual(first, second)

    def test_compare_rejects_same_batch(self) -> None:
        baseline_analysis, _ = self._baseline_and_candidate()
        with self.assertRaises(ValidationFailed):
            self.service.compare_batches(
                "stat", "batch-base", "batch-base", baseline_analysis, baseline_analysis, self.rules
            )

    def test_compare_rejects_unsealed_batch(self) -> None:
        baseline_analysis, _ = self._baseline_and_candidate()
        self.service.create_batch("operator", "batch-open", "demo-delivery-v1", 1, "build-a2")
        self.service.start_batch("operator", "batch-open", 1)
        with self.assertRaises(InvalidState):
            self.service.compare_batches(
                "stat", "batch-base", "batch-open", baseline_analysis, baseline_analysis, self.rules
            )

    def test_compare_rejects_incompatible_protocol(self) -> None:
        baseline_analysis, _ = self._baseline_and_candidate()
        protocol_v2 = deepcopy(self.protocol)
        protocol_v2["version"] = 2
        self.service.publish_protocol("stat", protocol_v2)
        self.service.create_batch("operator", "batch-v2", "demo-delivery-v1", 2, "build-a2")
        self.service.start_batch("operator", "batch-v2", 1)
        rows_v2 = [dict(row, protocol_version=2) for row in _improved_rows(self.rows)]
        self.service.import_observations("operator", "batch-v2", "key-v2", rows_v2)
        self.service.seal_batch("stat", "batch-v2", 2)
        job = self.service.claim_job("worker", 30)
        analysis_v2 = self.service.complete_job("worker", job["job_id"], "stat")
        with self.assertRaisesRegex(Conflict, "协议版本不兼容"):
            self.service.compare_batches(
                "stat", "batch-base", "batch-v2", baseline_analysis, analysis_v2["analysis_id"], self.rules
            )

    def test_compare_rejects_incompatible_robot_model(self) -> None:
        baseline_analysis, _ = self._baseline_and_candidate()
        rows_b = [dict(row, robot_id="robot-b") for row in self.rows]
        other_analysis = self._run_batch("batch-b", "build-b1", rows_b, "key-b")
        with self.assertRaisesRegex(Conflict, "机器人型号不兼容"):
            self.service.compare_batches(
                "stat", "batch-base", "batch-b", baseline_analysis, other_analysis, self.rules
            )

    def test_compare_rejects_non_current_analysis(self) -> None:
        baseline_analysis, candidate_analysis = self._baseline_and_candidate()
        row = self.connection.execute(
            "SELECT batch_revision,protocol_sha256,seed,created_by,created_at FROM analyses WHERE analysis_id=?",
            (baseline_analysis,),
        ).fetchone()
        self.connection.execute(
            "INSERT INTO analyses(batch_id,batch_revision,protocol_sha256,input_sha256,algorithm_version,"
            "seed,result_json,created_by,created_at) VALUES('batch-base',?,?,?,?,?,?,?,?)",
            (
                row["batch_revision"], row["protocol_sha256"], "f" * 64, "robot-trials-analysis/1",
                row["seed"], "{}", row["created_by"], row["created_at"],
            ),
        )
        with self.assertRaisesRegex(InvalidState, "不是批次当前版本"):
            self._compare(baseline_analysis, candidate_analysis)

    def test_compare_rejects_non_current_algorithm_version(self) -> None:
        baseline_analysis, candidate_analysis = self._baseline_and_candidate()
        self.connection.execute(
            "UPDATE analyses SET algorithm_version='robot-trials-analysis/0' WHERE analysis_id=?",
            (baseline_analysis,),
        )
        with self.assertRaisesRegex(InvalidState, "不是当前算法版本"):
            self._compare(baseline_analysis, candidate_analysis)

    def test_compare_rejects_changed_exclusions(self) -> None:
        baseline_analysis, candidate_analysis = self._baseline_and_candidate()
        observation_id = self.connection.execute(
            "SELECT observation_id FROM observations WHERE batch_id='batch-base' ORDER BY observation_id LIMIT 1"
        ).fetchone()[0]
        requested = self.service.request_exclusion("operator", observation_id, "事后发现记录损坏")
        self.service.review_exclusion("stat", requested["exclusion_id"], True, "确认")
        with self.assertRaisesRegex(InvalidState, "不再冻结"):
            self._compare(baseline_analysis, candidate_analysis)

    def test_compare_insufficient_when_stratum_missing(self) -> None:
        baseline_analysis = self._run_batch("batch-base", "build-a1", self.rows, "key-base")
        partial = [row for row in _improved_rows(self.rows) if row["stratum_key"] == "clear-aisle"]
        candidate_analysis = self._run_batch("batch-cand", "build-a2", partial, "key-cand")
        outcome = self._compare(baseline_analysis, candidate_analysis)
        result = outcome["result"]
        self.assertEqual(result["conclusion"], "insufficient")
        self.assertEqual(
            result["insufficient"],
            [{"stratum": "cross-traffic", "baseline": 3, "candidate": 0}],
        )
        for metric in result["metrics"].values():
            self.assertFalse(metric["available"])
        for rule in result["rules"]:
            self.assertFalse(rule["passed"])
            self.assertEqual(rule["conclusion"], "insufficient")

    def test_superiority_rule_distinguishes_identical_builds(self) -> None:
        baseline_analysis = self._run_batch("batch-base", "build-a1", self.rows, "key-base")
        candidate_analysis = self._run_batch("batch-cand", "build-a2", self.rows, "key-cand")
        rules = {
            "rules": [
                {"metric": "completed", "rule": "non_inferior", "margin": "0.5"},
                {"metric": "completion_seconds", "rule": "superior", "margin": "0"},
            ]
        }
        outcome = self._compare(baseline_analysis, candidate_analysis, rules)
        result = outcome["result"]
        self.assertEqual(result["conclusion"], "fail")
        by_metric = {rule["metric"]: rule for rule in result["rules"]}
        self.assertTrue(by_metric["completed"]["passed"])
        self.assertFalse(by_metric["completion_seconds"]["passed"])
        self.assertEqual(by_metric["completion_seconds"]["conclusion"], "inconclusive")
        seconds = result["metrics"]["completion_seconds"]
        self.assertLessEqual(Decimal(seconds["oriented_ci_lower"]), Decimal(0))

    def test_compare_does_not_change_batch_decisions(self) -> None:
        baseline_analysis, candidate_analysis = self._baseline_and_candidate()
        self.service.decide("approver", "batch-base", baseline_analysis, "approved", "基线准入")
        self.service.decide("approver", "batch-cand", candidate_analysis, "approved", "候选准入")
        outcome = self._compare(baseline_analysis, candidate_analysis)
        for batch_id in ("batch-base", "batch-cand"):
            report = self.service.report("auditor", batch_id)
            self.assertEqual(report["batch"]["state"], "decided")
            self.assertEqual(report["decision"]["decision"], "approved")
            self.assertEqual(len(report["comparisons"]), 1)
            self.assertEqual(report["comparisons"][0]["comparison_id"], outcome["comparison_id"])
        baseline_report = self.service.report("auditor", "batch-base")
        candidate_report = self.service.report("auditor", "batch-cand")
        self.assertEqual(baseline_report["comparisons"][0]["role"], "baseline")
        self.assertEqual(candidate_report["comparisons"][0]["role"], "candidate")

    def test_compare_validates_rule_spec(self) -> None:
        baseline_analysis, candidate_analysis = self._baseline_and_candidate()
        with self.assertRaises(ValidationFailed):
            self._compare(baseline_analysis, candidate_analysis, {"rules": []})
        with self.assertRaises(ValidationFailed):
            self._compare(
                baseline_analysis, candidate_analysis,
                {"rules": [{"metric": "unknown", "rule": "non_inferior", "margin": "0"}]},
            )
        with self.assertRaises(ValidationFailed):
            self._compare(
                baseline_analysis, candidate_analysis,
                {"rules": [{"metric": "completed", "rule": "non_inferior", "margin": "-0.1"}]},
            )
        with self.assertRaises(ValidationFailed):
            self._compare(
                baseline_analysis, candidate_analysis,
                {"rules": [
                    {"metric": "completed", "rule": "non_inferior"},
                    {"metric": "completed", "rule": "superior"},
                ]},
            )

    def test_compare_requires_statistician_role(self) -> None:
        baseline_analysis, candidate_analysis = self._baseline_and_candidate()
        with self.assertRaises(Forbidden):
            self.service.compare_batches(
                "operator", "batch-base", "batch-cand", baseline_analysis, candidate_analysis, self.rules
            )
        outcome = self._compare(baseline_analysis, candidate_analysis)
        with self.assertRaises(Forbidden):
            self.service.get_comparison("operator", outcome["comparison_id"])
        fetched = self.service.get_comparison("auditor", outcome["comparison_id"])
        self.assertEqual(fetched["comparison_id"], outcome["comparison_id"])


class ComparisonApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = TrialService(self.connection, clock)
        self.app = JsonApplication(self.service)
        for user_id, role in (("operator", "operator"), ("stat", "statistician"), ("auditor", "auditor")):
            self.service.create_user(user_id, user_id, role)
        protocol = load_json(ROOT / "fixtures" / "demo_protocol.json")
        rows = [
            json.loads(line)
            for line in (ROOT / "fixtures" / "demo_observations.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.service.register_robot("operator", "robot-a", "A 型", "厂商")
        self.service.register_build("operator", "build-a1", "robot-a", "1.0", "a" * 64)
        self.service.register_build("operator", "build-a2", "robot-a", "1.1", "b" * 64)
        self.service.publish_protocol("stat", protocol)
        self.analyses = {}
        for batch_id, build_id, batch_rows in (
            ("batch-base", "build-a1", rows),
            ("batch-cand", "build-a2", _improved_rows(rows)),
        ):
            self.service.create_batch("operator", batch_id, "demo-delivery-v1", 1, build_id)
            self.service.start_batch("operator", batch_id, 1)
            self.service.import_observations("operator", batch_id, f"key-{batch_id}", batch_rows)
            self.service.seal_batch("stat", batch_id, 2)
            job = self.service.claim_job("worker", 30)
            self.analyses[batch_id] = self.service.complete_job("worker", job["job_id"], "stat")["analysis_id"]

    def tearDown(self) -> None:
        self.connection.close()

    def _post_comparison(self) -> tuple[int, dict]:
        payload = json.dumps({
            "baseline_batch_id": "batch-base",
            "candidate_batch_id": "batch-cand",
            "baseline_analysis_id": self.analyses["batch-base"],
            "candidate_analysis_id": self.analyses["batch-cand"],
            "rules": [{"metric": "completed", "rule": "non_inferior", "margin": "0.05"}],
        }).encode()
        response = self.app.handle("POST", "/comparisons", {"x-actor-id": "stat"}, payload)
        return response.status, response.body

    def test_comparison_routes(self) -> None:
        status, body = self._post_comparison()
        self.assertEqual(status, 201)
        self.assertTrue(body["created"])
        replay_status, replay_body = self._post_comparison()
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body["comparison_id"], body["comparison_id"])
        fetched = self.app.handle("GET", f"/comparisons/{body['comparison_id']}", {"x-actor-id": "auditor"})
        self.assertEqual(fetched.status, 200)
        self.assertEqual(fetched.body["result"]["conclusion"], body["result"]["conclusion"])
        report = self.app.handle("GET", "/batches/batch-cand/report", {"x-actor-id": "auditor"})
        self.assertEqual(report.status, 200)
        self.assertEqual(len(report.body["comparisons"]), 1)

    def test_comparison_route_requires_actor(self) -> None:
        payload = json.dumps({
            "baseline_batch_id": "batch-base",
            "candidate_batch_id": "batch-cand",
            "baseline_analysis_id": self.analyses["batch-base"],
            "candidate_analysis_id": self.analyses["batch-cand"],
            "rules": [{"metric": "completed", "rule": "non_inferior"}],
        }).encode()
        response = self.app.handle("POST", "/comparisons", {}, payload)
        self.assertEqual(response.status, 422)


class ComparisonMathTests(unittest.TestCase):
    def test_bootstrap_difference_interval_is_deterministic(self) -> None:
        baseline = [[Decimal("1"), Decimal("2"), Decimal("3")]]
        candidate = [[Decimal("2"), Decimal("3"), Decimal("4")]]
        first = bootstrap_difference_interval(baseline, candidate, [Decimal(1)], seed=7, samples=200)
        second = bootstrap_difference_interval(baseline, candidate, [Decimal(1)], seed=7, samples=200)
        self.assertEqual(first, second)
        self.assertLessEqual(first[0], Decimal(1))
        self.assertGreaterEqual(first[1], Decimal(1))

    def test_bootstrap_difference_interval_rejects_empty_stratum(self) -> None:
        with self.assertRaises(ValueError):
            bootstrap_difference_interval([[]], [[Decimal(1)]], [Decimal(1)], seed=7, samples=100)


if __name__ == "__main__":
    unittest.main()
