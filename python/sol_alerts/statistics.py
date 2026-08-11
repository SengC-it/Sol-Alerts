from __future__ import annotations

import math


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def max_drawdown_from_r(values: list[float]) -> float:
    equity = 1.0
    peak = equity
    result = 0.0
    for value in values:
        equity *= max(0.0, 1.0 + value * 0.01)
        peak = max(peak, equity)
        result = max(result, (peak - equity) / peak)
    return result


def deterministic_random(seed: int):
    state = seed & 0xFFFFFFFF
    while True:
        state = (1664525 * state + 1013904223) & 0xFFFFFFFF
        yield state / 2**32


def block_bootstrap_lower_bound(values: list[float], seed: int, replicates: int = 2000, block_size: int | None = None) -> float:
    if not values:
        return 0.0
    size = block_size or max(2, int(math.sqrt(len(values))))
    random = deterministic_random(seed)
    means: list[float] = []
    for _ in range(replicates):
        sample: list[float] = []
        while len(sample) < len(values):
            start = int(next(random) * len(values))
            sample.extend(values[start : min(len(values), start + size)])
        means.append(mean(sample[: len(values)]))
    means.sort()
    return means[max(0, int(len(means) * 0.025) - 1)]


def paired_block_lower_bound(differences: list[float], seed: int, replicates: int = 2000) -> float:
    return block_bootstrap_lower_bound(differences, seed, replicates)


def concentration_share(contributions: list[float]) -> float:
    positive = [value for value in contributions if value > 0]
    total = sum(positive)
    return max(positive, default=0.0) / total if total else 0.0
