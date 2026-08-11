from __future__ import annotations

import argparse
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Callable, Iterable
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .config import load_config
from .data import Candle, read_candles
from .download import INTERVAL_MS, _fetch_json, download_klines, fetch_exchange_info

BASELINE_VERSION = "risk-v1"
OBSERVATIONS = 90 * 24 * 4
RISK_CANDLE_COUNT = OBSERVATIONS + 1


def _supabase_request_headers(service_role_key: str) -> dict[str, str]:
    headers = {"apikey": service_role_key, "Content-Type": "application/json"}
    if not service_role_key.startswith("sb_secret_"):
        headers["Authorization"] = f"Bearer {service_role_key}"
    return headers


def fetch_24h_tickers(base_url: str = "https://fapi.binance.com") -> list[dict[str, object]]:
    payload = _fetch_json(f"{base_url.rstrip('/')}/fapi/v1/ticker/24hr")
    if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
        raise RuntimeError("Binance 24h ticker response is malformed")
    return [dict(row) for row in payload]


def fetch_book_tickers(base_url: str = "https://fapi.binance.com") -> list[dict[str, object]]:
    payload = _fetch_json(f"{base_url.rstrip('/')}/fapi/v1/ticker/bookTicker")
    if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
        raise RuntimeError("Binance bookTicker response is malformed")
    return [dict(row) for row in payload]


def _returns(candles: Iterable[Candle]) -> dict[int, float]:
    ordered = sorted(candles, key=lambda candle: candle.close_time)
    return {
        current.close_time: current.close / previous.close - 1
        for previous, current in zip(ordered, ordered[1:])
        if previous.close > 0 and math.isfinite(current.close / previous.close - 1)
    }


def baseline_window(end_ms: int) -> tuple[int, int]:
    """Return exactly 8641 canonical 15m bar opens and the final close."""
    last_open = end_ms - (INTERVAL_MS - 1)
    if end_ms <= 0 or last_open % INTERVAL_MS != 0 or last_open + INTERVAL_MS - 1 != end_ms:
        raise ValueError("risk baseline cutoff must be a canonical closed 15m boundary")
    return last_open - (RISK_CANDLE_COUNT - 1) * INTERVAL_MS, end_ms


def validate_baseline_candles(symbol: str, candles: list[Candle], first_open: int, end_ms: int) -> None:
    """Fail closed unless every retained row is an aligned, fully closed bar."""
    if len(candles) != RISK_CANDLE_COUNT:
        raise ValueError(f"{symbol} must contain exactly {RISK_CANDLE_COUNT} closed 15m candles")
    for index, candle in enumerate(candles):
        expected_open = first_open + index * INTERVAL_MS
        expected_close = expected_open + INTERVAL_MS - 1
        if (
            candle.open_time != expected_open
            or candle.close_time != expected_close
            or candle.close_time > end_ms
            or not all(math.isfinite(value) for value in (candle.open, candle.high, candle.low, candle.close, candle.volume, candle.quote_volume))
            or candle.close <= 0
        ):
            raise ValueError(f"{symbol} has an incomplete, misaligned, or invalid closed-15m baseline")


def build_risk_state(candles_by_symbol: dict[str, list[Candle]]) -> dict[str, object]:
    """Build the published 90-day risk seed from aligned closed official bars."""
    if "BTCUSDT" not in candles_by_symbol:
        raise ValueError("BTCUSDT is required for the risk baseline")
    returns_by_symbol: dict[str, dict[int, float]] = {}
    first_open: int | None = None
    end_ms: int | None = None
    for symbol, candles in sorted(candles_by_symbol.items()):
        if len(candles) < RISK_CANDLE_COUNT:
            raise ValueError(f"{symbol} has fewer than 90 days of closed 15m bars")
        window = candles[-RISK_CANDLE_COUNT:]
        symbol_first = window[0].open_time
        symbol_end = window[-1].close_time
        validate_baseline_candles(symbol, window, symbol_first, symbol_end)
        if first_open is None:
            first_open, end_ms = symbol_first, symbol_end
        returns_by_symbol[symbol] = _returns(window)
    assert first_open is not None and end_ms is not None
    timestamps = sorted(returns_by_symbol["BTCUSDT"])
    if len(timestamps) != OBSERVATIONS:
        raise ValueError("BTCUSDT baseline is incomplete")
    market_returns: list[float] = []
    for timestamp in timestamps:
        values = [series[timestamp] for series in returns_by_symbol.values() if timestamp in series and math.isfinite(series[timestamp])]
        if len(values) != len(returns_by_symbol):
            raise ValueError(f"market risk baseline is not aligned at timestamp {timestamp}")
        market_returns.append(float(median(values)))
    symbol_series: dict[str, list[float]] = {}
    for symbol, series in returns_by_symbol.items():
        values = [series.get(timestamp) for timestamp in timestamps]
        if len(values) != OBSERVATIONS or any(value is None or not math.isfinite(value) for value in values):
            raise ValueError(f"{symbol} is not aligned with the BTCUSDT 90-day baseline")
        symbol_series[symbol] = [float(value) for value in values if value is not None]
    return {
        "baselineVersion": BASELINE_VERSION,
        "btcReturns": [returns_by_symbol["BTCUSDT"][timestamp] for timestamp in timestamps],
        "eligibleMarketReturns": market_returns,
        "returnsBySymbol": symbol_series,
        "lastObservationAt": timestamps[-1],
        "pauseUntil": None,
    }


