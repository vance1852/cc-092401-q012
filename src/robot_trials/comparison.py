"""两个已封存批次之间的确定性构建对照分析。"""

from __future__ import annotations

import math
import random
from decimal import Decimal
from typing import Sequence

from .contracts import ComparisonSpec, Observation, Protocol
from .numeric import quantile, summarize, wilson_interval


COMPARISON_VERSION = "robot-trials-comparison/1"


def _resample_mean(values: Sequence[Decimal], generator: random.Random) -> Decimal:
    total = sum((values[generator.randrange(len(values))] for _ in values), Decimal(0))
    return total / Decimal(len(values))


def bootstrap_difference_interval(
    baseline_strata: Sequence[Sequence[Decimal]],
    candidate_strata: Sequence[Sequence[Decimal]],
    weights: Sequence[Decimal],
    *,
    seed: int,
    samples: int,
) -> tuple[Decimal, Decimal]:
    """对分层加权均值差做确定性 bootstrap，区间只依赖输入与种子。"""

    if not baseline_strata or not (
        len(baseline_strata) == len(candidate_strata) == len(weights)
    ):
        raise ValueError("分层输入与权重必须一一对应且非空")
    if any(not values for values in (*baseline_strata, *candidate_strata)):
        raise ValueError("bootstrap 的每个分层都至少需要一个样本")
    generator = random.Random(seed)
    differences: list[Decimal] = []
    for _ in range(samples):
        total = Decimal(0)
        for weight, baseline_values, candidate_values in zip(
            weights, baseline_strata, candidate_strata
        ):
            total += weight * (
                _resample_mean(candidate_values, generator)
                - _resample_mean(baseline_values, generator)
            )
        differences.append(total)
    return quantile(differences, Decimal("0.025")), quantile(differences, Decimal("0.975"))


def _newcombe_difference_interval(
    candidate_successes: int, candidate_trials: int, baseline_successes: int, baseline_trials: int
) -> tuple[float, float, float]:
    """两个独立比例的 Newcombe 混合评分区间。"""

    candidate = wilson_interval(candidate_successes, candidate_trials)
    baseline = wilson_interval(baseline_successes, baseline_trials)
    candidate_proportion = candidate_successes / candidate_trials
    baseline_proportion = baseline_successes / baseline_trials
    difference = candidate_proportion - baseline_proportion
    lower = difference - math.sqrt(
        (candidate_proportion - candidate.lower) ** 2 + (baseline.upper - baseline_proportion) ** 2
    )
    upper = difference + math.sqrt(
        (candidate.upper - candidate_proportion) ** 2 + (baseline_proportion - baseline.lower) ** 2
    )
    return difference, max(-1.0, lower), min(1.0, upper)


def _hedges_g(baseline_values: Sequence[Decimal], candidate_values: Sequence[Decimal]) -> Decimal | None:
    """候选相对基线的标准化均值差（Hedges g，正向表示候选更高）。"""

    baseline_summary = summarize(baseline_values)
    candidate_summary = summarize(candidate_values)
    difference = candidate_summary.mean - baseline_summary.mean
    degrees = len(baseline_values) + len(candidate_values) - 2
    if degrees <= 0:
        return None
    baseline_variance = baseline_summary.sample_variance or Decimal(0)
    candidate_variance = candidate_summary.sample_variance or Decimal(0)
    pooled = (
        (len(baseline_values) - 1) * baseline_variance
        + (len(candidate_values) - 1) * candidate_variance
    ) / Decimal(degrees)
    if pooled == 0:
        return Decimal(0) if difference == 0 else None
    correction = Decimal(1) - Decimal(3) / Decimal(
        4 * (len(baseline_values) + len(candidate_values)) - 9
    )
    return correction * difference / pooled.sqrt()


