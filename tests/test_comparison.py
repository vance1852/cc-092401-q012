from __future__ import annotations

import json
import sqlite3
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from robot_trials.clock import FrozenClock
from robot_trials.comparison import COMPARISON_ALGORITHM_VERSION, compare
from robot_trials.contracts import Observation, parse_comparison_rules
from robot_trials.errors import Forbidden, InvalidState, ValidationFailed
from robot_trials.jsonio import load_json, load_protocol


ROOT = Path(__file__).resolve().parents[1]

RULES = [
    {"metric": "completed", "type": "non_inferior", "margin": "0.6"},
    {"metric": "completion_seconds", "type": "non_inferior", "margin": "10"},
    {"metric": "interventions", "type": "non_inferior", "margin": "1.5"},
]


def make_observation(
    stratum: str,
    row: str,
    completed: int,
    seconds: str,
    interventions: int,
    excluded: str | None = None,
) -> Observation:
    return Observation(
        source_batch="unit",
        source_row=row,
        robot_id="robot-x",
        protocol_id="demo-delivery-v1",
        protocol_version=1,
        stratum_key=stratum,
        observed_at="2026-09-22T09:00:00+08:00",
        metrics={
            "completed": Decimal(completed),
            "completion_seconds": Decimal(seconds),
            "interventions": Decimal(interventions),
        },
        excluded_reason=excluded,
    )


def sample_rows(seconds: tuple[str, ...], completed: tuple[int, ...], interventions: tuple[int, ...]):
    rows = []
    for index, stratum in enumerate(("clear-aisle", "cross-traffic")):
        for offset in range(3):
            position = index * 3 + offset
            rows.append(
                make_observation(
                    stratum,
                    f"{position + 1:03d}",
                    completed[position],
                    seconds[position],
                    interventions[position],
                )
            )
    return tuple(rows)


class CompareUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.protocol = load_protocol(ROOT / "fixtures" / "demo_protocol.json")

    def test_superiority_established_and_deterministic(self) -> None:
        baseline = sample_rows(
            ("70", "72", "68", "90", "95", "85"),
            (1, 1, 1, 1, 0, 1),
            (2, 2, 2, 3, 3, 3),
        )
        candidate = sample_rows(
            ("40", "42", "38", "50", "55", "45"),
            (1, 1, 1, 1, 1, 1),
            (0, 0, 0, 1, 1, 1),
        )
        rules = parse_comparison_rules(
            [
                {"metric": "completed", "type": "non_inferior", "margin": "0.6"},
                {"metric": "completion_seconds", "type": "superior", "margin": "0"},
                {"metric": "interventions", "type": "superior", "margin": "0"},
            ],
            self.protocol,
        )
        first = compare(self.protocol, baseline, candidate, rules)
        second = compare(self.protocol, baseline, candidate, rules)
        self.assertEqual(first, second)
        self.assertEqual(first["algorithm_version"], COMPARISON_ALGORITHM_VERSION)
        self.assertEqual(first["conclusion"], "established")
        conclusions = {item["metric"]: item["conclusion"] for item in first["items"]}
        self.assertEqual(
            conclusions,
            {"completed": "non_inferior", "completion_seconds": "superior", "interventions": "superior"},
        )
        seconds = first["aggregate"]["completion_seconds"]
        self.assertLess(Decimal(seconds["upper"]), 0)
        self.assertLess(Decimal(seconds["effect_size"]), 0)
        completed = first["strata"]["cross-traffic"]["metrics"]["completed"]
        self.assertEqual(completed["candidate"]["successes"], 3)
        self.assertEqual(completed["baseline"]["successes"], 2)
        self.assertGreater(completed["difference"], 0)

    def test_superiority_not_established_when_interval_straddles_zero(self) -> None:
        rows = sample_rows(
            ("50", "52", "48", "70", "75", "65"),
            (1, 1, 1, 1, 0, 1),
            (1, 1, 1, 2, 2, 2),
        )
        rules = parse_comparison_rules(
            [
                {"metric": "completed", "type": "superior", "margin": "0"},
                {"metric": "completion_seconds", "type": "superior", "margin": "0"},
                {"metric": "interventions", "type": "superior", "margin": "0"},
            ],
            self.protocol,
        )
        result = compare(self.protocol, rows, rows, rules)
        self.assertEqual(result["conclusion"], "not_established")
        self.assertTrue(all(item["conclusion"] == "not_established" for item in result["items"]))

    def test_missing_stratum_is_insufficient(self) -> None:
        baseline = sample_rows(
            ("50", "52", "48", "70", "75", "65"),
            (1, 1, 1, 1, 0, 1),
            (1, 1, 1, 2, 2, 2),
        )
        candidate = tuple(row for row in baseline if row.stratum_key == "clear-aisle")
        rules = parse_comparison_rules(RULES, self.protocol)
        result = compare(self.protocol, baseline, candidate, rules)
        self.assertEqual(result["conclusion"], "insufficient")
        self.assertEqual(result["insufficient"][0]["stratum"], "cross-traffic")
        self.assertEqual(result["insufficient"][0]["candidate"], 0)
        self.assertTrue(all(item["conclusion"] == "insufficient" for item in result["items"]))
        self.assertFalse(result["aggregate"]["completed"]["available"])

    def test_excluded_observations_are_not_compared(self) -> None:
        baseline = sample_rows(
            ("50", "52", "48", "70", "75", "65"),
            (1, 1, 1, 1, 0, 1),
            (1, 1, 1, 2, 2, 2),
        ) + (make_observation("clear-aisle", "099", 0, "99", 5, excluded="记录损坏"),)
        candidate = sample_rows(
            ("50", "52", "48", "70", "75", "65"),
            (1, 1, 1, 1, 0, 1),
            (1, 1, 1, 2, 2, 2),
        )
        rules = parse_comparison_rules(RULES, self.protocol)
        result = compare(self.protocol, baseline, candidate, rules)
        self.assertEqual(result["baseline"]["included_count"], 6)
        self.assertEqual(result["baseline"]["excluded_count"], 1)
        self.assertEqual(result["strata"]["clear-aisle"]["coverage"]["baseline"], 3)


class ComparisonServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        from robot_trials.service import TrialService

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
        self.service.register_robot("operator", "robot-b", "A 型", "厂商")
        self.service.register_robot("operator", "robot-c", "B 型", "厂商")
        self.service.register_build("operator", "build-a", "robot-a", "1.0", "a" * 64)
        self.service.register_build("operator", "build-b", "robot-b", "1.1", "b" * 64)
        self.service.register_build("operator", "build-c", "robot-c", "1.0", "c" * 64)
        self.service.publish_protocol("stat", self.protocol)

    def tearDown(self) -> None:
        self.connection.close()

    def _rows_for(self, robot_id: str, protocol_version: int = 1, strata: set[str] | None = None):
        rows = []
        for row in self.rows:
            item = dict(row)
            item["robot_id"] = robot_id
            item["protocol_version"] = protocol_version
            rows.append(item)
        if strata is not None:
            rows = [row for row in rows if row["stratum_key"] in strata]
        return rows

    def _run_batch(
        self,
        batch_id: str,
        build_id: str,
        rows,
        decision: str = "approved",
        protocol_version: int = 1,
    ):
        self.service.create_batch("operator", batch_id, "demo-delivery-v1", protocol_version, build_id)
        self.service.start_batch("operator", batch_id, 1)
        self.service.import_observations("operator", batch_id, f"key-{batch_id}", rows)
        self.service.seal_batch("stat", batch_id, 2)
        job = self.service.claim_job("worker", 30)
        analysis = self.service.complete_job("worker", job["job_id"], "stat")
        self.service.decide("approver", batch_id, analysis["analysis_id"], decision, "测试决定")
        return analysis

    def _two_batches(self):
        baseline = self._run_batch("batch-a", "build-a", self._rows_for("robot-a"))
        candidate = self._run_batch("batch-b", "build-b", self._rows_for("robot-b"))
        return baseline, candidate

    def test_create_comparison_is_idempotent_and_versioned(self) -> None:
        self._two_batches()
        created = self.service.create_comparison("stat", "batch-a", "batch-b", RULES)
        self.assertFalse(created["replayed"])
        self.assertEqual(created["algorithm_version"], COMPARISON_ALGORITHM_VERSION)
        self.assertEqual(created["result"]["conclusion"], "established")
        self.assertEqual(len(created["result"]["items"]), 3)
        self.assertTrue(
            all(item["conclusion"] == "non_inferior" for item in created["result"]["items"])
        )
        self.assertEqual(Decimal(created["result"]["aggregate"]["completed"]["difference"]), 0)
        replayed = self.service.create_comparison("stat", "batch-a", "batch-b", RULES)
        self.assertTrue(replayed["replayed"])
        self.assertEqual(replayed["comparison_id"], created["comparison_id"])
        self.assertEqual(replayed["result"], created["result"])
        count = self.connection.execute("SELECT count(*) FROM comparisons").fetchone()[0]
        self.assertEqual(count, 1)
        events = self.connection.execute(
            "SELECT event_type FROM audit_events WHERE entity_type='comparison'"
        ).fetchall()
        self.assertEqual([row[0] for row in events], ["comparison.created"])
        loaded = self.service.get_comparison("auditor", created["comparison_id"])
        self.assertEqual(loaded["result"], created["result"])
        self.assertEqual(loaded["spec"]["rules"][0]["metric"], "completed")

    def test_comparison_leaves_batch_decisions_untouched(self) -> None:
        self._two_batches()
        before_decisions = self.connection.execute(
            "SELECT batch_id,analysis_id,decision FROM decisions ORDER BY decision_id"
        ).fetchall()
        before_states = self.connection.execute(
            "SELECT batch_id,state,revision FROM batches ORDER BY batch_id"
        ).fetchall()
        self.service.create_comparison("stat", "batch-a", "batch-b", RULES)
        after_decisions = self.connection.execute(
            "SELECT batch_id,analysis_id,decision FROM decisions ORDER BY decision_id"
        ).fetchall()
        after_states = self.connection.execute(
            "SELECT batch_id,state,revision FROM batches ORDER BY batch_id"
        ).fetchall()
        self.assertEqual([tuple(row) for row in before_decisions], [tuple(row) for row in after_decisions])
        self.assertEqual([tuple(row) for row in before_states], [tuple(row) for row in after_states])
        self.assertTrue(all(row["state"] == "decided" for row in after_states))

    def test_comparison_enters_both_batch_reports(self) -> None:
        self._two_batches()
        created = self.service.create_comparison("stat", "batch-a", "batch-b", RULES)
        baseline_report = self.service.report("auditor", "batch-a")
        candidate_report = self.service.report("auditor", "batch-b")
        self.assertEqual(len(baseline_report["comparisons"]), 1)
        entry = baseline_report["comparisons"][0]
        self.assertEqual(entry["comparison_id"], created["comparison_id"])
        self.assertEqual(entry["role"], "baseline")
        self.assertEqual(entry["result"]["conclusion"], "established")
        self.assertEqual(candidate_report["comparisons"][0]["role"], "candidate")
        self.assertEqual(
            candidate_report["comparisons"][0]["comparison_id"], created["comparison_id"]
        )

    def test_protocol_incompatible_batches_are_rejected(self) -> None:
        upgraded = deepcopy(self.protocol)
        upgraded["version"] = 2
        upgraded["admission_rules"][0]["threshold"] = "0.30"
        self.service.publish_protocol("stat", upgraded)
        self._run_batch("batch-a", "build-a", self._rows_for("robot-a"))
        self._run_batch(
            "batch-c", "build-a", self._rows_for("robot-a", protocol_version=2), protocol_version=2
        )
        with self.assertRaisesRegex(ValidationFailed, "协议版本不兼容"):
            self.service.create_comparison("stat", "batch-a", "batch-c", RULES)

    def test_robot_model_incompatible_batches_are_rejected(self) -> None:
        self._run_batch("batch-a", "build-a", self._rows_for("robot-a"))
        self._run_batch("batch-d", "build-c", self._rows_for("robot-c"))
        with self.assertRaisesRegex(ValidationFailed, "机器人型号不兼容"):
            self.service.create_comparison("stat", "batch-a", "batch-d", RULES)

    def test_missing_stratum_yields_explicit_insufficient(self) -> None:
        self._run_batch("batch-a", "build-a", self._rows_for("robot-a"))
        self._run_batch(
            "batch-e",
            "build-b",
            self._rows_for("robot-b", strata={"clear-aisle"}),
            decision="needs_more_data",
        )
        created = self.service.create_comparison("stat", "batch-a", "batch-e", RULES)
        self.assertEqual(created["result"]["conclusion"], "insufficient")
        self.assertEqual(created["result"]["insufficient"][0]["stratum"], "cross-traffic")
        self.assertTrue(
            all(item["conclusion"] == "insufficient" for item in created["result"]["items"])
        )

    def test_exclusion_change_after_analysis_is_rejected(self) -> None:
        self._two_batches()
        observation_id = self.connection.execute(
            "SELECT observation_id FROM observations WHERE batch_id='batch-a' LIMIT 1"
        ).fetchone()[0]
        requested = self.service.request_exclusion("operator", observation_id, "事后发现记录失效")
        self.service.review_exclusion("stat", requested["exclusion_id"], True, "证据充分")
        with self.assertRaisesRegex(InvalidState, "不再冻结"):
            self.service.create_comparison("stat", "batch-a", "batch-b", RULES)

    def test_non_current_analysis_reference_is_rejected(self) -> None:
        baseline, candidate = self._two_batches()
        with self.assertRaisesRegex(InvalidState, "非当前分析版本"):
            self.service.create_comparison(
                "stat", "batch-a", "batch-b", RULES,
                baseline_analysis_id=candidate["analysis_id"],
            )
        with self.assertRaisesRegex(InvalidState, "非当前分析版本"):
            self.service.create_comparison(
                "stat", "batch-a", "batch-b", RULES, candidate_analysis_id=999
            )
        current = self.service.create_comparison(
            "stat", "batch-a", "batch-b", RULES,
            baseline_analysis_id=baseline["analysis_id"],
            candidate_analysis_id=candidate["analysis_id"],
        )
        self.assertFalse(current["replayed"])

    def test_unsealed_or_same_batch_is_rejected(self) -> None:
        self._run_batch("batch-a", "build-a", self._rows_for("robot-a"))
        self.service.create_batch("operator", "batch-f", "demo-delivery-v1", 1, "build-b")
        self.service.start_batch("operator", "batch-f", 1)
        with self.assertRaisesRegex(InvalidState, "已封存"):
            self.service.create_comparison("stat", "batch-a", "batch-f", RULES)
        with self.assertRaisesRegex(ValidationFailed, "不同的批次"):
            self.service.create_comparison("stat", "batch-a", "batch-a", RULES)

    def test_comparison_permissions(self) -> None:
        self._two_batches()
        with self.assertRaises(Forbidden):
            self.service.create_comparison("operator", "batch-a", "batch-b", RULES)
        with self.assertRaises(Forbidden):
            self.service.create_comparison("approver", "batch-a", "batch-b", RULES)
        created = self.service.create_comparison("stat", "batch-a", "batch-b", RULES)
        with self.assertRaises(Forbidden):
            self.service.get_comparison("operator", created["comparison_id"])
        self.assertEqual(
            self.service.get_comparison("approver", created["comparison_id"])["comparison_id"],
            created["comparison_id"],
        )

    def test_invalid_rules_are_rejected(self) -> None:
        self._two_batches()
        with self.assertRaises(ValidationFailed):
            self.service.create_comparison("stat", "batch-a", "batch-b", RULES[:2])
        broken = [dict(rule) for rule in RULES]
        broken[0]["margin"] = "-0.1"
        with self.assertRaises(ValidationFailed):
            self.service.create_comparison("stat", "batch-a", "batch-b", broken)


if __name__ == "__main__":
    unittest.main()