def _number(value: object, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"risk universe field {field} is malformed") from error
    if not math.isfinite(result):
        raise ValueError(f"risk universe field {field} is not finite")
    return result


def _unique_ticker_batch(rows: list[dict[str, object]], batch_name: str) -> dict[str, dict[str, object]]:
    indexed: dict[str, dict[str, object]] = {}
    for row in rows:
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            raise ValueError(f"Binance {batch_name} ticker symbol is malformed")
        normalized = symbol.upper()
        if normalized in indexed:
            raise ValueError(f"duplicate Binance {batch_name} ticker for {normalized}")
        indexed[normalized] = row
    return indexed


def select_risk_universe(exchange_info: dict[str, object], volume_tickers: list[dict[str, object]], book_tickers: list[dict[str, object]], cutoff_end_ms: int, config: dict[str, Any] | None = None) -> list[str]:
    """Select every current static-eligible USDT perpetual listed for 90 days.

    This intentionally stops at the live static gates (volume and spread). Depth
    is directional and is not a universe-wide gate for the risk baseline.
    ``deliveryDate`` is lifecycle metadata, never an eligibility condition.
    """
    config = config or load_config()
    first_open, _ = baseline_window(cutoff_end_ms)
    rows = exchange_info.get("symbols")
    if not isinstance(rows, list):
        raise ValueError("Binance exchangeInfo symbols are missing")
    volume_map = _unique_ticker_batch(volume_tickers, "24h")
    book_map = _unique_ticker_batch(book_tickers, "bookTicker")
    selected: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("symbol"), str):
            raise ValueError("Binance exchangeInfo symbol row is malformed")
        symbol = str(row["symbol"]).upper()
        instrument_ok = (
            row.get("status") == "TRADING"
            and row.get("quoteAsset") == "USDT"
            and row.get("marginAsset") == "USDT"
            and row.get("contractType") == "PERPETUAL"
        )
        if not instrument_ok:
            continue
        onboard = row.get("onboardDate")
        try:
            onboard_ms = int(onboard) if onboard is not None else 0
        except (TypeError, ValueError) as error:
            raise ValueError(f"{symbol} onboardDate is malformed") from error
        if onboard_ms <= 0:
            raise ValueError(f"{symbol} lacks official onboardDate for 90-day eligibility")
        if onboard_ms > first_open:
            continue
        volume_ticker = volume_map.get(symbol)
        if volume_ticker is None:
            raise ValueError(f"{symbol} is missing from the all-market 24h ticker batch")
        book_ticker = book_map.get(symbol)
        if book_ticker is None:
            raise ValueError(f"{symbol} is missing from the all-market bookTicker batch")
        quote_volume = _number(volume_ticker.get("quoteVolume"), "quoteVolume")
        bid = _number(book_ticker.get("bidPrice"), "bidPrice")
        ask = _number(book_ticker.get("askPrice"), "askPrice")
        if bid <= 0 or ask <= 0 or ask < bid:
            raise ValueError(f"{symbol} has invalid bid/ask data")
        spread_bps = (ask - bid) / ((ask + bid) / 2) * 10_000
        if quote_volume >= float(config["universe"]["minQuoteVolume24h"]) and spread_bps <= float(config["universe"]["maxSpreadBps"]):
            selected.append(symbol)
    selected = sorted(set(selected))
    if "BTCUSDT" not in selected:
        raise ValueError("BTCUSDT is not currently static-eligible; refusing to publish risk baseline")
    if not selected:
        raise ValueError("formal risk universe is empty")
    return selected


