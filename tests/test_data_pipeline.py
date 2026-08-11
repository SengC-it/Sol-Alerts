from __future__ import annotations

import csv
import gzip
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from sol_alerts import prepare_data
from sol_alerts.backtest import _validate_prepared_provenance
from sol_alerts.data import read_candles
from sol_alerts.download import INTERVAL_MS, canonical_closed_bounds, download_funding_rates, download_klines


def _kline(timestamp: int, close: float = 100.0) -> list[object]:
    return [timestamp, str(close), str(close + 1), str(close - 1), str(close), "10", timestamp + INTERVAL_MS - 1, str(close * 10), "1", "0", "0", "0"]


def _interval_kline(timestamp: int, interval_ms: int, close: float = 100.0) -> list[object]:
    return [timestamp, str(close), str(close + 1), str(close - 1), str(close), "10", timestamp + interval_ms - 1, str(close * 10), "1", "0", "0", "0"]


def _write_klines(path: Path, rows: list[list[object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _write_funding(path: Path, rows: list[tuple[str, int, float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=("symbol", "funding_time", "funding_rate"))
        writer.writeheader()
        for symbol, timestamp, rate in rows:
            writer.writerow({"symbol": symbol, "funding_time": timestamp, "funding_rate": rate})


def test_incremental_klines_merge_deduplicates_bounds_and_fetches_only_tail(tmp_path: Path) -> None:
    output = tmp_path / "BTCUSDT.jsonl.gz"
    _write_klines(output, [_kline(index * INTERVAL_MS, 100 + index) for index in range(4)])
    calls: list[str] = []

    def fake_fetch(url: str) -> object:
        calls.append(url)
        query = parse_qs(urlparse(url).query)
        assert int(query["startTime"][0]) == 4 * INTERVAL_MS
        return [_kline(4 * INTERVAL_MS, 104), _kline(5 * INTERVAL_MS, 105), _kline(5 * INTERVAL_MS, 106)]

    assert download_klines("BTCUSDT", 0, 6 * INTERVAL_MS, output, fetch_json=fake_fetch) == 6
    assert len(calls) == 1
    with gzip.open(output, "rt", encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    assert [int(row[0]) for row in rows] == [index * INTERVAL_MS for index in range(6)]
    assert float(rows[-1][1]) == 106

    calls.clear()
    assert download_klines("BTCUSDT", 0, 6 * INTERVAL_MS, output, fetch_json=fake_fetch) == 6
    assert calls == []


def test_incremental_kline_failure_preserves_previous_valid_file(tmp_path: Path) -> None:
    output = tmp_path / "BTCUSDT.jsonl.gz"
    _write_klines(output, [_kline(index * INTERVAL_MS) for index in range(4)])
    before = hashlib.sha256(output.read_bytes()).hexdigest()

    def failing_fetch(_url: str) -> object:
        raise OSError("simulated tail timeout")

    with pytest.raises(OSError, match="tail timeout"):
        download_klines("BTCUSDT", 0, 6 * INTERVAL_MS, output, fetch_json=failing_fetch)
    assert hashlib.sha256(output.read_bytes()).hexdigest() == before


def test_closed_kline_boundary_excludes_late_bar_and_matches_formal_validator(tmp_path: Path) -> None:
    output = tmp_path / "klines" / "BTCUSDT.jsonl.gz"
    end_ms = 2 * INTERVAL_MS + 60_000

    def fake_fetch(_url: str) -> object:
        return [_kline(0, 100), _kline(INTERVAL_MS, 101), _kline(2 * INTERVAL_MS, 102)]

    assert download_klines("BTCUSDT", 0, end_ms, output, fetch_json=fake_fetch) == 2
    candles = read_candles(output)
    assert [candle.open_time for candle in candles] == [0, INTERVAL_MS]
    assert candles[-1].close_time <= end_ms
    assert canonical_closed_bounds(0, end_ms, INTERVAL_MS) == (0, INTERVAL_MS)

    manifest_path = tmp_path / "manifests" / "universe-manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps({"symbols": [{"symbol": "BTCUSDT", "eligible_contract": True}]}), encoding="utf-8")
    report_path = tmp_path / "prepare-report.json"
    report_path.write_text(json.dumps({
        "schema": "sol-alerts-prepared-data-v1",
        "formal_ready": True,
        "input_root": str(output.parent),
        "start_ms": 0,
        "end_ms": end_ms,
        "symbols": ["BTCUSDT"],
        "manifest": str(manifest_path),
        "gaps": [],
        "funding_missing": [],
        "cache_bytes": 1,
        "cache_limit_bytes": 10_000_000_000,
        "point_in_time_membership_supported": True,
        "historical_liquidity_proxy_supported": True,
        "liquidity_proxy": {"schema": "sol-alerts-impact-proxy-v1"},
        "availability_bounds": {"BTCUSDT": {"start_ms": 0, "end_ms": end_ms}},
    }), encoding="utf-8")
    funding = {"BTCUSDT": {INTERVAL_MS: 0.001}}
    _validate_prepared_provenance(report_path, output.parent, tmp_path / "funding", {"BTCUSDT": candles}, funding)

    invalid = tmp_path / "invalid-root" / "klines" / "BTCUSDT.jsonl.gz"
    _write_klines(invalid, [_kline(0, 100), _kline(INTERVAL_MS, 101), _kline(2 * INTERVAL_MS, 102)])
    with pytest.raises(ValueError, match="prepared candle bounds"):
        _validate_prepared_provenance(report_path, invalid.parent, tmp_path / "funding", {"BTCUSDT": read_candles(invalid)}, funding)


def test_prepare_report_uses_same_closed_boundary_and_cutoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    symbol = "BOUNDARYUSDT"
    end_ms = 2 * INTERVAL_MS + 60_000
    monkeypatch.setattr(prepare_data, "fetch_exchange_info", lambda _base_url: {"symbols": [{"symbol": symbol, "quoteAsset": "USDT", "marginAsset": "USDT", "contractType": "PERPETUAL", "status": "TRADING", "onboardDate": 0, "deliveryDate": 4_133_404_800_000}]})

    def fake_klines(_symbol: str, _start_ms: int, _end_ms: int, output: Path, *_args: object) -> int:
        _write_klines(output, [_kline(0), _kline(INTERVAL_MS, 101), _kline(2 * INTERVAL_MS, 102)])
        return 3

    def fake_funding(_symbol: str, start_ms: int, _end_ms: int, output: Path, *_args: object) -> int:
        _write_funding(output, [(symbol, start_ms, 0.001)])
        return 1

    monkeypatch.setattr(prepare_data, "download_klines", fake_klines)
    monkeypatch.setattr(prepare_data, "download_funding_rates", fake_funding)
    report = prepare_data.prepare_official_data(tmp_path, 0, end_ms)
    assert report["formal_ready"] is False
    assert any(gap["kind"] == "noncanonical_kline_rows" for gap in report["gaps"])
    assert report["selection_cutoff_ms"] == 2 * INTERVAL_MS - 1
    assert report["selection_cutoff_ms"] <= end_ms


def test_incremental_kline_tail_uses_requested_interval(tmp_path: Path) -> None:
    output = tmp_path / "BTCUSDT-1h.jsonl.gz"
    hour = 60 * 60_000
    _write_klines(output, [_interval_kline(index * hour, hour, 100 + index) for index in range(2)])
    calls: list[str] = []

    def fake_fetch(url: str) -> object:
        calls.append(url)
        query = parse_qs(urlparse(url).query)
        assert int(query["startTime"][0]) == 2 * hour
        return [_interval_kline(2 * hour, hour, 102)]

    assert download_klines("BTCUSDT", 0, 3 * hour, output, interval="1h", fetch_json=fake_fetch) == 3
    assert len(calls) == 1
    assert [candle.open_time for candle in read_candles(output)] == [0, hour, 2 * hour]


def test_incremental_funding_advances_tail_and_preserves_file_on_failure(tmp_path: Path) -> None:
    output = tmp_path / "BTCUSDT.csv.gz"
    funding_interval = 8 * 60 * 60_000
    _write_funding(output, [("BTCUSDT", funding_interval, 0.001)])
    calls: list[str] = []
    requested_start = funding_interval

    def fake_fetch(url: str) -> object:
        calls.append(url)
        query = parse_qs(urlparse(url).query)
        assert int(query["startTime"][0]) == funding_interval + 1
        return [
            {"symbol": "BTCUSDT", "fundingTime": funding_interval * 2, "fundingRate": "-0.001"},
            {"symbol": "BTCUSDT", "fundingTime": funding_interval * 3, "fundingRate": "0.002"},
            {"symbol": "BTCUSDT", "fundingTime": funding_interval * 3, "fundingRate": "0.002"},
        ]

    assert download_funding_rates("BTCUSDT", requested_start, funding_interval * 3, output, fetch_json=fake_fetch) == 3
    assert len(calls) == 1
    calls.clear()
    assert download_funding_rates("BTCUSDT", requested_start, funding_interval * 3, output, fetch_json=fake_fetch) == 3
    assert calls == []

    before = hashlib.sha256(output.read_bytes()).hexdigest()

    def failing_fetch(_url: str) -> object:
        raise OSError("simulated funding timeout")

    with pytest.raises(OSError, match="funding timeout"):
        download_funding_rates("BTCUSDT", requested_start, funding_interval * 4, output, fetch_json=failing_fetch)
    assert hashlib.sha256(output.read_bytes()).hexdigest() == before


def test_prepare_data_returns_partial_report_without_claiming_formal_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    symbol = "NEWUSDT"
    monkeypatch.setattr(prepare_data, "fetch_exchange_info", lambda _base_url: {"symbols": [{"symbol": symbol, "quoteAsset": "USDT", "marginAsset": "USDT", "contractType": "PERPETUAL", "status": "TRADING", "onboardDate": 0, "deliveryDate": 4_133_404_800_000}]})

    def fake_klines(_symbol: str, start_ms: int, _end_ms: int, output: Path, *_args: object) -> int:
        _write_klines(output, [_kline(start_ms)])
        return 1

    def fake_funding(_symbol: str, start_ms: int, _end_ms: int, output: Path, *_args: object) -> int:
        _write_funding(output, [(symbol, start_ms, 0.001)])
        return 1

    monkeypatch.setattr(prepare_data, "download_klines", fake_klines)
    monkeypatch.setattr(prepare_data, "download_funding_rates", fake_funding)
    report = prepare_data.prepare_official_data(tmp_path, 1_700_000_000_000, 1_700_000_000_000 + 2 * 8 * 60 * 60_000)
    report_path = tmp_path / "prepare-report.json"
    assert report_path.is_file()
    assert report["formal_ready"] is False
    assert report["point_in_time_membership_supported"] is False
    assert report["gaps"]
    assert report["funding_gaps"]
    assert json.loads(report_path.read_text(encoding="utf-8"))["formal_ready"] is False