def _orient(metric_direction: str, difference: Decimal, lower: Decimal, upper: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """把候选减基线的差值换算成“正数代表候选改善”的方向。"""

    if metric_direction == "higher":
        return difference, lower, upper
    return -difference, -upper, -lower


def compare(
    protocol: Protocol,
    baseline_observations: Sequence[Observation],
    candidate_observations: Sequence[Observation],
    spec: ComparisonSpec,
) -> dict[str, object]:
    """按预注册分层与预声明规则对照两个冻结输入，输出确定性的逐项结论。"""

    baseline_included = tuple(item for item in baseline_observations if item.excluded_reason is None)
    candidate_included = tuple(item for item in candidate_observations if item.excluded_reason is None)

    coverage: dict[str, dict[str, int]] = {}
    insufficient: list[dict[str, object]] = []
    for stratum in protocol.strata:
        counts = {
            "baseline": sum(1 for item in baseline_included if item.stratum_key == stratum.key),
            "candidate": sum(1 for item in candidate_included if item.stratum_key == stratum.key),
        }
        coverage[stratum.key] = counts
        if counts["baseline"] == 0 or counts["candidate"] == 0:
            insufficient.append({"stratum": stratum.key, **counts})

    weights = [protocol.stratum_weights[stratum.key] for stratum in protocol.strata]
    metrics: dict[str, object] = {}
    for metric_index, metric in enumerate(protocol.metrics):
        per_stratum: dict[str, object] = {}
        baseline_strata: list[list[Decimal]] = []
        candidate_strata: list[list[Decimal]] = []
        for stratum in protocol.strata:
            baseline_values = [
                item.metrics[metric.key] for item in baseline_included if item.stratum_key == stratum.key
            ]
            candidate_values = [
                item.metrics[metric.key] for item in candidate_included if item.stratum_key == stratum.key
            ]
            if not baseline_values or not candidate_values:
                continue
            baseline_strata.append(baseline_values)
            candidate_strata.append(candidate_values)
            if metric.kind == "binary":
                baseline_successes = sum(int(value) for value in baseline_values)
                candidate_successes = sum(int(value) for value in candidate_values)
                difference, lower, upper = _newcombe_difference_interval(
                    candidate_successes, len(candidate_values), baseline_successes, len(baseline_values)
                )
                per_stratum[stratum.key] = {
                    "baseline": {"trials": len(baseline_values), "successes": baseline_successes},
                    "candidate": {"trials": len(candidate_values), "successes": candidate_successes},
                    "difference": round(difference, 12),
                    "newcombe_lower": round(lower, 12),
                    "newcombe_upper": round(upper, 12),
                }
            else:
                baseline_summary = summarize(baseline_values)
                candidate_summary = summarize(candidate_values)
                per_stratum[stratum.key] = {
                    "baseline": {"count": len(baseline_values), "mean": format(baseline_summary.mean, "f")},
                    "candidate": {"count": len(candidate_values), "mean": format(candidate_summary.mean, "f")},
                    "difference": format(candidate_summary.mean - baseline_summary.mean, "f"),
                }

        entry: dict[str, object] = {
            "kind": metric.kind,
            "direction": metric.direction,
            "strata": per_stratum,
        }
        if insufficient:
            entry["available"] = False
            entry["reason"] = "至少一个预注册分层在基线或候选中无有效样本"
            metrics[metric.key] = entry
            continue

        baseline_weighted = sum(
            weight * summarize(values).mean
            for weight, values in zip(weights, baseline_strata)
        )
        candidate_weighted = sum(
            weight * summarize(values).mean
            for weight, values in zip(weights, candidate_strata)
        )
        difference = candidate_weighted - baseline_weighted
        lower, upper = bootstrap_difference_interval(
            baseline_strata,
            candidate_strata,
            weights,
            seed=protocol.seed + 1000003 * (metric_index + 1),
            samples=protocol.bootstrap_samples,
        )
        oriented_difference, oriented_lower, oriented_upper = _orient(
            metric.direction, difference, lower, upper
        )
        entry.update({
            "available": True,
            "baseline_weighted_mean": format(baseline_weighted, "f"),
            "candidate_weighted_mean": format(candidate_weighted, "f"),
            "difference": format(difference, "f"),
            "ci_lower": format(lower, "f"),
            "ci_upper": format(upper, "f"),
            "oriented_difference": format(oriented_difference, "f"),
            "oriented_ci_lower": format(oriented_lower, "f"),
            "oriented_ci_upper": format(oriented_upper, "f"),
        })
        if metric.kind != "binary":
            pooled_baseline = [value for values in baseline_strata for value in values]
            pooled_candidate = [value for values in candidate_strata for value in values]
            effect = _hedges_g(pooled_baseline, pooled_candidate)
            entry["effect_size"] = None if effect is None else format(effect, "f")
            if effect is not None:
                oriented_effect = effect if metric.direction == "higher" else -effect
                entry["oriented_effect_size"] = format(oriented_effect, "f")
        metrics[metric.key] = entry

    rule_results: list[dict[str, object]] = []
    for rule in spec.rules:
        metric = protocol.metric_map[rule.metric]
        metric_result = metrics[rule.metric]
        outcome: dict[str, object] = {
            "metric": rule.metric,
            "rule": rule.rule,
            "margin": format(rule.margin, "f"),
        }
        if not metric_result["available"]:
            outcome.update({"passed": False, "conclusion": "insufficient", "oriented_ci_lower": None})
        else:
            oriented_lower = Decimal(str(metric_result["oriented_ci_lower"]))
            if rule.rule == "non_inferior":
                passed = oriented_lower > -rule.margin
                conclusion = "non_inferior" if passed else "inconclusive"
            else:
                passed = oriented_lower > rule.margin
                conclusion = "superior" if passed else "inconclusive"
            outcome.update({
                "passed": passed,
                "conclusion": conclusion,
                "oriented_ci_lower": metric_result["oriented_ci_lower"],
            })
        rule_results.append(outcome)

    if insufficient:
        conclusion = "insufficient"
    else:
        conclusion = "pass" if all(item["passed"] for item in rule_results) else "fail"
    return {
        "algorithm_version": COMPARISON_VERSION,
        "seed": protocol.seed,
        "bootstrap_samples": protocol.bootstrap_samples,
        "baseline_included": len(baseline_included),
        "candidate_included": len(candidate_included),
        "baseline_excluded": len(baseline_observations) - len(baseline_included),
        "candidate_excluded": len(candidate_observations) - len(candidate_included),
        "strata_coverage": coverage,
        "metrics": metrics,
        "rules": rule_results,
        "insufficient": insufficient,
        "conclusion": conclusion,
    }
