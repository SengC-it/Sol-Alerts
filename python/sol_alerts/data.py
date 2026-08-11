from __future__ import annotations

import csv
import gzip
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class Candle:
    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float


@dataclass(frozen=True)
class Instrument:
    symbol: str
    quote_asset: str = "USDT"
    margin_asset: str = "USDT"
    contract_type: str = "PERPETUAL"
    status: str = "TRADING"
    delivery_time: int = 0


def read_candles(path: Path) -> list[Candle]:
    def from_row(row: dict[str, object]) -> Candle:
        return Candle(
            open_time=int(row["open_time"]), close_time=int(row["close_time"]), open=float(row["open"]),
            high=float(row["high"]), low=float(row["low"]), close=float(row["close"]),
            volume=float(row["volume"]), quote_volume=float(row["quote_volume"]),
        )

    def from_binance(row: list[object]) -> Candle:
        if len(row) < 9:
            raise ValueError(f"malformed Binance kline row in {path}")
        return Candle(int(row[0]), int(row[6]), float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5]), float(row[7]))

    if path.name.endswith(".gz") and not path.name.endswith(".csv.gz"):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return [from_binance(json.loads(line)) for line in handle if line.strip()]
    if path.name.endswith(".gz"):
        handle_context = gzip.open(path, "rt", newline="", encoding="utf-8")
    else:
        handle_context = path.open("rt", newline="", encoding="utf-8")
    with handle_context as handle:
        return [from_row(row) for row in csv.DictReader(handle)]


def read_funding_rates(path: Path | None, symbols: set[str] | None = None) -> dict[str, dict[int, float]]:
    """Read the downloader's per-symbol funding directory or a single CSV.

    Formal runs deliberately reject an absent file at the backtest boundary;
    this helper only parses supplied data.  A directory is the canonical
    downloader output, while accepting a single CSV keeps older manifests
    reproducible.
    """
    if path is None:
        return {}
    files = [path] if path.is_file() else sorted([*path.glob("*.csv"), *path.glob("*.csv.gz")]) if path.is_dir() else []
    if not files:
        raise ValueError(f"no funding CSV files found in {path}")
    result: dict[str, dict[int, float]] = {}
    for file in files:
        if file.name.endswith(".gz"):
            handle_context = gzip.open(file, "rt", newline="", encoding="utf-8")
        else:
            handle_context = file.open("rt", newline="", encoding="utf-8")
        with handle_context as handle:
            for row in csv.DictReader(handle):
                symbol = (row.get("symbol") or file.stem).upper()
                if symbols is not None and symbol not in symbols:
                    continue
                timestamp = int(row.get("funding_time", row.get("timestamp", "0")))
                rate = float(row["funding_rate"])
                if timestamp <= 0 or not (-1.0 <= rate <= 1.0):
                    raise ValueError(f"invalid funding event in {file}")
                result.setdefault(symbol, {})[timestamp] = rate
    return result


def validate_closed_series(candles: Iterable[Candle], interval_ms: int, now_ms: int) -> None:
    values = list(candles)
    if len(values) < 2:
        raise ValueError("insufficient candles")
    if now_ms - values[-1].close_time > interval_ms * 2:
        raise ValueError("stale candles")
    for current, following in zip(values, values[1:]):
        if following.open_time - current.open_time != interval_ms:
            raise ValueError("candle gap or duplicate")
        if current.close_time >= now_ms:
            raise ValueError("incomplete candle")


def ordered_split[T](items: list[T], train_fraction: float, validation_fraction: float) -> tuple[list[T], list[T], list[T]]:
    train_end = int(len(items) * train_fraction)
    validation_end = train_end + int(len(items) * validation_fraction)
    return items[:train_end], items[train_end:validation_end], items[validation_end:]


def eligibility(instrument: Instrument, history_days: float, quote_volume_24h: float, spread_bps: float, depth_notional: float, config: dict) -> dict[str, object]:
    universe = config["universe"]
    instrument_ok = (
        instrument.quote_asset == "USDT"
        and instrument.margin_asset == "USDT"
        and instrument.contract_type == "PERPETUAL"
        and instrument.status == "TRADING"
    )
    liquidity_ok = (
        quote_volume_24h >= universe["minQuoteVolume24h"]
        and spread_bps <= universe["maxSpreadBps"]
        and depth_notional >= universe["minDepthNotionalUsdt"]
    )
    formal = instrument_ok and liquidity_ok and history_days >= universe["minFormalHistoryDays"]
    return {
        "instrument_ok": instrument_ok,
        "liquidity_ok": liquidity_ok,
        "shadow_eligible": instrument_ok and history_days >= universe["shadowStartDays"],
        "formal_eligible": formal,
        "reason": "ok" if formal else "history_or_liquidity_or_contract_gate",
    }


def conservative_impact_proxy(order_notional_usdt: float, depth_notional_usdt: float, realized_volatility: float, config: dict) -> float:
    """A deliberately conservative non-order-book proxy for historical fills."""
    maximum = config["universe"]["referenceMaxOrderNotionalUsdt"]
    participation = min(1.0, order_notional_usdt / max(depth_notional_usdt, 1.0))
    size_factor = min(1.0, order_notional_usdt / maximum)
    return min(0.02, 0.0005 + abs(realized_volatility) * 0.5 + participation * 0.002 + size_factor * 0.001)
