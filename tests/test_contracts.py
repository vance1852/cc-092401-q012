from __future__ import annotations

import unittest
from copy import deepcopy
from pathlib import Path

from robot_trials.contracts import Observation, Protocol, ValidationError, parse_comparison_rules
from robot_trials.jsonio import load_json


ROOT = Path(__file__).resolve().parents[1]


class ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw_protocol = load_json(ROOT / "fixtures" / "demo_protocol.json")
        self.protocol = Protocol.from_dict(self.raw_protocol)

    def test_protocol_builds_indexes(self) -> None:
        self.assertEqual(self.protocol.version, 1)
        self.assertEqual(self.protocol.stratum_keys, {"clear-aisle", "cross-traffic"})
        self.assertEqual(set(self.protocol.metric_map), {"completed", "completion_seconds", "interventions"})

    def test_protocol_rejects_duplicate_metric(self) -> None:
        raw = deepcopy(self.raw_protocol)
        raw["metrics"].append(deepcopy(raw["metrics"][0]))
        with self.assertRaisesRegex(ValidationError, "不能重复"):
            Protocol.from_dict(raw)

    def test_observation_rejects_unknown_stratum(self) -> None:
        raw = {
            "source_batch": "batch",
            "source_row": "1",
            "robot_id": "r1",
            "protocol_id": self.protocol.protocol_id,
            "protocol_version": self.protocol.version,
            "stratum_key": "unknown",
            "observed_at": "2026-09-21T10:00:00+08:00",
            "metrics": {"completed": 1, "completion_seconds": 4, "interventions": 0},
            "excluded_reason": None,
        }
        with self.assertRaisesRegex(ValidationError, "未在协议中声明"):
            Observation.from_dict(raw, self.protocol)

    def test_binary_metric_is_strict(self) -> None:
        raw = {
            "source_batch": "batch",
            "source_row": "1",
            "robot_id": "r1",
            "protocol_id": self.protocol.protocol_id,
            "protocol_version": self.protocol.version,
            "stratum_key": "clear-aisle",
            "observed_at": "2026-09-21T10:00:00+08:00",
            "metrics": {"completed": 2, "completion_seconds": 4, "interventions": 0},
            "excluded_reason": None,
        }
        with self.assertRaisesRegex(ValidationError, "必须是 0 或 1"):
            Observation.from_dict(raw, self.protocol)

    def _rules(self) -> list[dict[str, str]]:
        return [
            {"metric": "completed", "type": "non_inferior", "margin": "0.1"},
            {"metric": "completion_seconds", "type": "superior", "margin": "0"},
            {"metric": "interventions", "type": "non_inferior", "margin": "1"},
        ]

    def test_comparison_rules_follow_protocol_metric_order(self) -> None:
        rules = parse_comparison_rules(list(reversed(self._rules())), self.protocol)
        self.assertEqual(
            [rule.metric for rule in rules],
            ["completed", "completion_seconds", "interventions"],
        )
        self.assertEqual(rules[0].as_dict(), {"metric": "completed", "type": "non_inferior", "margin": "0.1"})

    def test_comparison_rules_must_cover_every_metric(self) -> None:
        with self.assertRaisesRegex(ValidationError, "覆盖全部协议指标"):
            parse_comparison_rules(self._rules()[:1], self.protocol)

    def test_comparison_rules_reject_unknown_metric_and_bad_margin(self) -> None:
        rules = self._rules()
        rules[0] = {"metric": "unknown", "type": "superior", "margin": "0"}
        with self.assertRaisesRegex(ValidationError, "未在协议中声明"):
            parse_comparison_rules(rules, self.protocol)
        rules = self._rules()
        rules[0] = {"metric": "completed", "type": "non_inferior", "margin": "0"}
        with self.assertRaisesRegex(ValidationError, "必须大于零"):
            parse_comparison_rules(rules, self.protocol)
        rules = self._rules()
        rules[1] = {"metric": "completion_seconds", "type": "superior", "margin": "-1"}
        with self.assertRaisesRegex(ValidationError, "不能为负"):
            parse_comparison_rules(rules, self.protocol)

    def test_comparison_rules_reject_duplicates(self) -> None:
        rules = self._rules()
        rules.append({"metric": "completed", "type": "superior", "margin": "0"})
        with self.assertRaisesRegex(ValidationError, "重复"):
            parse_comparison_rules(rules, self.protocol)


if __name__ == "__main__":
    unittest.main()

