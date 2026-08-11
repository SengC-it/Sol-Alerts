from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from .download import FUNDING_INTERVAL_MS, build_universe_manifest, canonical_closed_bounds, download_funding_rates, download_klines, fetch_exchange_info
from .data import read_candles, read_funding_rates

DEFAULT_MAX_CACHE_BYTES = 10_000_000_000
INTERVAL_MS = 15 * 60_000


def _parse_time(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=timezone.utc).timestamp() * 1000)


def _closed_now_ms() -> int:
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    return now - (now % INTERVAL_MS) - 1


def _snapshot_name(end_ms: int) -> str:
    return datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc).strftime("exchangeInfo-%Y%m%dT%H%M%SZ.json")


def _cache_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def _funding_gaps(path: Path, symbol: str, expected_start: int, expected_end: int) -> list[dict[str, object]]:
    try:
        values = sorted(read_funding_rates(path).get(symbol, {}))
    except (OSError, ValueError) as error:
        return [{"symbol": symbol, "kind": "funding_missing_or_malformed", "detail": str(error)[:200]}]
    if not values:
        return [{"symbol": symbol, "kind": "funding_coverage_gap", "missing_start": expected_start, "missing_end": expected_end}]
    gaps: list[dict[str, object]] = []
    if values[0] > expected_start + FUNDING_INTERVAL_MS:
        gaps.append({"symbol": symbol, "kind": "funding_head_gap", "first": values[0], "expected_start": expected_start})
    if values[-1] < expected_end - FUNDING_INTERVAL_MS:
        gaps.append({"symbol": symbol, "kind": "funding_tail_gap", "last": values[-1], "expected_end": expected_end})
    for current, following in zip(values, values[1:]):
        if following - current > FUNDING_INTERVAL_MS:
            gaps.append({"symbol": symbol, "kind": "funding_internal_gap", "from": current, "to": following})
    return gaps


def _availability_bounds(row: dict[str, object], start_ms: int, end_ms: int) -> tuple[int, int]:
    observations = [item for item in row.get("observations", []) if isinstance(item, dict)]
    onboard_values = [int(item.get("onboard_date") or 0) for item in observations if int(item.get("onboard_date") or 0) > 0]
    expected_start = max(start_ms, min(onboard_values) if onboard_values else start_ms)
    latest = max(observations, key=lambda item: int(item.get("observed_at") or 0), default={})
    latest_status = str(latest.get("status") or row.get("last_status") or "")
    # A currently trading perpetual remains available through the run end.
    # For an inactive/delisted symbol, do not demand candles after its last
    # official observation. A future Binance perpetual deliveryDate is kept as
    # metadata and is never used to exclude the contract.
    latest_observed = int(latest.get("observed_at") or 0)
    # A symbol observed in the current snapshot is expected through the run
    # boundary. If later cached snapshots no longer contain it, its last
    # observed timestamp is the conservative availability end even when the
    # last row still said TRADING.
    currently_observed = latest_observed >= end_ms - INTERVAL_MS
    expected_end = end_ms if latest_status == "TRADING" and currently_observed else min(end_ms, latest_observed or end_ms)
    return expected_start, expected_end


