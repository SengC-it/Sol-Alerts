from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import median
from typing import Iterable

from .data import Candle, read_candles, validate_closed_series

BASELINE_VERSION = "risk-v1"
OBSERVATIONS = 90 * 24 * 4
INTERVAL_MS = 15 * 60_000


def _returns(candles: Iterable[Candle]) -> dict[int, float]:
    ordered = sorted(candles, key=lambda candle: candle.close_time)
    return {current.close_time: current.close / previous.close - 1 for previous, current in zip(ordered, ordered[1:]) if previous.close > 0}


def build_risk_state(candles_by_symbol: dict[str, list[Candle]]) -> dict[str, object]:
    """Build the published 90-day risk seed from closed official 15m bars."""
    if "BTCUSDT" not in candles_by_symbol:
        raise ValueError("BTCUSDT is required for the risk baseline")
    returns_by_symbol: dict[str, dict[int, float]] = {}
    for symbol, candles in sorted(candles_by_symbol.items()):
        if len(candles) < OBSERVATIONS + 1:
            raise ValueError(f"{symbol} has fewer than 90 days of closed 15m bars")
        validate_closed_series(candles[-(OBSERVATIONS + 1):], INTERVAL_MS, candles[-1].close_time + 1)
        returns_by_symbol[symbol] = _returns(candles[-(OBSERVATIONS + 1):])
    timestamps = sorted(returns_by_symbol["BTCUSDT"])[-OBSERVATIONS:]
    if len(timestamps) < OBSERVATIONS:
        raise ValueError("BTCUSDT baseline is incomplete")
    market_returns: list[float] = []
    for timestamp in timestamps:
        values = [series[timestamp] for series in returns_by_symbol.values() if timestamp in series and isinstance(series[timestamp], float)]
        if not values:
            raise ValueError(f"market risk baseline has a missing timestamp: {timestamp}")
        market_returns.append(float(median(values)))
    symbol_series: dict[str, list[float]] = {}
    for symbol, series in returns_by_symbol.items():
        values = [series[timestamp] for timestamp in timestamps if timestamp in series]
        if len(values) < OBSERVATIONS:
            raise ValueError(f"{symbol} is not aligned with the BTCUSDT 90-day baseline")
        symbol_series[symbol] = values[-OBSERVATIONS:]
    return {
        "baselineVersion": BASELINE_VERSION,
        "btcReturns": [returns_by_symbol["BTCUSDT"][timestamp] for timestamp in timestamps],
        "eligibleMarketReturns": market_returns,
        "returnsBySymbol": symbol_series,
        "lastObservationAt": timestamps[-1],
        "pauseUntil": None,
        "observationCount": OBSERVATIONS,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the closed-15m 90-day global-risk bootstrap state")
    parser.add_argument("--input", type=Path, required=True, help="directory with symbol.csv, symbol.csv.gz, or Binance JSONL .gz files")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    files = sorted(path for path in args.input.iterdir() if path.is_file() and path.name.endswith((".csv", ".csv.gz", ".jsonl.gz", ".json.gz")))
    if not files:
        raise SystemExit("risk bootstrap input directory is empty")
    candles = {}
    for path in files:
        name = path.name
        for suffix in (".jsonl.gz", ".json.gz", ".csv.gz", ".csv"):
            if name.endswith(suffix):
                name = name[: -len(suffix)]
                break
        candles[name.upper()] = read_candles(path)
    state = build_risk_state(candles)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print(json.dumps({"ok": True, "baselineVersion": BASELINE_VERSION, "observations": OBSERVATIONS, "output": str(args.output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
