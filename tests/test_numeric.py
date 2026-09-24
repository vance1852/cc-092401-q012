from __future__ import annotations

import unittest
from decimal import Decimal
from pathlib import Path

from robot_trials.jsonio import load_observations, load_protocol
from robot_trials.numeric import (
    group_metric,
    newcombe_difference_interval,
    pooled_effect_size,
    quantile,
    summarize,
    wilson_interval,
)


ROOT = Path(__file__).resolve().parents[1]


class NumericTests(unittest.TestCase):
    def test_summary_uses_sample_variance(self) -> None:
        summary = summarize([1, 2, 3, 4])
        self.assertEqual(summary.mean, Decimal("2.5"))
        self.assertEqual(summary.median, Decimal("2.5"))
        self.assertEqual(summary.sample_variance, Decimal(5) / Decimal(3))

    def test_single_value_has_no_variance(self) -> None:
        summary = summarize(["4.20"])
        self.assertEqual(summary.minimum, Decimal("4.20"))
        self.assertIsNone(summary.sample_variance)

    def test_wilson_interval_contains_observed_proportion(self) -> None:
        interval = wilson_interval(5, 6)
        self.assertLess(interval.lower, 5 / 6)
        self.assertGreater(interval.upper, 5 / 6)
        self.assertGreaterEqual(interval.lower, 0)
        self.assertLessEqual(interval.upper, 1)

    def test_group_metric_uses_declared_strata(self) -> None:
        protocol = load_protocol(ROOT / "fixtures" / "demo_protocol.json")
        rows = load_observations(ROOT / "fixtures" / "demo_observations.jsonl", protocol)
        grouped = group_metric(rows, "completion_seconds")
        self.assertEqual(grouped["clear-aisle"].count, 3)
        self.assertEqual(grouped["cross-traffic"].count, 3)

    def test_quantile_interpolates(self) -> None:
        values = [Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4")]
        self.assertEqual(quantile(values, Decimal("0.5")), Decimal("2.5"))
        self.assertEqual(quantile(values, Decimal("0")), Decimal("1"))
        self.assertEqual(quantile(values, Decimal("1")), Decimal("4"))

    def test_newcombe_interval_tracks_difference(self) -> None:
        interval = newcombe_difference_interval(6, 6, 3, 6)
        self.assertAlmostEqual(interval.difference, 0.5)
        self.assertGreater(interval.lower, 0)
        self.assertLess(interval.upper, 1)
        same = newcombe_difference_interval(3, 6, 3, 6)
        self.assertLess(same.lower, 0)
        self.assertGreater(same.upper, 0)

    def test_pooled_effect_size(self) -> None:
        first = summarize([1, 2, 3])
        second = summarize([3, 4, 5])
        self.assertEqual(pooled_effect_size(first, second), Decimal(-2))
        self.assertIsNone(pooled_effect_size(summarize([1]), second))
        self.assertIsNone(pooled_effect_size(summarize([2, 2]), summarize([3, 3])))


if __name__ == "__main__":
    unittest.main()

