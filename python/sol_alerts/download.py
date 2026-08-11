from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

INTERVAL_MS = 15 * 60_000
FUNDING_INTERVAL_MS = 8 * 60 * 60_000


def _fetch_json(url: str, timeout: int = 30) -> object:
    request = Request(url, headers={"User-Agent": "sol-alerts-research/1.0"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _atomic_replace(path: Path, writer: Callable[[Path], None]) -> None:
    """Replace a cache file only after the complete replacement is valid."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False) as handle:
            temporary_name = handle.name
        temporary = Path(temporary_name)
        writer(temporary)
        os.replace(temporary, path)
    finally:
        if temporary_name is not None:
            temporary_path = Path(temporary_name)
            if temporary_path.exists():
                temporary_path.unlink()


def _validate_kline_row(row: object, symbol: str) -> list[object]:
    if not isinstance(row, list) or len(row) < 9:
        raise ValueError(f"malformed Binance kline row for {symbol}")
    values = [int(row[0]), int(row[6]), float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5]), float(row[7])]
    if values[0] < 0 or values[1] < values[0] or not all(math.isfinite(float(value)) for value in values[2:]) or min(values[2:6]) <= 0 or values[6] < 0 or values[7] < 0:
        raise ValueError(f"invalid Binance kline row for {symbol}")
    return row


def _read_kline_rows(path: Path, symbol: str) -> list[list[object]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    return [_validate_kline_row(row, symbol) for row in rows]


def _interval_ms(interval: str) -> int:
    if interval == "15m":
        return INTERVAL_MS
    if interval == "1h":
        return 60 * 60_000
    raise ValueError(f"unsupported interval: {interval}")


def canonical_closed_bounds(start_ms: int, end_ms: int, interval_ms: int) -> tuple[int, int]:
    """Return the first open and last open whose complete bars fit the window."""
    if interval_ms <= 0:
        raise ValueError("interval must be positive")
    first_open = ((start_ms + interval_ms - 1) // interval_ms) * interval_ms
    last_open = ((end_ms - (interval_ms - 1)) // interval_ms) * interval_ms
    return first_open, last_open


def _is_canonical_kline_row(row: list[object], symbol: str, interval_ms: int, first_open: int, last_open: int, end_ms: int) -> bool:
    _validate_kline_row(row, symbol)
    open_time = int(row[0])
    close_time = int(row[6])
    return (
        first_open <= open_time <= last_open
        and open_time % interval_ms == 0
        and close_time - open_time == interval_ms - 1
        and close_time <= end_ms
    )


def _write_kline_rows(path: Path, rows: list[list[object]], compressed: bool) -> None:
    opener = gzip.open if compressed else open
    with opener(path, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")


def _fetch_kline_range(symbol: str, start_ms: int, end_ms: int, interval: str, base_url: str, fetch_json: Callable[[str], object]) -> list[list[object]]:
    rows: list[list[object]] = []
    cursor = start_ms
    interval_ms = _interval_ms(interval)
    while cursor < end_ms:
        query = urlencode({"symbol": symbol, "interval": interval, "startTime": cursor, "endTime": end_ms, "limit": 1500})
        payload = fetch_json(f"{base_url.rstrip('/')}/fapi/v1/klines?{query}")
        if not isinstance(payload, list) or not payload:
            break
        batch = [_validate_kline_row(row, symbol) for row in payload]
        rows.extend(batch)
        next_cursor = int(batch[-1][0]) + interval_ms
        if next_cursor <= cursor:
            raise RuntimeError("Binance kline cursor did not advance")
        cursor = next_cursor
        if len(batch) < 1500:
            break
        time.sleep(0.05)
    return rows


def _read_funding_rows(path: Path, symbol: str) -> list[dict[str, object]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "rt", newline="", encoding="utf-8") as handle:
        rows: list[dict[str, object]] = []
        for row in csv.DictReader(handle):
            timestamp = int(row.get("funding_time", row.get("fundingTime", row.get("timestamp", "0"))))
            rate = float(row.get("funding_rate", row.get("fundingRate", "nan")))
            row_symbol = str(row.get("symbol") or symbol).upper()
            if row_symbol != symbol or timestamp <= 0 or not math.isfinite(rate) or abs(rate) > 1:
                raise ValueError(f"invalid funding row for {symbol}")
            rows.append({"symbol": symbol, "funding_time": timestamp, "funding_rate": rate})
    return rows


def _fetch_funding_range(symbol: str, start_ms: int, end_ms: int, base_url: str, fetch_json: Callable[[str], object]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    cursor = start_ms
    while cursor <= end_ms:
        query = urlencode({"symbol": symbol, "startTime": cursor, "endTime": end_ms, "limit": 1000})
        payload = fetch_json(f"{base_url.rstrip('/')}/fapi/v1/fundingRate?{query}")
        if not isinstance(payload, list) or not payload:
            break
        batch: list[dict[str, object]] = []
        for row in payload:
            if not isinstance(row, dict):
                raise ValueError(f"malformed Binance funding row for {symbol}")
            timestamp = int(row.get("fundingTime", 0))
            rate = float(row.get("fundingRate", "nan"))
            row_symbol = str(row.get("symbol") or symbol).upper()
            if row_symbol != symbol or timestamp <= 0 or not math.isfinite(rate) or abs(rate) > 1:
                raise ValueError(f"invalid Binance funding row for {symbol}")
            batch.append({"symbol": symbol, "funding_time": timestamp, "funding_rate": rate})
        rows.extend(batch)
        next_cursor = int(batch[-1]["funding_time"]) + 1
        if next_cursor <= cursor:
            raise RuntimeError("Binance funding cursor did not advance")
        cursor = next_cursor
        if len(batch) < 1000:
            break
        time.sleep(0.05)
    return rows


def fetch_exchange_info(base_url: str = "https://fapi.binance.com") -> dict[str, object]:
    payload = _fetch_json(f"{base_url.rstrip('/')}/fapi/v1/exchangeInfo")
    if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
        raise RuntimeError("Binance exchangeInfo response is malformed")
    return payload


def build_universe_manifest(exchange_info_snapshots: list[Path]) -> dict[str, object]:
    """Merge official exchangeInfo snapshots into point-in-time observations.

    A symbol remains in the manifest when it later becomes inactive. The
    snapshot name is retained so a research run can join membership to the
    timestamp at which the metadata was observed instead of applying today's
    universe retroactively.
    """
    by_symbol: dict[str, dict[str, object]] = {}
    snapshot_times: list[int] = []
    for snapshot in exchange_info_snapshots:
        try:
            snapshot_time = int(datetime.strptime(snapshot.stem.replace("exchangeInfo-", ""), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp() * 1000)
        except (ValueError, IndexError):
            snapshot_time = 0
        snapshot_times.append(snapshot_time)
        payload = json.loads(snapshot.read_text(encoding="utf-8"))
        for row in payload.get("symbols", []):
            symbol = str(row.get("symbol", "")).upper()
            if not symbol:
                continue
            record = by_symbol.setdefault(symbol, {"symbol": symbol, "snapshots": 0, "observations": []})
            record["snapshots"] = int(record["snapshots"]) + 1
            record["last_status"] = row.get("status")
            record["quote_asset"] = row.get("quoteAsset")
            record["margin_asset"] = row.get("marginAsset")
            record["contract_type"] = row.get("contractType")
            record["delivery_date"] = row.get("deliveryDate")
            record["onboard_date"] = row.get("onboardDate")
            record["eligible_contract"] = row.get("quoteAsset") == "USDT" and row.get("marginAsset") == "USDT" and row.get("contractType") == "PERPETUAL"
            record["observations"].append({"snapshot": snapshot.name, "observed_at": snapshot_time, "status": row.get("status"), "quote_asset": row.get("quoteAsset"), "margin_asset": row.get("marginAsset"), "contract_type": row.get("contractType"), "onboard_date": row.get("onboardDate"), "delivery_date": row.get("deliveryDate")})
    return {"schema": "sol-alerts-universe-v1", "snapshot_count": len(exchange_info_snapshots), "snapshot_times": sorted(snapshot_times), "symbols": sorted(by_symbol.values(), key=lambda row: str(row["symbol"]))}


def _manifest_availability(row: dict[str, object], start_ms: int, end_ms: int) -> tuple[int, int]:
    observations = [item for item in row.get("observations", []) if isinstance(item, dict)]
    onboard = [int(item.get("onboard_date") or 0) for item in observations if int(item.get("onboard_date") or 0) > 0]
    expected_start = max(start_ms, min(onboard) if onboard else start_ms)
    latest = max(observations, key=lambda item: int(item.get("observed_at") or 0), default={})
    observed_at = int(latest.get("observed_at") or 0)
    currently_observed = observed_at >= end_ms - INTERVAL_MS
    expected_end = end_ms if str(latest.get("status") or row.get("last_status") or "") == "TRADING" and currently_observed else min(end_ms, observed_at or end_ms)
    return expected_start, expected_end


def download_manifest(manifest: Path, start_ms: int, end_ms: int, output_dir: Path, funding_dir: Path | None = None, interval: str = "15m", base_url: str = "https://fapi.binance.com") -> dict[str, int]:
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    rows = payload.get("symbols", [])
    if not isinstance(rows, list):
        raise ValueError("universe manifest symbols must be an array")
    output_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for row in rows:
        if not row.get("eligible_contract"):
            continue
        symbol = str(row.get("symbol", "")).upper()
        if not symbol:
            continue
        symbol_start, symbol_end = _manifest_availability(row, start_ms, end_ms)
        if symbol_end <= symbol_start:
            continue
        counts[symbol] = download_klines(symbol, symbol_start, symbol_end, output_dir / f"{symbol}.jsonl.gz", interval, base_url)
        if funding_dir is not None:
            download_funding_rates(symbol, symbol_start, symbol_end, funding_dir / f"{symbol}.csv.gz", base_url)
    return counts


def download_klines(
    symbol: str,
    start_ms: int,
    end_ms: int,
    output: Path,
    interval: str = "15m",
    base_url: str = "https://fapi.binance.com",
    fetch_json: Callable[[str], object] | None = None,
) -> int:
    """Incrementally download and atomically merge public klines."""
    if start_ms >= end_ms or not symbol.endswith("USDT"):
        raise ValueError("only a positive USDT public-data interval is supported")
    fetcher = fetch_json or _fetch_json
    interval_ms = _interval_ms(interval)
    first_open, last_open = canonical_closed_bounds(start_ms, end_ms, interval_ms)
    existing = _read_kline_rows(output, symbol)
    existing_canonical = [
        row for row in existing
        if last_open >= first_open and _is_canonical_kline_row(row, symbol, interval_ms, first_open, last_open, end_ms)
    ]
    existing_times = sorted({int(row[0]) for row in existing_canonical})
    ranges: list[tuple[int, int]] = []
    if last_open >= first_open and not existing_times:
        ranges.append((first_open, end_ms))
    elif existing_times:
        first, last = existing_times[0], existing_times[-1]
        if first > first_open:
            ranges.append((first_open, min(first, end_ms)))
        if last < last_open:
            ranges.append((last + interval_ms, end_ms))
    fetched: list[list[object]] = []
    for range_start, range_end in ranges:
        if range_start < range_end:
            fetched.extend(_fetch_kline_range(symbol, range_start, range_end, interval, base_url, fetcher))
    merged = {
        int(row[0]): row
        for row in existing + fetched
        if last_open >= first_open and _is_canonical_kline_row(row, symbol, interval_ms, first_open, last_open, end_ms)
    }
    ordered = [merged[timestamp] for timestamp in sorted(merged)]
    if not ordered and not existing and not fetched:
        return 0
    _atomic_replace(output, lambda temporary: _write_kline_rows(temporary, ordered, output.name.endswith(".gz")))
    return len(ordered)


def download_funding_rates(
    symbol: str,
    start_ms: int,
    end_ms: int,
    output: Path,
    base_url: str = "https://fapi.binance.com",
    fetch_json: Callable[[str], object] | None = None,
) -> int:
    if start_ms > end_ms or not symbol.endswith("USDT"):
        raise ValueError("only public USDT funding history is supported")
    fetcher = fetch_json or _fetch_json
    existing = _read_funding_rows(output, symbol)
    existing_times = sorted({int(row["funding_time"]) for row in existing})
    ranges: list[tuple[int, int]] = []
    if not existing_times:
        ranges.append((start_ms, end_ms))
    else:
        first, last = existing_times[0], existing_times[-1]
        if first > start_ms:
            ranges.append((start_ms, min(first, end_ms)))
        if last < end_ms:
            ranges.append((max(start_ms, last + 1), end_ms))
    fetched: list[dict[str, object]] = []
    for range_start, range_end in ranges:
        if range_start <= range_end:
            fetched.extend(_fetch_funding_range(symbol, range_start, range_end, base_url, fetcher))
    merged = {int(row["funding_time"]): row for row in existing + fetched if start_ms <= int(row["funding_time"]) <= end_ms}
    ordered = [merged[timestamp] for timestamp in sorted(merged)]

    def write_csv(temporary: Path) -> None:
        opener = gzip.open if temporary.name.endswith(".gz") or output.name.endswith(".gz") else open
        with opener(temporary, "wt", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=("symbol", "funding_time", "funding_rate"))
            writer.writeheader()
            writer.writerows(ordered)

    if ordered or existing or fetched:
        _atomic_replace(output, write_csv)
    return len(ordered)


def main() -> int:
    parser = argparse.ArgumentParser(description="Download public Binance USDT perpetual klines")
    parser.add_argument("--symbol")
    parser.add_argument("--manifest-input", type=Path, help="point-in-time universe manifest; downloads every historically eligible contract")
    parser.add_argument("--start", required=True, help="UTC ISO-8601 timestamp")
    parser.add_argument("--end", required=True, help="UTC ISO-8601 timestamp")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--output-dir", type=Path, help="directory used with --manifest-input")
    parser.add_argument("--funding-dir", type=Path, help="directory used with --manifest-input")
    parser.add_argument("--interval", choices=("15m", "1h"), default="15m")
    parser.add_argument("--funding-output", type=Path, help="also download public funding rows to CSV")
    parser.add_argument("--exchange-info-output", type=Path, help="save the official exchangeInfo snapshot used for the manifest")
    args = parser.parse_args()
    parse = lambda value: int(datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=timezone.utc).timestamp() * 1000)
    start_ms, end_ms = parse(args.start), parse(args.end)
    if args.manifest_input:
        if args.output_dir is None:
            parser.error("--output-dir is required with --manifest-input")
        counts = download_manifest(args.manifest_input, start_ms, end_ms, args.output_dir, args.funding_dir, args.interval)
        print(json.dumps({"ok": True, "symbols": len(counts), "rows": sum(counts.values()), "format": "Binance JSONL gzip; read_candles accepts this exact format", "survivorship": "point-in-time manifest observations"}, sort_keys=True))
        return 0
    if not args.symbol:
        parser.error("--symbol is required unless --manifest-input is used")
    if args.output is None:
        parser.error("--output is required for a single symbol")
    count = download_klines(args.symbol.upper(), start_ms, end_ms, args.output, args.interval)
    funding_count = None
    if args.funding_output:
        funding_count = download_funding_rates(args.symbol.upper(), start_ms, end_ms, args.funding_output)
    if args.exchange_info_output:
        args.exchange_info_output.parent.mkdir(parents=True, exist_ok=True)
        args.exchange_info_output.write_text(json.dumps(fetch_exchange_info(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "symbol": args.symbol.upper(), "rows": count, "funding_rows": funding_count, "output": str(args.output), "format": "Binance JSONL gzip; read_candles accepts this exact format"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
