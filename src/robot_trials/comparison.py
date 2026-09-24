"""两个已封存批次之间的确定性对照分析。

对照方向统一为“候选 - 基线”：二元指标使用 Newcombe 混合评分区间，
连续与计数指标使用固定种子的确定性 bootstrap 均值差区间，并给出
合并标准差效应量；分层结果按协议预注册权重汇总，再依据预先声明的
非劣或优效规则形成逐项结论。
"""

from __future__ import annotations

import random
from decimal import Decimal
from typing import Iterable, Sequence

from .contracts import ComparisonRule, Metric, Observation, Protocol
from .numeric import (
    newcombe_difference_interval,
    pooled_effect_size,
    quantile,
    summarize,
)


COMPARISON_ALGORITHM_VERSION = "robot-trials-comparison/1"

# 对照分析的 bootstrap 种子在协议种子上平移，避免与单批次分析的重采样序列混淆。
SEED_OFFSET = 500000


def bootstrap_mean_difference_interval(
    baseline: Sequence[Decimal], candidate: Sequence[Decimal], *, seed: int, samples: int
) -> tuple[Decimal, Decimal]:
    """对均值差（候选 - 基线）做确定性 bootstrap 区间估计。"""

    if not baseline or not candidate:
        raise ValueError("bootstrap 需要两侧都至少有一个样本")
    generator = random.Random(seed)
    differences: list[Decimal] = []
    for _ in range(samples):
        baseline_mean = sum(
            (baseline[generator.randrange(len(baseline))] for _ in baseline), Decimal(0)
        ) / Decimal(len(baseline))
        candidate_mean = sum(
            (candidate[generator.randrange(len(candidate))] for _ in candidate), Decimal(0)
        ) / Decimal(len(candidate))
        differences.append(candidate_mean - baseline_mean)
    return quantile(differences, Decimal("0.025")), quantile(differences, Decimal("0.975"))


def _binary_result(baseline_values: list[Decimal], candidate_values: list[Decimal]) -> dict[str, object]:
    baseline_successes = sum(int(value) for value in baseline_values)
    candidate_successes = sum(int(value) for value in candidate_values)
    interval = newcombe_difference_interval(
        candidate_successes, len(candidate_values), baseline_successes, len(baseline_values)
    )
    return {
        "kind": "binary",
        "baseline": {
            "successes": baseline_successes,
            "trials": len(baseline_values),
            "proportion": baseline_successes / len(baseline_values),
        },
        "candidate": {
            "successes": candidate_successes,
            "trials": len(candidate_values),
            "proportion": candidate_successes / len(candidate_values),
        },
        **interval.as_dict(),
    }


def _summary_result(
    metric: Metric,
    baseline_values: list[Decimal],
    candidate_values: list[Decimal],
    *,
    seed: int,
    samples: int,
) -> dict[str, object]:
    baseline_summary = summarize(baseline_values)
    candidate_summary = summarize(candidate_values)
    lower, upper = bootstrap_mean_difference_interval(
        baseline_values, candidate_values, seed=seed, samples=samples
    )
    effect = pooled_effect_size(candidate_summary, baseline_summary)
    return {
        "kind": metric.kind,
        "baseline": baseline_summary.as_dict(),
        "candidate": candidate_summary.as_dict(),
        "difference": format(candidate_summary.mean - baseline_summary.mean, "f"),
        "effect_size": None if effect is None else format(effect, "f"),
        "lower": format(lower, "f"),
        "upper": format(upper, "f"),
    }


def _evaluate_rule(metric: Metric, rule: ComparisonRule, lower: Decimal, upper: Decimal) -> str:
    if metric.direction == "higher":
        established = lower > -rule.margin if rule.rule_type == "non_inferior" else lower > rule.margin
    else:
        established = upper < rule.margin if rule.rule_type == "non_inferior" else upper < -rule.margin
    return rule.rule_type if established else "not_established"