def prepare_official_data(
    cache_dir: Path,
    start_ms: int,
    end_ms: int,
    base_url: str = "https://fapi.binance.com",
    max_cache_bytes: int = DEFAULT_MAX_CACHE_BYTES,
) -> dict[str, object]:
    """Prepare only official, compressed market inputs for a bounded run.

    Existing non-empty per-symbol files are reused; missing or empty files are
    fetched.  The function never creates synthetic candles or funding rows.
    """
    if start_ms >= end_ms:
        raise ValueError("end must be after start")
    manifests = cache_dir / "manifests"
    klines = cache_dir / "klines"
    funding = cache_dir / "funding"
    for directory in (manifests, klines, funding):
        directory.mkdir(parents=True, exist_ok=True)

    snapshot = manifests / _snapshot_name(end_ms)
    if not snapshot.exists():
        snapshot.write_text(json.dumps(fetch_exchange_info(base_url), sort_keys=True) + "\n", encoding="utf-8")
    snapshots = sorted(manifests.glob("exchangeInfo-*.json"))
    manifest = build_universe_manifest(snapshots)
    manifest_path = manifests / "universe-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    counts: dict[str, int] = {}
    gaps: list[dict[str, object]] = []
    funding_missing: list[str] = []
    availability_bounds: dict[str, dict[str, int]] = {}
    closed_candle_bounds: dict[str, dict[str, int]] = {}
    selection_cutoffs: list[int] = []
    liquidity_proxy_valid = True
    symbols = [str(row["symbol"]).upper() for row in manifest["symbols"] if row.get("eligible_contract")]
    manifest_snapshot_times = [int(value) for value in manifest.get("snapshot_times", []) if int(value) > 0]
    membership_supported = bool(manifest_snapshot_times) and min(manifest_snapshot_times) <= start_ms and max(manifest_snapshot_times) >= end_ms and int(manifest.get("snapshot_count", 0)) >= 2
    manifest_by_symbol = {str(row["symbol"]).upper(): row for row in manifest["symbols"]}
    for symbol in symbols:
        expected_start, expected_end = _availability_bounds(manifest_by_symbol[symbol], start_ms, end_ms)
        availability_bounds[symbol] = {"start_ms": expected_start, "end_ms": expected_end}
        if expected_end <= expected_start:
            gaps.append({"symbol": symbol, "kind": "no_official_availability_in_requested_window"})
            continue
        candle_path = klines / f"{symbol}.jsonl.gz"
        try:
            # download_klines validates existing bounds and fetches only a
            # missing head/tail. It replaces the gzip atomically after all
            # requested ranges have succeeded.
            counts[symbol] = download_klines(symbol, expected_start, expected_end, candle_path, "15m", base_url)
        except Exception as error:  # noqa: BLE001 - preserve a partial report and continue other symbols
            gaps.append({"symbol": symbol, "kind": "kline_fetch_failed", "detail": str(error)[:200]})
            counts[symbol] = 0
        try:
            candles = read_candles(candle_path)
        except (OSError, ValueError) as error:
            gaps.append({"symbol": symbol, "kind": "malformed_klines", "detail": str(error)[:200]})
            candles = []
        if not candles:
            gaps.append({"symbol": symbol, "kind": "missing_klines"})
        else:
            expected_symbol_start, expected_last_open = canonical_closed_bounds(expected_start, expected_end, INTERVAL_MS)
            expected_last_close = expected_last_open + INTERVAL_MS - 1
            closed_candle_bounds[symbol] = {"first_open_ms": expected_symbol_start, "last_close_ms": expected_last_close if expected_last_open >= expected_symbol_start else 0}
            canonical_candles = [
                candle for candle in candles
                if expected_last_open >= expected_symbol_start
                and expected_symbol_start <= candle.open_time <= expected_last_open
                and candle.open_time % INTERVAL_MS == 0
                and candle.close_time - candle.open_time == INTERVAL_MS - 1
                and candle.close_time <= expected_end
            ]
            if len(canonical_candles) != len(candles):
                gaps.append({"symbol": symbol, "kind": "noncanonical_kline_rows", "count": len(candles) - len(canonical_candles), "expected_end": expected_end})
            if not canonical_candles or canonical_candles[0].open_time > expected_symbol_start or canonical_candles[-1].close_time < expected_last_close:
                gaps.append({"symbol": symbol, "kind": "coverage_gap", "first": candles[0].open_time, "last": candles[-1].close_time, "expected_start": expected_symbol_start, "expected_last": expected_last_close})
            if canonical_candles:
                selection_cutoffs.append(max(candle.close_time for candle in canonical_candles))
        if len(candles) > 1:
            internal_gaps = sum(1 for current, following in zip(candles, candles[1:]) if following.open_time - current.open_time != INTERVAL_MS)
            if internal_gaps:
                gaps.append({"symbol": symbol, "kind": "internal_kline_gaps", "count": internal_gaps})
            if any(not all(math.isfinite(value) for value in (candle.open, candle.high, candle.low, candle.close, candle.volume, candle.quote_volume)) or min(candle.open, candle.high, candle.low, candle.close) <= 0 or candle.volume < 0 or candle.quote_volume < 0 for candle in candles):
                liquidity_proxy_valid = False
                gaps.append({"symbol": symbol, "kind": "historical_liquidity_proxy_invalid"})
        funding_path = funding / f"{symbol}.csv.gz"
        try:
            # A non-empty funding file is not assumed complete; the downloader
            # extends its missing tail/head and atomically merges/deduplicates.
            download_funding_rates(symbol, expected_start, expected_end, funding_path, base_url)
        except Exception as error:  # noqa: BLE001 - retain a fail-closed report for one symbol
            gaps.append({"symbol": symbol, "kind": "funding_fetch_failed", "detail": str(error)[:200]})
        symbol_funding_gaps = _funding_gaps(funding_path, symbol, expected_start, expected_end)
        if symbol_funding_gaps:
            funding_missing.append(symbol)
            gaps.extend(symbol_funding_gaps)

    size = _cache_bytes(cache_dir)
    report = {
        "schema": "sol-alerts-prepared-data-v1",
        "input_root": str(cache_dir),
        "manifest": str(manifest_path),
        "start_ms": start_ms,
        "end_ms": end_ms,
        "selection_cutoff_ms": max(selection_cutoffs, default=0),
        "symbols": symbols,
        "candle_rows": counts,
        "gaps": gaps,
        "funding_missing": sorted(set(funding_missing)),
        "funding_gaps": [gap for gap in gaps if str(gap.get("kind", "")).startswith("funding_")],
        "availability_bounds": availability_bounds,
        "closed_candle_bounds": closed_candle_bounds,
        "cache_bytes": size,
        "cache_limit_bytes": max_cache_bytes,
        "point_in_time_membership_supported": membership_supported,
        "historical_liquidity_proxy_supported": liquidity_proxy_valid,
        "liquidity_proxy": {"schema": "sol-alerts-impact-proxy-v1", "formula": "0.0005 + 0.5*realized_volatility + 0.002*min(1,order_notional/depth_proxy) + 0.001*min(1,order_notional/reference_max)", "historical_spread_depth": "not observable in public klines; conservative quote-volume/volatility/order-size proxy only"},
        "formal_ready": not gaps and not funding_missing and membership_supported,
        "survivorship": "manifest merges cached official snapshots; per-symbol onboard/inactive bounds are applied. A single current snapshot is not survivorship-free point-in-time evidence.",
    }
    (cache_dir / "prepare-report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if size > max_cache_bytes:
        raise RuntimeError(f"prepared market-data cache exceeds {max_cache_bytes} bytes")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare bounded official Binance USDT perpetual research inputs")
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--start", required=True, help="UTC ISO-8601 timestamp")
    parser.add_argument("--end", required=True, help="UTC ISO-8601 timestamp for the last closed boundary")
    parser.add_argument("--base-url", default="https://fapi.binance.com")
    parser.add_argument("--max-cache-bytes", type=int, default=DEFAULT_MAX_CACHE_BYTES)
    args = parser.parse_args()
    report = prepare_official_data(args.cache_dir, _parse_time(args.start), _parse_time(args.end), args.base_url, args.max_cache_bytes)
    print(json.dumps({"ok": True, "formal_ready": report["formal_ready"], "symbols": len(report["symbols"]), "gaps": len(report["gaps"]), "cache_bytes": report["cache_bytes"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