def build_official_risk_state(cache_dir: Path, cutoff_end_ms: int, base_url: str = "https://fapi.binance.com", config: dict[str, Any] | None = None) -> tuple[dict[str, object], list[str]]:
    """Download and validate one reproducible official risk baseline."""
    config = config or load_config()
    first_open, end_ms = baseline_window(cutoff_end_ms)
    symbols = select_risk_universe(fetch_exchange_info(base_url), fetch_24h_tickers(base_url), fetch_book_tickers(base_url), end_ms, config)
    kline_dir = cache_dir / "klines"
    kline_dir.mkdir(parents=True, exist_ok=True)
    candles_by_symbol: dict[str, list[Candle]] = {}
    for symbol in symbols:
        path = kline_dir / f"{symbol}.jsonl.gz"
        download_klines(symbol, first_open, end_ms, path, interval="15m", base_url=base_url)
        candles = read_candles(path)
        validate_baseline_candles(symbol, candles, first_open, end_ms)
        candles_by_symbol[symbol] = candles
    return build_risk_state(candles_by_symbol), symbols


def publish_risk_state(state: dict[str, object], open_url: Callable[..., Any] | None = None) -> None:
    """Atomically publish only the validated global_risk runtime status."""
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise RuntimeError("risk baseline publish requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY")
    request = Request(
        f"{url.rstrip('/')}/rest/v1/rpc/sol_set_runtime_status",
        data=json.dumps({"p_key": "global_risk", "p_value": state}, separators=(",", ":")).encode("utf-8"),
        headers=_supabase_request_headers(key),
        method="POST",
    )
    opener = open_url or urlopen
    try:
        with opener(request, timeout=20) as response:
            status = int(getattr(response, "status", 200))
            if status < 200 or status >= 300:
                raise RuntimeError(f"HTTP {status}")
    except HTTPError as error:
        raise RuntimeError(f"risk baseline publish failed: HTTP {error.code}") from error
    except RuntimeError:
        raise
    except Exception as error:
        raise RuntimeError(f"risk baseline publish failed: {type(error).__name__}") from error


def _closed_cutoff_now_ms() -> int:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return (now_ms // INTERVAL_MS) * INTERVAL_MS - 1


def _parse_cutoff(value: str | None) -> int:
    if not value:
        return _closed_cutoff_now_ms()
    try:
        raw_ms = int(value)
    except ValueError:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        raw_ms = int(parsed.timestamp() * 1000)
    return (raw_ms // INTERVAL_MS) * INTERVAL_MS - 1


def _read_input_directory(directory: Path) -> dict[str, list[Candle]]:
    files = sorted(path for path in directory.iterdir() if path.is_file() and path.name.endswith((".csv", ".csv.gz", ".jsonl.gz", ".json.gz")))
    if not files:
        raise ValueError("risk bootstrap input directory is empty")
    candles: dict[str, list[Candle]] = {}
    for path in files:
        name = path.name
        for suffix in (".jsonl.gz", ".json.gz", ".csv.gz", ".csv"):
            if name.endswith(suffix):
                name = name[: -len(suffix)]
                break
        candles[name.upper()] = read_candles(path)
    return candles


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the closed-15m 90-day global-risk bootstrap state")
    parser.add_argument("--input", type=Path, help="offline directory with symbol candles; never used by the official workflow")
    parser.add_argument("--cache-dir", type=Path, help="cache directory for official Binance klines")
    parser.add_argument("--as-of", help="UTC cutoff (ISO-8601 or milliseconds); rounded down to the latest closed 15m bar")
    parser.add_argument("--base-url", default="https://fapi.binance.com")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--publish", action="store_true", help="publish global_risk through the protected Supabase RPC")
    args = parser.parse_args()
    if args.input is not None and args.cache_dir is not None:
        parser.error("--input and --cache-dir are mutually exclusive")
    if args.input is None and args.cache_dir is None:
        parser.error("official mode requires --cache-dir")
    if args.input is not None:
        state = build_risk_state(_read_input_directory(args.input))
        symbols = sorted(state["returnsBySymbol"])
        cutoff = state["lastObservationAt"]
    else:
        cutoff = _parse_cutoff(args.as_of)
        state, symbols = build_official_risk_state(args.cache_dir, cutoff, args.base_url)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    if args.publish:
        publish_risk_state(state)
    print(json.dumps({"ok": True, "baselineVersion": BASELINE_VERSION, "observations": OBSERVATIONS, "symbols": symbols, "cutoff": cutoff, "published": args.publish, "output": str(args.output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
