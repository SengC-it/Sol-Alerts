from __future__ import annotations

from math import sqrt

from .data import Candle


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    return (ordered[middle - 1] + ordered[middle]) / 2 if len(ordered) % 2 == 0 else ordered[middle]


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * min(1.0, max(0.0, p))
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def ema(values: list[float], period: int) -> float:
    if len(values) < period:
        return 0.0
    current = mean(values[:period])
    multiplier = 2 / (period + 1)
    for value in values[period:]:
        current = (value - current) * multiplier + current
    return current


def atr(candles: list[Candle], period: int = 14) -> float:
    true_ranges: list[float] = []
    for index, candle in enumerate(candles):
        previous_close = candles[index - 1].close if index else candle.open
        true_ranges.append(max(candle.high - candle.low, abs(candle.high - previous_close), abs(candle.low - previous_close)))
    return mean(true_ranges[-period:])


def adx(candles: list[Candle], period: int = 14) -> float:
    if len(candles) < period + 2:
        return 0.0
    plus: list[float] = []
    minus: list[float] = []
    ranges: list[float] = []
    for index in range(1, len(candles)):
        current, previous = candles[index], candles[index - 1]
        up = current.high - previous.high
        down = previous.low - current.low
        plus.append(up if up > down and up > 0 else 0.0)
        minus.append(down if down > up and down > 0 else 0.0)
        ranges.append(max(current.high - current.low, abs(current.high - previous.close), abs(current.low - previous.close)))
    denominator = mean(ranges[-period:])
    if denominator == 0:
        return 0.0
    plus_di = mean(plus[-period:]) / denominator * 100
    minus_di = mean(minus[-period:]) / denominator * 100
    return abs(plus_di - minus_di) / (plus_di + minus_di) * 100 if plus_di + minus_di else 0.0


def rsi(values: list[float], period: int = 14) -> float:
    if len(values) <= period:
        return 50.0
    gains = sum(max(values[index] - values[index - 1], 0.0) for index in range(len(values) - period, len(values)))
    losses = sum(max(values[index - 1] - values[index], 0.0) for index in range(len(values) - period, len(values)))
    if losses == 0:
        return 100.0 if gains else 50.0
    relative_strength = gains / losses
    return 100 - 100 / (1 + relative_strength)


def bollinger(values: list[float], period: int, deviations: float) -> tuple[float, float, float]:
    sample = values[-period:]
    middle = mean(sample)
    spread = sqrt(mean([(value - middle) ** 2 for value in sample])) * deviations
    return middle, middle + spread, middle - spread


def returns(values: list[float]) -> list[float]:
    return [current / previous - 1 for previous, current in zip(values, values[1:]) if previous]


def state(candles: list[Candle], trend_threshold: float = 25, range_threshold: float = 15) -> str:
    closes = [candle.close for candle in candles]
    current_adx = adx(candles, 14)
    if atr(candles, 14) <= 0:
        return "ambiguous"
    fast, slow = ema(closes, 20), ema(closes, 50)
    if current_adx >= trend_threshold and fast > slow:
        return "trend_up"
    if current_adx >= trend_threshold and fast < slow:
        return "trend_down"
    if current_adx <= range_threshold:
        return "range"
    return "ambiguous"
