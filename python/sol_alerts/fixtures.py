from __future__ import annotations

import math

from .data import Candle


def synthetic_candles(symbol: str, count: int = 1_440, start_ms: int = 1_700_000_000_000) -> list[Candle]:
    """Create a deterministic fixture with alternating trend/range regimes."""
    seed = sum(ord(character) for character in symbol)
    price = 100.0 + seed % 37
    candles: list[Candle] = []
    for index in range(count):
        regime = (index // 180) % 4
        drift = (0.0012 if regime in (0, 1) else -0.0010 if regime == 2 else 0.0) * (1 if (seed + index // 180) % 2 == 0 else -1)
        wave = math.sin((index + seed) / 7.0) * 0.002 + math.sin((index + seed) / 31.0) * 0.004
        open_price = price
        close_price = max(1.0, open_price * (1 + drift + wave))
        high = max(open_price, close_price) * (1 + 0.002 + abs(math.sin(index / 11)) * 0.001)
        low = min(open_price, close_price) * (1 - 0.002 - abs(math.cos(index / 13)) * 0.001)
        volume = 10_000 + (index * 137 + seed * 17) % 4_000 + (8_000 if index % 41 == 0 else 0)
        candles.append(Candle(start_ms + index * 900_000, start_ms + (index + 1) * 900_000 - 1, open_price, high, low, close_price, volume, volume * close_price))
        price = close_price
    return candles


def research_fixture_candles(symbol: str, start_ms: int = 1_700_000_000_000) -> list[Candle]:
    """Deterministic closed-bar fixtures that exercise each real family.

    The bars deliberately contain observed 1h structures, volume expansion,
    pullbacks, and band excursions. Signals still come from ``generate_signals``;
    this helper never emits a signal or trade directly.
    """
    output: list[Candle] = []
    price = 100.0
    if symbol == "BTCUSDT":
        count = 1_600
        for index in range(count):
            phase = index % 400
            previous = price
            if phase == 0:
                price = 100.0
            if phase < 280:
                price = 100 + ((index % 16) - 8) * 0.02
            else:
                price = 100 - 0.06 * min(phase - 280, 14) + 0.04 * max(0, phase - 294)
            high, low = 100.1, 99.9
            if phase == 220:
                high, low = 110.0, 90.0
            if phase == 294:
                low = 98.5
            if phase == 295:
                high = 101.5
            if phase == 300:
                price = 110.0
                high = 110.0
            volume = 60_000 if phase in (220, 294, 300) else 10_000
            output.append(Candle(start_ms + index * 900_000, start_ms + (index + 1) * 900_000 - 1, previous, high, low, price, volume, volume * price))
        return output
    if symbol == "ETHUSDT":
        count = 2_200
        for index in range(count):
            phase = index % 700
            previous = price
            if phase == 0:
                price = 100.0
            if phase < 200:
                price = 100.0
            elif phase < 440:
                price *= 1.00045
            elif phase == 440:
                price *= 1.02
            elif phase < 460:
                price *= 1.0001
            elif phase < 465:
                price *= 0.999
            elif phase == 465:
                price *= 1.03
            else:
                price *= 1.0001
            high, low = max(previous, price) * 1.001, min(previous, price) * 0.999
            if phase == 440:
                high = 160.0
            volume = 60_000 if phase in (440, 465) else 10_000
            output.append(Candle(start_ms + index * 900_000, start_ms + (index + 1) * 900_000 - 1, previous, high, low, price, volume, volume * price))
        return output
    count = 2_200
    for index in range(count):
        phase = index % 1_000
        cycle = index // 1_000
        previous = price
        if phase == 0:
            price = 100.0
        if phase < 200:
            price = 100.0
        elif phase < 500:
            price *= 1.00045
        elif phase == 500:
            price *= 1.02
        elif phase < 520:
            price *= 0.999
        elif phase == 520:
            price *= 1.03
        elif phase < 700:
            price *= 1.0001
        elif phase < 900:
            price = 105 + ((phase % 20) - 10) * 0.03 if cycle == 0 else price * 1.00045
        elif phase == 900:
            price = 95.0 if cycle == 0 else price * 0.999
        elif phase == 901:
            price = 100.0 if cycle == 0 else price * 1.01
        elif phase < 1_200:
            price = 100 + ((phase % 16) - 8) * 0.1
        else:
            price *= 1.0003
        high, low = max(previous, price) * 1.001, min(previous, price) * 0.999
        if phase == 440:
            high = 160.0
        if phase == 840 and cycle >= 1:
            high = 160.0
        if phase == 500:
            high = 160.0
        volume = 60_000 if phase in (440, 500, 520, 900) else 10_000
        if phase == 900 and cycle == 0:
            low = 80.0
        output.append(Candle(start_ms + index * 900_000, start_ms + (index + 1) * 900_000 - 1, previous, high, low, price, volume, volume * price))
    return output