def compare(
    protocol: Protocol,
    baseline: Iterable[Observation],
    candidate: Iterable[Observation],
    rules: Sequence[ComparisonRule],
) -> dict[str, object]:
    """对两个冻结输入执行对照分析，输出逐项结论与总体结论。"""

    baseline_all = tuple(baseline)
    candidate_all = tuple(candidate)
    baseline_included = tuple(item for item in baseline_all if item.excluded_reason is None)
    candidate_included = tuple(item for item in candidate_all if item.excluded_reason is None)

    strata: dict[str, dict[str, object]] = {}
    insufficient: list[dict[str, object]] = []
    for stratum_index, stratum in enumerate(protocol.strata):
        baseline_rows = [item for item in baseline_included if item.stratum_key == stratum.key]
        candidate_rows = [item for item in candidate_included if item.stratum_key == stratum.key]
        coverage = {
            "required": stratum.required_trials,
            "baseline": len(baseline_rows),
            "candidate": len(candidate_rows),
            "complete": len(baseline_rows) >= stratum.required_trials
            and len(candidate_rows) >= stratum.required_trials,
        }
        if not coverage["complete"]:
            insufficient.append({"stratum": stratum.key, **coverage})
        metrics: dict[str, object] = {}
        for metric_index, metric in enumerate(protocol.metrics):
            baseline_values = [item.metrics[metric.key] for item in baseline_rows]
            candidate_values = [item.metrics[metric.key] for item in candidate_rows]
            if not baseline_values or not candidate_values:
                continue
            if metric.kind == "binary":
                metrics[metric.key] = _binary_result(baseline_values, candidate_values)
            else:
                metrics[metric.key] = _summary_result(
                    metric,
                    baseline_values,
                    candidate_values,
                    seed=protocol.seed + SEED_OFFSET + stratum_index * 1009 + metric_index,
                    samples=protocol.bootstrap_samples,
                )
        strata[stratum.key] = {"coverage": coverage, "metrics": metrics}

    aggregate: dict[str, object] = {}
    for metric in protocol.metrics:
        available = [
            (stratum.key, strata[stratum.key]["metrics"].get(metric.key))
            for stratum in protocol.strata
        ]
        if any(value is None for _, value in available):
            aggregate[metric.key] = {"available": False, "reason": "至少一个预注册分层在对照任一侧无有效样本"}
            continue
        difference = sum(
            protocol.stratum_weights[key] * Decimal(str(value["difference"]))
            for key, value in available
        )
        lower = sum(
            protocol.stratum_weights[key] * Decimal(str(value["lower"]))
            for key, value in available
        )
        upper = sum(
            protocol.stratum_weights[key] * Decimal(str(value["upper"]))
            for key, value in available
        )
        entry: dict[str, object] = {
            "available": True,
            "difference": format(difference, "f"),
            "lower": format(lower, "f"),
            "upper": format(upper, "f"),
        }
        if metric.kind != "binary":
            effects = [value["effect_size"] for _, value in available]
            if all(effect is not None for effect in effects):
                weighted_effect = sum(
                    protocol.stratum_weights[key] * Decimal(str(effect))
                    for (key, _), effect in zip(available, effects)
                )
                entry["effect_size"] = format(weighted_effect, "f")
            else:
                entry["effect_size"] = None
        aggregate[metric.key] = entry

    rule_map = {rule.metric: rule for rule in rules}
    items: list[dict[str, object]] = []
    for metric in protocol.metrics:
        rule = rule_map[metric.key]
        entry = aggregate.get(metric.key, {})
        item: dict[str, object] = {
            "metric": metric.key,
            "direction": metric.direction,
            "rule": rule.as_dict(),
        }
        if isinstance(entry, dict) and entry.get("available"):
            item["difference"] = entry["difference"]
            item["lower"] = entry["lower"]
            item["upper"] = entry["upper"]
            if "effect_size" in entry:
                item["effect_size"] = entry["effect_size"]
        if insufficient or not (isinstance(entry, dict) and entry.get("available")):
            item["conclusion"] = "insufficient"
        else:
            item["conclusion"] = _evaluate_rule(
                metric,
                rule,
                Decimal(str(entry["lower"])),
                Decimal(str(entry["upper"])),
            )
        items.append(item)

    if insufficient:
        conclusion = "insufficient"
    elif all(item["conclusion"] == rule_map[item["metric"]].rule_type for item in items):
        conclusion = "established"
    else:
        conclusion = "not_established"
    return {
        "algorithm_version": COMPARISON_ALGORITHM_VERSION,
        "seed": protocol.seed,
        "bootstrap_samples": protocol.bootstrap_samples,
        "baseline": {
            "included_count": len(baseline_included),
            "excluded_count": len(baseline_all) - len(baseline_included),
        },
        "candidate": {
            "included_count": len(candidate_included),
            "excluded_count": len(candidate_all) - len(candidate_included),
        },
        "strata": strata,
        "aggregate": aggregate,
        "items": items,
        "insufficient": insufficient,
        "conclusion": conclusion,
    }
