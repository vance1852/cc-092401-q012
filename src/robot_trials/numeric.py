"""不依赖第三方库的基础描述性统计。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Sequence


@dataclass(frozen=True, slots=True)
class NumericSummary:
    count: int
    minimum: Decimal
    maximum: Decimal
    mean: Decimal
    median: Decimal
    sample_variance: Decimal | None

    def as_dict(self) -> dict[str, str | int | None]:
        return {
            "count": self.count,
            "minimum": format(self.minimum, "f"),
            "maximum": format(self.maximum, "f"),
            "mean": format(self.mean, "f"),
            "median": format(self.median, "f"),
            "sample_variance": (
                None if self.sample_variance is None else format(self.sample_variance, "f")
            ),
        }


@dataclass(frozen=True, slots=True)
class WilsonInterval:
    successes: int
    trials: int
    lower: float
    upper: float

    def as_dict(self) -> dict[str, int | float]:
        return {
            "successes": self.successes,
            "trials": self.trials,
            "lower": round(self.lower, 12),
            "upper": round(self.upper, 12),
        }


@dataclass(frozen=True, slots=True)
class DifferenceInterval:
    """两个独立样本比例差的区间估计。"""

    difference: float
    lower: float
    upper: float

    def as_dict(self) -> dict[str, float]:
        return {
            "difference": round(self.difference, 12),
            "lower": round(self.lower, 12),
            "upper": round(self.upper, 12),
        }


def quantile(values: Iterable[Decimal], probability: Decimal) -> Decimal:
    """按线性插值计算分位数，输入不能为空。"""

    ordered = sorted(values)
    if not ordered:
        raise ValueError("分位数输入不能为空")
    position = probability * Decimal(len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - Decimal(lower)
    return ordered[lower] * (Decimal(1) - fraction) + ordered[upper] * fraction


def summarize(values: Iterable[Decimal | int | str]) -> NumericSummary:
    """计算有限数值的稳定摘要，方差使用 n-1 分母。"""

    data = sorted(Decimal(str(value)) for value in values)
    if not data:
        raise ValueError("至少需要一个数值")
    if any(not item.is_finite() for item in data):
        raise ValueError("数值必须有限")
    count = len(data)
    total = sum(data, Decimal(0))
    mean = total / count
    midpoint = count // 2
    median = (
        data[midpoint]
        if count % 2
        else (data[midpoint - 1] + data[midpoint]) / Decimal(2)
    )
    variance = None
    if count > 1:
        squared = sum(((item - mean) ** 2 for item in data), Decimal(0))
        variance = squared / Decimal(count - 1)
    return NumericSummary(
        count=count,
        minimum=data[0],
        maximum=data[-1],
        mean=mean,
        median=median,
        sample_variance=variance,
    )


def wilson_interval(successes: int, trials: int, z: float = 1.959963984540054) -> WilsonInterval:
    """计算二项比例的双侧 Wilson 区间。"""

    if isinstance(successes, bool) or isinstance(trials, bool):
        raise ValueError("计数必须是整数")
    if not isinstance(successes, int) or not isinstance(trials, int):
        raise ValueError("计数必须是整数")
    if trials <= 0 or successes < 0 or successes > trials:
        raise ValueError("成功数与试验数不合法")
    if not math.isfinite(z) or z <= 0:
        raise ValueError("z 必须是有限正数")
    proportion = successes / trials
    z2 = z * z
    denominator = 1.0 + z2 / trials
    center = (proportion + z2 / (2.0 * trials)) / denominator
    radius = (
        z
        * math.sqrt((proportion * (1.0 - proportion) + z2 / (4.0 * trials)) / trials)
        / denominator
    )
    return WilsonInterval(
        successes=successes,
        trials=trials,
        lower=max(0.0, center - radius),
        upper=min(1.0, center + radius),
    )


def newcombe_difference_interval(
    successes_a: int,
    trials_a: int,
    successes_b: int,
    trials_b: int,
    z: float = 1.959963984540054,
) -> DifferenceInterval:
    """Newcombe 混合评分区间，估计比例差 p_a - p_b（两组独立）。"""

    first = wilson_interval(successes_a, trials_a, z)
    second = wilson_interval(successes_b, trials_b, z)
    proportion_a = successes_a / trials_a
    proportion_b = successes_b / trials_b
    difference = proportion_a - proportion_b
    lower = difference - math.sqrt(
        (proportion_a - first.lower) ** 2 + (second.upper - proportion_b) ** 2
    )
    upper = difference + math.sqrt(
        (first.upper - proportion_a) ** 2 + (proportion_b - second.lower) ** 2
    )
    return DifferenceInterval(
        difference=difference,
        lower=max(-1.0, lower),
        upper=min(1.0, upper),
    )


def pooled_effect_size(a: NumericSummary, b: NumericSummary) -> Decimal | None:
    """Cohen's d = (mean_a - mean_b) / 合并标准差；方差缺失或合并标准差为零时返回 None。"""

    if a.sample_variance is None or b.sample_variance is None:
        return None
    degrees = (a.count - 1) + (b.count - 1)
    pooled = (
        (a.count - 1) * a.sample_variance + (b.count - 1) * b.sample_variance
    ) / Decimal(degrees)
    if pooled <= 0:
        return None
    return (a.mean - b.mean) / pooled.sqrt()


def group_metric(
    rows: Sequence[object],
    metric_name: str,
    *,
    include_excluded: bool = False,
) -> dict[str, NumericSummary]:
    """按观测分层汇总一个指标，不推断业务准入结论。"""

    grouped: dict[str, list[Decimal]] = {}
    for row in rows:
        excluded_reason = getattr(row, "excluded_reason")
        if excluded_reason is not None and not include_excluded:
            continue
        metrics = getattr(row, "metrics")
        if metric_name not in metrics:
            raise KeyError(metric_name)
        grouped.setdefault(getattr(row, "stratum_key"), []).append(metrics[metric_name])
    return {key: summarize(values) for key, values in sorted(grouped.items())}

