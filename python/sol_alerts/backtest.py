from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import load_config
from .data import Candle, conservative_impact_proxy, read_candles, read_funding_rates, validate_closed_series
from .download import canonical_closed_bounds
from .fixtures import research_fixture_candles
from .portfolio import Trade, simulate_portfolio, simulate_signal
from .statistics import block_bootstrap_lower_bound, concentration_share, max_drawdown_from_r
from .strategies import Signal, generate_signals
from .weights import build_candidate_descriptor, descriptor_hash, validate_descriptor

FAMILIES = ("trend_breakout", "trend_pullback", "range_mean_reversion")
DIRECTIONS = ("long", "short")
INTERVAL_MS = 15 * 60_000
OOS_BLOCK_MS = 7 * 86_400_000


def _smoke_inputs() -> dict[str, list[Candle]]:
    return {symbol: research_fixture_candles(symbol) for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT")}


def _owned_symbols(symbols: set[str] | list[str], shard_index: int, shard_count: int) -> list[str]:
    return sorted(symbols)[shard_index::shard_count]


def _load_inputs(input_path: Path | None, *, fixture: bool = False, symbols: set[str] | None = None) -> dict[str, list[Candle]]:
    """Load explicit official inputs; fixture mode is only for --smoke."""
    if fixture:
        if input_path is not None:
            raise ValueError("--smoke cannot be combined with --input")
        return _smoke_inputs()
    if input_path is None:
        raise ValueError("official backtest requires --input; use --smoke for fixtures")
    if not input_path.is_dir():
        raise ValueError(f"official candle input directory does not exist: {input_path}")
    files = sorted(path for path in input_path.iterdir() if path.is_file() and path.name.endswith((".csv", ".csv.gz", ".jsonl.gz", ".json.gz")))
    if not files:
        raise ValueError(f"no CSV or Binance JSONL candle files found in {input_path}")
    result: dict[str, list[Candle]] = {}
    for file in files:
        name = file.name
        for suffix in (".jsonl.gz", ".json.gz", ".csv.gz", ".csv"):
            if name.endswith(suffix):
                name = name[: -len(suffix)]
                break
        if symbols is not None and name.upper() not in symbols:
            continue
        candles = read_candles(file)
        if len(candles) < 240:
            raise ValueError(f"insufficient official candles for {name}")
        validate_closed_series(candles, INTERVAL_MS, candles[-1].close_time + 1)
        result[name.upper()] = candles
    return result


def _gap_count(candles: list[Candle]) -> int:
    return sum(1 for current, following in zip(candles, candles[1:]) if following.open_time - current.open_time != INTERVAL_MS)


def _score(signal: Signal, evidence: dict[str, Any], config: dict) -> float:
    score = 0.0
    score += min(config["score"]["historicalRobustnessMax"], max(0.0, evidence.get("mean_r_lower_bound_95", 0.0) + 0.25) / 1.25 * config["score"]["historicalRobustnessMax"])
    score += min(config["score"]["currentSetupMax"], max(0.0, (signal.gross_reward_risk - 2) / 3) * config["score"]["currentSetupMax"])
    score += config["score"]["marketStateMatchMax"]
    score += config["score"]["executionQualityMax"]
    return max(0.0, min(100.0, score))


def _parameter_keys(config: dict[str, Any]) -> dict[str, list[str]]:
    breakout = [
        f"lookback={lookback};buffer={buffer};volume={multiplier}"
        for lookback in config["strategies"]["trend_breakout"]["lookbacks"]
        for buffer in config["strategies"]["trend_breakout"]["closeBufferAtr"]
        for multiplier in config["strategies"]["trend_breakout"]["volumeMultipliers"]
    ]
    pullback = [f"ema={config['strategies']['trend_pullback']['emaPeriod']};structure={config['strategies']['trend_pullback']['structureLookback']}" ]
    mean_reversion = [
        f"bb={deviation};rsi={lower}/{upper}"
        for deviation in config["strategies"]["range_mean_reversion"]["bollingerDeviations"]
        for lower, upper in config["strategies"]["range_mean_reversion"]["rsiThresholds"]
    ]
    return {"trend_breakout": breakout, "trend_pullback": pullback, "range_mean_reversion": mean_reversion}


def _mean_net_r(trades: list[Trade]) -> float:
    values = [trade.net_r for trade in trades]
    return sum(values) / len(values) if values else 0.0


def _trades_for(
    signals: list[Signal],
    candles: list[Candle],
    config: dict,
    funding: dict[int, float],
    start_time: int | None = None,
    end_time: int | None = None,
) -> list[Trade]:
    trades: list[Trade] = []
    for signal in signals:
        trade = simulate_signal(signal, candles, config, funding)
        if trade is None:
            continue
        if start_time is not None and trade.entry_time < start_time:
            continue
        if end_time is not None and trade.exit_time > end_time:
            continue
        trades.append(trade)
    return sorted(trades, key=lambda trade: (trade.entry_time, trade.exit_time, trade.signal.symbol, trade.signal.parameter_key))


def _split_times(candles: list[Candle], config: dict, window_start: int | None = None, window_end: int | None = None) -> tuple[int, int, int, int]:
    if not candles:
        raise ValueError("cannot split an empty candle series")
    start = max(candles[0].open_time, window_start if window_start is not None else candles[0].open_time)
    end = min(candles[-1].close_time, window_end if window_end is not None else candles[-1].close_time)
    if end <= start:
        raise ValueError("adaptive window does not overlap candle history")
    span = end - start
    train_end = start + int(span * config["research"]["trainFraction"])
    validation_end = train_end + int(span * config["research"]["validationFraction"])
    return start, train_end, validation_end, end


def _common_untouched_interval(
    candles_by_symbol: dict[str, list[Candle]],
    prepared_report: dict[str, Any],
    config: dict[str, Any],
    cutoff: int,
    window_start: int | None = None,
) -> tuple[int, int]:
    """Use one time interval for both frozen replay descriptors and all shards."""
    report_start = int(prepared_report.get("start_ms", 0)) if prepared_report else 0
    history_start = min((candles[0].open_time for candles in candles_by_symbol.values() if candles), default=0)
    start = window_start or report_start or history_start
    if start >= cutoff:
        raise ValueError("untouched test interval is empty")
    span = cutoff - start
    test_start = start + int(span * (float(config["research"]["trainFraction"]) + float(config["research"]["validationFraction"])))
    if test_start >= cutoff:
        raise ValueError("untouched test interval is empty")
    return test_start, cutoff


def _calendar_block_ids(start_time: int, end_time: int) -> list[str]:
    if start_time > end_time:
        raise ValueError("calendar block interval is inverted")
    first = (start_time // OOS_BLOCK_MS) * OOS_BLOCK_MS
    last = (end_time // OOS_BLOCK_MS) * OOS_BLOCK_MS
    return [str(block_start) for block_start in range(first, last + OOS_BLOCK_MS, OOS_BLOCK_MS)]


def _calendar_oos_blocks(events: list[dict[str, Any]], start_time: int, end_time: int) -> list[dict[str, Any]]:
    block_ids = _calendar_block_ids(start_time, end_time)
    values: dict[str, list[float]] = {block_id: [] for block_id in block_ids}
    valid_ids = set(values)
    for event in events:
        exit_time = int(event.get("exit_time", 0))
        if start_time <= exit_time <= end_time:
            block_id = str((exit_time // OOS_BLOCK_MS) * OOS_BLOCK_MS)
            if block_id in valid_ids:
                values[block_id].append(float(event.get("net_r", 0.0)))
    return [{"block_id": block_id, "mean_r": sum(rows) / len(rows) if rows else 0.0} for block_id, rows in values.items()]


def _select_global_parameters(
    candles_by_symbol: dict[str, list[Candle]],
    signals_by_symbol: dict[str, list[Signal]],
    funding_by_symbol: dict[str, dict[int, float]],
    config: dict[str, Any],
    global_cutoff: int | None = None,
) -> tuple[dict[str, dict[str, str]], list[dict[str, Any]]]:
    """Select one fixed-grid key per family+direction using train/validation only."""
    keys_by_family = _parameter_keys(config)
    stats = _selection_statistics(candles_by_symbol, signals_by_symbol, funding_by_symbol, config, global_cutoff)
    selected: dict[str, dict[str, str]] = {}
    evidence: list[dict[str, Any]] = []
    for family in FAMILIES:
        for direction in DIRECTIONS:
            ranked: list[tuple[float, float, str]] = []
            for key in keys_by_family[family]:
                row = stats[(family, direction, key)]
                train_mean = float(row["train_sum_r"]) / int(row["train_trades"]) if row["train_trades"] else 0.0
                validation_mean = float(row["validation_sum_r"]) / int(row["validation_trades"]) if row["validation_trades"] else 0.0
                ranked.append((validation_mean, train_mean, key))
                evidence.append({"family": family, "direction": direction, "parameter_key": key, "train_trades": row["train_trades"], "train_sum_r": row["train_sum_r"], "train_mean_r": train_mean, "validation_trades": row["validation_trades"], "validation_sum_r": row["validation_sum_r"], "validation_mean_r": validation_mean})
            selected.setdefault(family, {})[direction] = max(ranked, key=lambda item: (item[0], item[1], item[2]))[2]
    return selected, evidence


def _selection_statistics(
    candles_by_symbol: dict[str, list[Candle]],
    signals_by_symbol: dict[str, list[Signal]],
    funding_by_symbol: dict[str, dict[int, float]],
    config: dict[str, Any],
    global_cutoff: int | None = None,
) -> dict[tuple[str, str, str], dict[str, float | int]]:
    """Return mergeable train/validation sufficient statistics only.

    The returned rows contain counts and sums, never raw signals or trades.
    Every shard can calculate the same rows for its disjoint symbol subset and
    the merge is therefore exactly equivalent to one monolithic selection.
    """
    keys_by_family = _parameter_keys(config)
    stats: dict[tuple[str, str, str], dict[str, float | int]] = {
        (family, direction, key): {"train_trades": 0, "train_sum_r": 0.0, "validation_trades": 0, "validation_sum_r": 0.0}
        for family in FAMILIES
        for direction in DIRECTIONS
        for key in keys_by_family[family]
    }
    cutoff = global_cutoff if global_cutoff is not None else max((candles[-1].close_time for candles in candles_by_symbol.values()), default=0)
    window_start = cutoff - config["research"]["adaptiveLongWindowMonths"] * 30 * 86_400_000
    for symbol, candles in candles_by_symbol.items():
        start, validation_start, test_start, _end = _split_times(candles, config, window_start, cutoff)
        for signal in signals_by_symbol.get(symbol, []):
            if signal.index < 95 or sum(candle.quote_volume for candle in candles[max(0, signal.index - 95) : signal.index + 1]) < float(config["universe"]["minQuoteVolume24h"]):
                continue
            trade = simulate_signal(signal, candles, config, funding_by_symbol.get(symbol, {}))
            if trade is None:
                continue
            key = (signal.family, signal.direction, signal.parameter_key)
            if key not in stats:
                continue
            if start <= trade.entry_time and trade.exit_time < validation_start:
                stats[key]["train_trades"] = int(stats[key]["train_trades"]) + 1
                stats[key]["train_sum_r"] = float(stats[key]["train_sum_r"]) + trade.net_r
            elif validation_start <= trade.entry_time and trade.exit_time < test_start:
                stats[key]["validation_trades"] = int(stats[key]["validation_trades"]) + 1
                stats[key]["validation_sum_r"] = float(stats[key]["validation_sum_r"]) + trade.net_r
    return stats


def prepare_selection_shard(
    input_path: Path,
    funding_path: Path,
    prepare_report_path: Path,
    output_path: Path,
    shard_index: int,
    shard_count: int,
) -> dict[str, Any]:
    config = load_config()
    if _descriptor is not None and not validate_descriptor(config, _descriptor):
        raise ValueError("evaluation descriptor is missing or hash-invalid")
    prepared = json.loads(prepare_report_path.read_text(encoding="utf-8"))
    expected_symbols = {str(symbol).upper() for symbol in prepared.get("symbols", [])}
    owned_symbols = set(_owned_symbols(expected_symbols, shard_index, shard_count))
    all_candles = _load_inputs(input_path, symbols=owned_symbols)
    if set(all_candles) != owned_symbols:
        raise ValueError("selection shard candle symbols do not match deterministic ownership")
    funding_by_symbol = read_funding_rates(funding_path, owned_symbols)
    _validate_prepared_provenance(prepare_report_path, input_path, funding_path, all_candles, funding_by_symbol, expected_symbols)
    all_signals = _all_signals(all_candles, config)
    stats = _selection_statistics(all_candles, all_signals, funding_by_symbol, config, int(prepared.get("selection_cutoff_ms", 0)) or int(prepared.get("end_ms", 0)) or None)
    rows = [{"family": family, "direction": direction, "parameter_key": key, **values} for (family, direction, key), values in sorted(stats.items())]
    result = {
        "schema": "sol-alerts-selection-evidence-v1",
        "config_version": config["configVersion"],
        "selection_shard": {"index": shard_index, "count": shard_count, "symbols": sorted(owned_symbols)},
        "symbols": sorted(owned_symbols),
        "prepare_report_sha256": hashlib.sha256(prepare_report_path.read_bytes()).hexdigest(),
        "statistics": rows,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def merge_selection_evidence(
    evidence_reports: list[dict[str, Any]],
    expected_symbols: set[str],
    config: dict[str, Any],
    shard_count: int,
    prepare_report_sha256: str,
) -> dict[str, Any]:
    if len(evidence_reports) != shard_count:
        raise ValueError("selection evidence shard count is incomplete")
    owned: set[str] = set()
    aggregate: dict[tuple[str, str, str], dict[str, float | int]] = {}
    indexes: set[int] = set()
    expected_stat_keys = {(family, direction, key) for family in FAMILIES for direction in DIRECTIONS for key in _parameter_keys(config)[family]}
    for report in evidence_reports:
        if report.get("schema") != "sol-alerts-selection-evidence-v1" or report.get("config_version") != config["configVersion"]:
            raise ValueError("selection evidence config or schema mismatch")
        if report.get("prepare_report_sha256") != prepare_report_sha256:
            raise ValueError("selection evidence provenance mismatch")
        shard = report.get("selection_shard", {})
        index, count = int(shard.get("index", -1)), int(shard.get("count", -1))
        if count != shard_count or index < 0 or index >= shard_count or index in indexes:
            raise ValueError("invalid or duplicate selection shard index")
        indexes.add(index)
        symbols = {str(symbol).upper() for symbol in report.get("symbols", [])}
        if symbols != {str(symbol).upper() for symbol in shard.get("symbols", [])} or owned.intersection(symbols):
            raise ValueError("selection shard ownership overlaps or is malformed")
        owned.update(symbols)
        rows = report.get("statistics", [])
        if not isinstance(rows, list):
            raise ValueError("selection evidence statistics are malformed")
        row_keys = {(str(row.get("family")), str(row.get("direction")), str(row.get("parameter_key"))) for row in rows if isinstance(row, dict)}
        if row_keys != expected_stat_keys:
            raise ValueError("selection evidence parameter grid is incomplete")
        for row in rows:
            key = (str(row.get("family")), str(row.get("direction")), str(row.get("parameter_key")))
            if key in aggregate:
                target = aggregate[key]
            else:
                target = {"train_trades": 0, "train_sum_r": 0.0, "validation_trades": 0, "validation_sum_r": 0.0}
                aggregate[key] = target
            for field in target:
                target[field] = int(target[field]) + int(row.get(field, 0)) if field.endswith("trades") else float(target[field]) + float(row.get(field, 0.0))
    if indexes != set(range(shard_count)) or owned != {str(symbol).upper() for symbol in expected_symbols}:
        raise ValueError("selection evidence does not cover the complete disjoint universe")
    keys_by_family = _parameter_keys(config)
    selected: dict[str, dict[str, str]] = {}
    evidence: list[dict[str, Any]] = []
    for family in FAMILIES:
        for direction in DIRECTIONS:
            ranked: list[tuple[float, float, str]] = []
            for key in keys_by_family[family]:
                row = aggregate.get((family, direction, key), {"train_trades": 0, "train_sum_r": 0.0, "validation_trades": 0, "validation_sum_r": 0.0})
                train_mean = float(row["train_sum_r"]) / int(row["train_trades"]) if row["train_trades"] else 0.0
                validation_mean = float(row["validation_sum_r"]) / int(row["validation_trades"]) if row["validation_trades"] else 0.0
                ranked.append((validation_mean, train_mean, key))
                evidence.append({"family": family, "direction": direction, "parameter_key": key, "train_trades": int(row["train_trades"]), "train_sum_r": float(row["train_sum_r"]), "train_mean_r": train_mean, "validation_trades": int(row["validation_trades"]), "validation_sum_r": float(row["validation_sum_r"]), "validation_mean_r": validation_mean})
            selected.setdefault(family, {})[direction] = max(ranked, key=lambda item: (item[0], item[1], item[2]))[2]
    return {"schema": "sol-alerts-selection-v1", "config_version": config["configVersion"], "symbols": sorted(owned), "selected_parameters": selected, "selection_evidence": evidence, "selection_source": {"shards": shard_count, "sufficient_statistics": True, "prepare_report_sha256": prepare_report_sha256}}


def merge_selection_files(paths: list[Path], prepare_report_path: Path, output_path: Path, shard_count: int) -> dict[str, Any]:
    config = load_config()
    prepared = json.loads(prepare_report_path.read_text(encoding="utf-8"))
    if prepared.get("schema") != "sol-alerts-prepared-data-v1" or prepared.get("formal_ready") is not True:
        raise ValueError("formal selection requires a formally ready prepare report")
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(paths)]
    result = merge_selection_evidence(reports, {str(symbol).upper() for symbol in prepared.get("symbols", [])}, config, shard_count, hashlib.sha256(prepare_report_path.read_bytes()).hexdigest())
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def _evidence(trades: list[Trade], candles: list[Candle], config: dict, symbol: str, family: str, direction: str) -> dict[str, Any]:
    ordered = sorted(trades, key=lambda trade: (trade.exit_time, trade.entry_time, trade.signal.symbol))
    rs = [trade.net_r for trade in ordered]
    lower = block_bootstrap_lower_bound(rs, config["research"]["randomSeed"], min(2_000, config["research"]["bootstrapReplicates"])) if rs else 0.0
    mean_r = sum(rs) / len(rs) if rs else 0.0
    history_days = (candles[-1].close_time - candles[0].open_time) / 86_400_000 if candles else 0.0
    max_dd = max_drawdown_from_r(rs)
    return {
        "symbol": symbol,
        "family": family,
        "direction": direction,
        "history_days": history_days,
        "untouched_test_trades": len(rs),
        "untouched_mean_r": mean_r,
        "untouched_max_drawdown": max_dd,
        "mean_r_lower_bound_95": lower,
        "robustness_score": max(0.0, min(30.0, (lower + 0.25) / 1.25 * 30.0)),
        "formal_eligible": history_days >= config["universe"]["minFormalHistoryDays"] and len(rs) >= config["research"]["minimumTestTradesPerSymbolStrategy"] and lower > 0 and max_dd <= config["research"]["maxPortfolioDrawdown"],
    }


def _signals_for_test(
    symbol: str,
    candles: list[Candle],
    config: dict,
    funding: dict[int, float] | None = None,
    selected_parameters: dict[str, dict[str, str]] | None = None,
    precomputed_signals: list[Signal] | None = None,
    window_start: int | None = None,
    window_end: int | None = None,
) -> tuple[list[Signal], dict[str, dict[str, Any]]]:
    if selected_parameters is None:
        raise ValueError("formal test evaluation requires globally selected family-direction parameters")
    _start, _validation_start, test_start, test_end = _split_times(candles, config, window_start, window_end)
    all_signals = precomputed_signals if precomputed_signals is not None else [signal for direction in DIRECTIONS for signal in generate_signals(symbol, candles, config, direction)]
    selected_candidates = [signal for signal in all_signals if signal.trigger_time >= test_start and selected_parameters.get(signal.family, {}).get(signal.direction) == signal.parameter_key]
    selected_test = [trade.signal for trade in _trades_for(selected_candidates, candles, config, funding or {}, test_start, test_end)]
    evidence: dict[str, dict[str, Any]] = {}
    for family in FAMILIES:
        family_test = [signal for signal in selected_test if signal.family == family]
        for direction in DIRECTIONS:
            trades = _trades_for([signal for signal in family_test if signal.direction == direction], candles, config, funding or {}, test_start, test_end)
            evidence[f"{family}:{direction}"] = _evidence(trades, candles, config, symbol, family, direction)
        evidence[family] = _evidence(_trades_for(family_test, candles, config, funding or {}, test_start, test_end), candles, config, symbol, family, "both")
    return selected_test, evidence


def _regression_scenario(config: dict[str, Any], precomputed_signals: list[Signal] | None = None) -> dict[str, Any]:
    candles = research_fixture_candles("BTCUSDT")
    entry_index = 295
    signals = precomputed_signals if precomputed_signals is not None else generate_signals("BTCUSDT", candles, config, "long")
    real_signal = next(signal for signal in signals if signal.family == "range_mean_reversion" and signal.index == entry_index)
    conflict_bar = replace(candles[entry_index + 1], high=real_signal.reference_target * 1.02, low=real_signal.reference_stop * 0.98)
    fixture = candles[: entry_index + 1] + [conflict_bar] + candles[entry_index + 2 :]
    trade = simulate_signal(real_signal, fixture, config)
    if trade is None:
        raise AssertionError("real strategy regression signal did not fill")
    return {"trade_count": 1, "exit_reason": trade.reason, "net_r": trade.net_r, "costs_applied": trade.net_r < trade.gross_r, "real_family": real_signal.family, "same_bar_stop_first": trade.reason == "stop"}


def _smoke_execution_regressions(config: dict[str, Any]) -> dict[str, bool]:
    """Exercise the deterministic fill/exit/cost path used by the smoke gate."""
    interval = INTERVAL_MS
    bars = [Candle(index * interval, index * interval + interval - 1, 100, 101, 99, 100, 10, 1_000) for index in range(240)]
    long_signal = Signal("SMOKEUSDT", "trend_breakout", "long", "smoke", 0, bars[0].close_time, 100, 101, 101, 90, 89, 200, 2.5)
    filled = simulate_signal(long_signal, bars, config)
    expiry_signal = replace(long_signal, chase_boundary=1)
    expired = simulate_signal(expiry_signal, bars, config)
    gap_bar = replace(bars[2], open=80, high=100, low=70, close=75)
    gap_trade = simulate_signal(long_signal, bars[:2] + [gap_bar] + bars[3:], config)
    invalidation_bar = replace(bars[2], high=100, low=95, close=89)
    invalidation_trade = simulate_signal(long_signal, bars[:2] + [invalidation_bar] + bars[3:], config)
    funding = {bars[1].open_time + 1: 0.001, bars[2].open_time + 1: 0.0005}
    funded = simulate_signal(long_signal, bars[:12], config, funding)
    short_signal = Signal("SMOKEUSDT", "trend_breakout", "short", "smoke", 0, bars[0].close_time, 99, 100, 99, 110, 111, 1, 2.5)
    short_funded = simulate_signal(short_signal, bars[:12], config, funding)
    portfolio_signals = [replace(long_signal, symbol=f"S{index}USDT", direction="long" if index % 2 == 0 else "short", chase_boundary=101 if index % 2 == 0 else 99, technical_invalidation=90 if index % 2 == 0 else 110, reference_stop=89 if index % 2 == 0 else 111, reference_target=200 if index % 2 == 0 else 1) for index in range(8)]
    portfolio = simulate_portfolio(portfolio_signals, {signal.symbol: bars for signal in portfolio_signals}, config)
    return {
        "fill_next_open": filled is not None and filled.entry_index == 1,
        "entry_expiry": expired is None,
        "gap_beyond_stop": gap_trade is not None and gap_trade.reason == "stop" and gap_trade.exit_price < long_signal.reference_stop,
        "invalidation_exit": invalidation_trade is not None and invalidation_trade.reason == "invalidation",
        "max_hold_bound": filled is not None and filled.reason == "time" and filled.exit_time <= filled.entry_time + config["execution"]["maxHoldHours"] * 60 * 60_000,
        "funding_direction_aware": funded is not None and short_funded is not None and funded.funding_cost > 0 and short_funded.funding_cost < 0,
        "capacity_6_3_3": len(portfolio["trades"]) <= 6 and sum(trade.signal.direction == "long" for trade in portfolio["trades"]) <= 3 and sum(trade.signal.direction == "short" for trade in portfolio["trades"]) <= 3,
    }


def _chronological_drawdown(trades: list[Trade]) -> float:
    return max_drawdown_from_r([trade.net_r for trade in sorted(trades, key=lambda trade: (trade.exit_time, trade.signal.symbol, trade.signal.direction))])


def _all_signals(
    candles_by_symbol: dict[str, list[Candle]],
    config: dict[str, Any],
    operation_counts: dict[str, dict[str, int]] | None = None,
    selected_parameters: dict[str, dict[str, str]] | None = None,
    descriptor: dict[str, Any] | None = None,
) -> dict[str, list[Signal]]:
    output: dict[str, list[Signal]] = {}
    for symbol, candles in candles_by_symbol.items():
        counter: dict[str, int] = {}
        output[symbol] = []
        symbol_weights = (descriptor or {}).get("symbolWeights", {}).get(symbol, (descriptor or {}).get("weights", config["weights"]["globalBase"]))
        family_modes = (descriptor or {}).get("symbolModes", {}).get(symbol, (descriptor or {}).get("modes", {}))
        for direction in DIRECTIONS:
            direction_parameters = None
            if selected_parameters is not None:
                direction_parameters = {
                    family: values[direction]
                    for family, values in selected_parameters.items()
                    if direction in values
                }
            output[symbol].extend(generate_signals(symbol, candles, config, direction, selected_parameters=direction_parameters, operation_counter=counter, trigger_weights=symbol_weights, family_modes=family_modes))
        if operation_counts is not None:
            operation_counts[symbol] = counter
    return output


def _live_equivalent_events(
    selected_signals_by_symbol: dict[str, list[Signal]],
    candles_by_symbol: dict[str, list[Candle]],
    config: dict[str, Any],
    funding_by_symbol: dict[str, dict[int, float]],
    *,
    smoke: bool = False,
) -> tuple[list[Signal], list[dict[str, Any]]]:
    """Turn diagnostic family candidates into deployable activation events.

    The research portfolio must consume the same activation semantics as the
    live runtime: rolling quote-volume eligibility, one vote per family,
    opposing-direction suppression, one activation while a setup persists, a
    one-hour re-arm cooldown, and only then portfolio capacity.  ``smoke`` is
    the explicit offline component fixture and therefore does not pretend its
    small quote-volume values are a historical liquidity observation.
    """
    grouped: dict[tuple[str, int], dict[str, list[Signal]]] = {}
    minimum_volume = float(config["universe"]["minQuoteVolume24h"])
    for symbol, signals in selected_signals_by_symbol.items():
        candles = candles_by_symbol[symbol]
        for signal in signals:
            if not smoke:
                first = max(0, signal.index - 95)
                rolling_volume = sum(candle.quote_volume for candle in candles[first : signal.index + 1])
                if signal.index < 95 or rolling_volume < minimum_volume:
                    continue
            grouped.setdefault((symbol, signal.trigger_time), {}).setdefault(signal.direction, []).append(signal)

    activation_signals: list[Signal] = []
    activation_events: list[dict[str, Any]] = []
    stressed_double_slippage = deepcopy(config)
    stressed_double_slippage["execution"]["baseAdverseSlippageRatePerSide"] *= 2
    stressed_impact = deepcopy(config)
    stressed_impact["execution"]["baseAdverseSlippageRatePerSide"] += conservative_impact_proxy(10_000, 100_000, 0.01, config) * 1.5
    for symbol, candles in sorted(candles_by_symbol.items()):
        symbol_groups = {trigger_time: directions for (group_symbol, trigger_time), directions in grouped.items() if group_symbol == symbol}
        if not symbol_groups:
            continue
        first_time = min(symbol_groups)
        last_time = max(symbol_groups)
        timeline = sorted({candle.close_time for candle in candles if first_time <= candle.close_time <= last_time} | set(symbol_groups))
        active = {direction: False for direction in DIRECTIONS}
        cooldown_until: dict[str, int | None] = {direction: None for direction in DIRECTIONS}
        for trigger_time in timeline:
            directions = symbol_groups.get(trigger_time, {})
            conflict = len(directions) > 1
            for direction in DIRECTIONS:
                choices = directions.get(direction, [])
                if conflict or not choices:
                    if active[direction]:
                        active[direction] = False
                        cooldown_until[direction] = trigger_time + 60 * 60_000
                    continue
                # Every present 15m bar keeps the single symbol+direction
                # activation alive, even when the primary family changes.
                if active[direction]:
                    continue
                if cooldown_until[direction] is not None and trigger_time < int(cooldown_until[direction]):
                    continue
                by_family: dict[str, Signal] = {}
                for signal in choices:
                    previous = by_family.get(signal.family)
                    if previous is None or signal.trigger_score > previous.trigger_score or (signal.trigger_score == previous.trigger_score and signal.parameter_key < previous.parameter_key):
                        by_family[signal.family] = signal
                selected = sorted(by_family.values(), key=lambda signal: (-signal.trigger_score, signal.family, signal.parameter_key))
                primary_signal = selected[0]
                active[direction] = True
                families = [signal.family for signal in selected]
                # The execution plan is selected before any simulation. Trade
                # results are computed only for this already-selected primary
                # and never participate in activation ordering.
                primary_trade = simulate_signal(primary_signal, candles, config, funding_by_symbol.get(symbol, {}))
                if primary_trade is None:
                    continue
                double_trade = simulate_signal(primary_signal, candles_by_symbol[symbol], stressed_double_slippage, funding_by_symbol.get(symbol, {}))
                impact_trade = simulate_signal(primary_signal, candles_by_symbol[symbol], stressed_impact, funding_by_symbol.get(symbol, {}))
                total_weight = sum(max(float(signal.trigger_weight), 0.0001) for signal in selected)
                combined_score = min(100.0, sum(signal.trigger_score * max(float(signal.trigger_weight), 0.0001) for signal in selected) / total_weight + min(5, len(families)))
                activation_signals.append(primary_signal)
                activation_events.append({
                    "symbol": symbol,
                    "family": primary_signal.family,
                    "families": families,
                    "direction": direction,
                    "parameter_key": primary_signal.parameter_key,
                    "trigger_time": primary_signal.trigger_time,
                    "entry_time": primary_trade.entry_time,
                    "exit_time": primary_trade.exit_time,
                    "net_r": primary_trade.net_r,
                    "double_slippage_net_r": double_trade.net_r if double_trade is not None else primary_trade.net_r,
                    "impact_stress_net_r": impact_trade.net_r if impact_trade is not None else primary_trade.net_r,
                    "shadow_only": bool(getattr(primary_signal, "shadow_only", False)),
                    "activation_key": f"{symbol}:{direction}:{primary_signal.trigger_time}",
                    "state_key": f"{symbol}:{direction}",
                    "trigger_score": combined_score,
                    "weight": total_weight / len(selected),
                    "confluence": min(5, len(families)),
                })
    return activation_signals, activation_events


def _events_for_trades(trades: list[Trade]) -> list[dict[str, Any]]:
    return [{
        "symbol": trade.signal.symbol,
        "family": trade.signal.family,
        "families": [trade.signal.family],
        "direction": trade.signal.direction,
        "parameter_key": trade.signal.parameter_key,
        "trigger_time": trade.signal.trigger_time,
        "entry_time": trade.entry_time,
        "exit_time": trade.exit_time,
        "net_r": trade.net_r,
        "shadow_only": trade.shadow_only,
        "activation_key": f"{trade.signal.symbol}:{trade.signal.direction}:{trade.signal.trigger_time}",
        "state_key": f"{trade.signal.symbol}:{trade.signal.direction}",
        "confluence": 1,
    } for trade in trades]


def _validate_prepared_provenance(
    report_path: Path | None,
    input_path: Path,
    funding_path: Path,
    candles_by_symbol: dict[str, list[Candle]],
    funding_by_symbol: dict[str, dict[int, float]],
    expected_symbols: set[str] | None = None,
) -> dict[str, Any]:
    """Reject arbitrary folders at the formal research boundary."""
    if report_path is None or not report_path.is_file():
        raise ValueError("formal backtest requires --prepare-report provenance")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("prepared-data report is unreadable") from error
    if report.get("schema") != "sol-alerts-prepared-data-v1" or report.get("formal_ready") is not True:
        raise ValueError("prepared-data report is not formally ready")
    if report.get("input_root") and Path(str(report["input_root"])).name != input_path.parent.name and Path(str(report["input_root"])).name != input_path.name:
        raise ValueError("prepared-data report input root does not match backtest input")
    report_symbols = {str(symbol).upper() for symbol in report.get("symbols", [])}
    actual_symbols = set(candles_by_symbol)
    if expected_symbols is None and actual_symbols != report_symbols:
        raise ValueError("backtest symbols do not match prepared-data report")
    if expected_symbols is not None and expected_symbols != report_symbols:
        raise ValueError("prepared-data symbol expectation does not match report")
    if not actual_symbols.issubset(report_symbols):
        raise ValueError("backtest shard contains symbols outside prepared-data report")
    manifest_value = report.get("manifest")
    if not manifest_value:
        raise ValueError("prepared-data report has no manifest provenance")
    manifest_path = Path(str(manifest_value))
    if not manifest_path.is_file():
        raise ValueError("prepared universe manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_symbols = {str(row.get("symbol", "")).upper() for row in manifest.get("symbols", []) if row.get("eligible_contract")}
    if manifest_symbols != report_symbols:
        raise ValueError("prepared manifest and report symbol sets differ")
    if report.get("gaps") or report.get("funding_missing"):
        raise ValueError("prepared-data report contains gaps or missing funding")
    if int(report.get("cache_bytes", 0)) > int(report.get("cache_limit_bytes", 10_000_000_000)):
        raise ValueError("prepared-data cache exceeds its declared bound")
    if report.get("point_in_time_membership_supported") is not True:
        raise ValueError("official point-in-time membership provenance is unavailable")
    if report.get("historical_liquidity_proxy_supported") is not True:
        raise ValueError("historical spread/depth proxy provenance is unavailable")
    if report.get("liquidity_proxy", {}).get("schema") != "sol-alerts-impact-proxy-v1":
        raise ValueError("historical liquidity proxy provenance is missing")
    availability = report.get("availability_bounds")
    if not isinstance(availability, dict) or not report_symbols.issubset({str(symbol).upper() for symbol in availability}):
        raise ValueError("prepared per-symbol availability provenance is unavailable")
    start_ms = int(report.get("start_ms", 0))
    end_ms = int(report.get("end_ms", 0))
    if start_ms >= end_ms:
        raise ValueError("prepared-data report has invalid time bounds")
    for symbol, candles in candles_by_symbol.items():
        bounds = report.get("availability_bounds", {}).get(symbol) if isinstance(report.get("availability_bounds"), dict) else None
        symbol_start = int(bounds.get("start_ms", start_ms)) if isinstance(bounds, dict) else start_ms
        symbol_end = int(bounds.get("end_ms", end_ms)) if isinstance(bounds, dict) else end_ms
        first_open, last_open = canonical_closed_bounds(symbol_start, symbol_end, INTERVAL_MS)
        if (
            not candles
            or last_open < first_open
            or candles[0].open_time < first_open
            or candles[-1].close_time > symbol_end
            or any(
                candle.open_time < first_open
                or candle.open_time > last_open
                or candle.open_time % INTERVAL_MS != 0
                or candle.close_time - candle.open_time != INTERVAL_MS - 1
                or candle.close_time > symbol_end
                for candle in candles
            )
        ):
            raise ValueError(f"prepared candle bounds are outside report for {symbol}")
        if not funding_by_symbol.get(symbol):
            raise ValueError(f"prepared funding coverage missing for {symbol}")
        if any(timestamp < symbol_start or timestamp > symbol_end for timestamp in funding_by_symbol[symbol]):
            raise ValueError(f"prepared funding bounds are outside report for {symbol}")
    return report


def prepare_selection(input_path: Path, funding_path: Path, prepare_report_path: Path, output_path: Path) -> dict[str, Any]:
    config = load_config()
    all_candles = _load_inputs(input_path)
    funding_by_symbol = read_funding_rates(funding_path)
    prepared = _validate_prepared_provenance(prepare_report_path, input_path, funding_path, all_candles, funding_by_symbol)
    all_signals = _all_signals(all_candles, config)
    selected, evidence = _select_global_parameters(all_candles, all_signals, funding_by_symbol, config, int(prepared.get("selection_cutoff_ms", 0)) or None)
    result = {"schema": "sol-alerts-selection-v1", "config_version": config["configVersion"], "symbols": sorted(all_candles), "selected_parameters": selected, "selection_evidence": evidence, "selection_source": {"shards": 1, "sufficient_statistics": True, "prepare_report_sha256": hashlib.sha256(prepare_report_path.read_bytes()).hexdigest()}}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def _paired_block_rows(champion_report: dict[str, Any], challenger_report: dict[str, Any], cutoff: int, champion_hash: str, challenger_hash: str) -> list[dict[str, Any]]:
    champion_evaluation = champion_report.get("evaluation", {}) if isinstance(champion_report.get("evaluation"), dict) else {}
    challenger_evaluation = challenger_report.get("evaluation", {}) if isinstance(challenger_report.get("evaluation"), dict) else {}
    if any(int(value.get("cutoff", cutoff)) != cutoff for value in (champion_evaluation, challenger_evaluation)):
        raise ValueError("Champion and Challenger cutoff metadata differ")
    if (champion_evaluation.get("untouched_test_start"), champion_evaluation.get("untouched_test_end")) != (challenger_evaluation.get("untouched_test_start"), challenger_evaluation.get("untouched_test_end")):
        raise ValueError("Champion and Challenger untouched interval metadata differ")
    champion_blocks = {str(row.get("block_id")): float(row.get("mean_r", 0.0)) for row in champion_report.get("oos_blocks", []) if isinstance(row, dict) and row.get("block_id") is not None}
    challenger_blocks = {str(row.get("block_id")): float(row.get("mean_r", 0.0)) for row in challenger_report.get("oos_blocks", []) if isinstance(row, dict) and row.get("block_id") is not None}
    for evaluation, actual in ((champion_evaluation, champion_blocks), (challenger_evaluation, challenger_blocks)):
        declared = {str(block_id) for block_id in evaluation.get("oos_block_ids", [])}
        if declared and declared != set(actual):
            raise ValueError("reported OOS blocks do not match the declared untouched calendar universe")
    if set(champion_blocks) != set(challenger_blocks):
        raise ValueError("Champion and Challenger do not share the exact untouched calendar blocks")
    return [
        {
            "block_id": block_id,
            "cutoff": cutoff,
            "champion_descriptor_hash": champion_hash,
            "challenger_descriptor_hash": challenger_hash,
            "champion_mean_r": champion_blocks[block_id],
            "challenger_mean_r": challenger_blocks[block_id],
            "difference": challenger_blocks[block_id] - champion_blocks[block_id],
        }
        for block_id in sorted(champion_blocks)
    ]


def build_evaluation_descriptors(
    selection_path: Path,
    output_path: Path,
    *,
    champion_path: Path | None = None,
    bootstrap_champion: bool = False,
    symbols: list[str] | None = None,
) -> dict[str, Any]:
    """Freeze champion/challenger descriptors before any untouched evaluation."""
    config = load_config()
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    if selection.get("schema") != "sol-alerts-selection-v1" or selection.get("config_version") != config["configVersion"]:
        raise ValueError("selection artifact is incompatible with descriptor build")
    selected = selection.get("selected_parameters")
    if not isinstance(selected, dict):
        raise ValueError("selection artifact has no selected parameter map")
    champion: dict[str, Any] | None = None
    if champion_path is not None and champion_path.is_file():
        payload = json.loads(champion_path.read_text(encoding="utf-8"))
        champion = payload.get("descriptor") if isinstance(payload, dict) and "descriptor" in payload else payload
        if champion is not None and not validate_descriptor(config, champion):
            raise ValueError("active champion descriptor is incompatible or hash-invalid")
    if champion is None and not bootstrap_champion:
        raise ValueError("active champion descriptor is required unless explicit bootstrap is requested")
    source_id = selection.get("selection_source", {}).get("prepare_report_sha256")
    challenger = build_candidate_descriptor(config, selected, selection.get("selection_evidence", []), symbols=symbols, previous_descriptor=champion, source_report_id=source_id)
    descriptor_list = [descriptor for descriptor in (champion, challenger) if descriptor is not None]
    parameter_union = {
        family: {direction: sorted({item["selectedParameters"][family][direction] for item in descriptor_list}) for direction in DIRECTIONS}
        for family in FAMILIES
    }
    payload = {
        "schema": "sol-alerts-evaluation-descriptors-v1",
        "configVersion": config["configVersion"],
        "champion": champion,
        "challenger": challenger,
        "bootstrapChampion": bool(bootstrap_champion and champion is None),
        "selectionSource": source_id,
        "parameterUnion": parameter_union,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def run_backtest(
    input_path: Path | None = None,
    shard_index: int = 0,
    shard_count: int = 1,
    funding_path: Path | None = None,
    *,
    smoke: bool = False,
    prepare_report_path: Path | None = None,
    selection_path: Path | None = None,
    evaluation_descriptors_path: Path | None = None,
    _descriptor: dict[str, Any] | None = None,
    _descriptor_role: str | None = None,
) -> dict[str, Any]:
    if evaluation_descriptors_path is not None and _descriptor is None:
        payload = json.loads(evaluation_descriptors_path.read_text(encoding="utf-8"))
        config = load_config()
        if payload.get("schema") != "sol-alerts-evaluation-descriptors-v1" or payload.get("configVersion") != config["configVersion"]:
            raise ValueError("evaluation descriptors are incompatible")
        challenger = payload.get("challenger")
        champion = payload.get("champion")
        if not validate_descriptor(config, challenger):
            raise ValueError("challenger descriptor is missing or hash-invalid")
        if champion is not None and not validate_descriptor(config, champion):
            raise ValueError("champion descriptor is missing or hash-invalid")
        reports: dict[str, dict[str, Any]] = {}
        if champion is not None:
            reports["champion"] = run_backtest(input_path, shard_index, shard_count, funding_path, smoke=smoke, prepare_report_path=prepare_report_path, selection_path=selection_path, _descriptor=champion, _descriptor_role="champion")
        reports["challenger"] = run_backtest(input_path, shard_index, shard_count, funding_path, smoke=smoke, prepare_report_path=prepare_report_path, selection_path=selection_path, _descriptor=challenger, _descriptor_role="challenger")
        cutoff_values = {int(report.get("evaluation", {}).get("cutoff", 0)) for report in reports.values()}
        if len(cutoff_values) != 1:
            raise ValueError("champion and challenger cutoffs differ")
        cutoff = next(iter(cutoff_values))
        evaluation_values = [report.get("evaluation", {}) for report in reports.values()]
        interval_values = {(int(item.get("untouched_test_start", 0)), int(item.get("untouched_test_end", 0))) for item in evaluation_values}
        block_universes = {tuple(str(block_id) for block_id in item.get("oos_block_ids", [])) for item in evaluation_values}
        if len(interval_values) != 1 or len(block_universes) != 1 or not next(iter(block_universes), ()):
            raise ValueError("Champion and Challenger do not share the exact untouched calendar interval")
        paired = _paired_block_rows(reports["champion"], reports["challenger"], cutoff, champion["descriptorHash"], challenger["descriptorHash"]) if champion is not None else []
        untouched_start, untouched_end = next(iter(interval_values))
        block_ids = list(next(iter(block_universes)))
        return {
            "schema": "sol-alerts-replay-shard-v1",
            "config_version": config["configVersion"],
            "shard": reports["challenger"]["shard"],
            "evaluation": {
                "cutoff": cutoff,
                "champion_descriptor_hash": champion.get("descriptorHash") if champion else None,
                "challenger_descriptor_hash": challenger["descriptorHash"],
                "same_untouched_blocks": True,
                "untouched_test_start": untouched_start,
                "untouched_test_end": untouched_end,
                "oos_block_ids": block_ids,
                "parameter_union": payload.get("parameterUnion", {}),
            },
            "champion_descriptor": champion,
            "challenger_descriptor": challenger,
            "champion": reports.get("champion"),
            "challenger": reports["challenger"],
            "paired_oos_blocks": paired,
        }
    config = load_config()
    if smoke:
        if input_path is not None or funding_path is not None:
            raise ValueError("--smoke is fixture-only and cannot accept official input or funding paths")
    else:
        if input_path is None:
            raise ValueError("scheduled/formal backtest cannot fall back to deterministic fixtures")
        if funding_path is None:
            raise ValueError("funding directory/file is required for an official/formal backtest")
    prepared_symbols: set[str] | None = None
    if not smoke:
        if prepare_report_path is None or not prepare_report_path.is_file():
            raise ValueError("formal backtest requires --prepare-report provenance")
        try:
            prepared_metadata = json.loads(prepare_report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("prepared-data report is unreadable") from error
        prepared_symbols = {str(symbol).upper() for symbol in prepared_metadata.get("symbols", [])}
        if not prepared_symbols:
            raise ValueError("prepared-data report has no symbols")
        if shard_count > 1 and selection_path is None:
            raise ValueError("sharded official backtest requires the one-time global --selection artifact")
    if prepared_symbols is None:
        all_candles = _load_inputs(input_path, fixture=smoke)
    else:
        owned_symbols = set(_owned_symbols(prepared_symbols, shard_index, shard_count))
        all_candles = _load_inputs(input_path, symbols=owned_symbols)
    funding_by_symbol = {} if smoke else read_funding_rates(funding_path, set(all_candles))
    if not smoke:
        missing_funding = sorted(symbol for symbol in all_candles if not funding_by_symbol.get(symbol))
        if missing_funding:
            raise ValueError(f"official funding coverage is missing for {len(missing_funding)} symbols; refusing zero-funding substitution")
    prepared_report = {} if smoke else _validate_prepared_provenance(prepare_report_path, input_path, funding_path, all_candles, funding_by_symbol, prepared_symbols)
    symbols = _owned_symbols(prepared_symbols if prepared_symbols is not None else set(all_candles), shard_index, shard_count)
    operation_counts: dict[str, dict[str, int]] = {}
    selection_evidence: list[dict[str, Any]] = []
    if _descriptor is not None and selection_path is None:
        candidate_descriptor = _descriptor
        selected_parameters = _descriptor["selectedParameters"]
        all_signals_by_symbol = _all_signals(all_candles, config, operation_counts, selected_parameters, candidate_descriptor)
    elif selection_path is not None:
        if smoke or not selection_path.is_file():
            raise ValueError("--selection is supported only for official prepared runs")
        selection = json.loads(selection_path.read_text(encoding="utf-8"))
        if selection.get("schema") != "sol-alerts-selection-v1" or selection.get("config_version") != config["configVersion"] or set(selection.get("symbols", [])) != (prepared_symbols or set(all_candles)):
            raise ValueError("selection artifact is incompatible with prepared inputs")
        if selection.get("selection_source", {}).get("prepare_report_sha256") != hashlib.sha256(prepare_report_path.read_bytes()).hexdigest():
            raise ValueError("selection artifact provenance does not match prepared inputs")
        selected_parameters = _descriptor["selectedParameters"] if _descriptor is not None else selection["selected_parameters"]
        selection_evidence = selection.get("selection_evidence", [])
        candidate_descriptor = _descriptor or build_candidate_descriptor(config, selected_parameters, selection_evidence, symbols=sorted(prepared_symbols or set(all_candles)))
        all_signals_by_symbol = _all_signals({symbol: all_candles[symbol] for symbol in symbols}, config, operation_counts, selected_parameters, candidate_descriptor)
    else:
        all_signals_for_selection = _all_signals(all_candles, config, operation_counts)
        selected_parameters, selection_evidence = _select_global_parameters(all_candles, all_signals_for_selection, funding_by_symbol, config, int(prepared_report.get("selection_cutoff_ms", 0)) or None)
    if smoke:
        # Smoke is a component/regression fixture, not an adaptive publication
        # run.  Use the first key from each frozen grid so the fixture exercises
        # all three real family implementations deterministically while the
        # formal path above remains train/validation-only.
        keys = _parameter_keys(config)
        selected_parameters = {
            family: {direction: keys[family][0] for direction in DIRECTIONS}
            for family in FAMILIES
        }
    if _descriptor is None and selection_path is None:
        candidate_descriptor = build_candidate_descriptor(config, selected_parameters, selection_evidence, symbols=sorted(prepared_symbols or set(all_candles)))
        all_signals_by_symbol = _all_signals(all_candles, config, operation_counts, selected_parameters, candidate_descriptor)
    candles_by_symbol = {symbol: all_candles[symbol] for symbol in symbols}
    adaptive_cutoff = int(prepared_report.get("end_ms", 0)) if prepared_report else max((candles[-1].close_time for candles in candles_by_symbol.values()), default=0)
    adaptive_window_start = adaptive_cutoff - config["research"]["adaptiveLongWindowMonths"] * 30 * 86_400_000
    untouched_test_start, untouched_test_end = _common_untouched_interval(candles_by_symbol, prepared_report, config, adaptive_cutoff, adaptive_window_start)
    selected_signals_by_symbol: dict[str, list[Signal]] = {}
    evidence_rows: list[dict[str, Any]] = []
    directional_evidence_rows: list[dict[str, Any]] = []
    all_test_trades: list[Trade] = []
    all_test_signals: list[Signal] = []
    symbol_results: dict[str, dict[str, Any]] = {}
    for symbol, candles in candles_by_symbol.items():
        signals, evidence = _signals_for_test(symbol, candles, config, funding_by_symbol.get(symbol, {}), selected_parameters, all_signals_by_symbol[symbol], adaptive_window_start, adaptive_cutoff)
        selected_signals_by_symbol[symbol] = signals
        all_test_signals.extend(signals)
        symbol_trades: list[Trade] = []
        per_family: dict[str, dict[str, Any]] = {}
        for family in FAMILIES:
            row = evidence[family]
            per_family[family] = {key: value for key, value in row.items() if key not in {"symbol", "family", "direction"}}
            evidence_rows.append(row)
            _test_start, _test_end_start, test_start, test_end = _split_times(candles, config, adaptive_window_start, adaptive_cutoff)
            family_trades = _trades_for([signal for signal in signals if signal.family == family], candles, config, funding_by_symbol.get(symbol, {}), test_start, test_end)
            symbol_trades.extend(family_trades)
            directional_evidence_rows.extend(evidence[f"{family}:{direction}"] for direction in DIRECTIONS)
        all_test_trades.extend(symbol_trades)
        symbol_results[symbol] = {
            "history_days": (candles[-1].close_time - candles[0].open_time) / 86_400_000,
            "train_bars": int(len(candles) * config["research"]["trainFraction"]),
            "validation_bars": int(len(candles) * config["research"]["validationFraction"]),
            "untouched_test_bars": len(candles) - int(len(candles) * config["research"]["trainFraction"]) - int(len(candles) * config["research"]["validationFraction"]),
            "signals": len(signals),
            "trades": len(symbol_trades),
            "max_drawdown": _chronological_drawdown(symbol_trades),
            "families": per_family,
            "formal_eligible": any(per_family[family]["formal_eligible"] for family in FAMILIES),
        }

    evidence_rows_by_key = {(row["symbol"], row["family"], row["direction"]): row for row in directional_evidence_rows}
    if not smoke:
        # Weak symbol/family evidence remains observable shadow research and
        # cannot consume formal capacity or make the whole market halt.
        selected_signals_by_symbol = {
            symbol: [replace(signal, shadow_only=not evidence_rows_by_key.get((symbol, signal.family, signal.direction), {}).get("formal_eligible", False)) for signal in signals]
            for symbol, signals in selected_signals_by_symbol.items()
        }
    live_signals, live_events = _live_equivalent_events(selected_signals_by_symbol, candles_by_symbol, config, funding_by_symbol, smoke=smoke)
    portfolio = simulate_portfolio(live_signals, candles_by_symbol, config, funding_by_symbol=funding_by_symbol)

    family_results: dict[str, dict[str, Any]] = {}
    for family in FAMILIES:
        family_trades = [trade for trade in portfolio["trades"] if trade.signal.family == family]
        values = [trade.net_r for trade in sorted(family_trades, key=lambda trade: trade.exit_time)]
        lower = block_bootstrap_lower_bound(values, config["research"]["randomSeed"], min(2_000, config["research"]["bootstrapReplicates"])) if values else 0.0
        family_results[family] = {
            "trades": len(values),
            "mean_r": _mean_net_r(family_trades),
            "max_drawdown": _chronological_drawdown(family_trades),
            "symbols": len({trade.signal.symbol for trade in family_trades}),
            "lower_bound_95": lower,
            "formal_eligible": len(values) >= config["research"]["minimumTestTradesPerFamily"] and lower > 0 and _chronological_drawdown(family_trades) <= config["research"]["maxPortfolioDrawdown"],
        }

    regression = {**_regression_scenario(config, all_signals_by_symbol.get("BTCUSDT")), **_smoke_execution_regressions(config)}
    contribution = [sum(trade.net_r for trade in portfolio["trades"] if trade.signal.symbol == symbol) for symbol in symbols]
    evidence_by_family_symbol = {(row["symbol"], row["family"]): row for row in evidence_rows}
    score_bounds = all(0 <= _score(signal, evidence_by_family_symbol.get((signal.symbol, signal.family), {}), config) <= 100 for signal in all_test_signals)
    lookahead_checks: list[bool] = []
    for symbol, candles in candles_by_symbol.items():
        cut = max(100, int(len(candles) * 0.65))
        prefix = {(signal.index, signal.family, signal.direction, signal.parameter_key) for direction in DIRECTIONS for signal in generate_signals(symbol, candles[:cut], config, direction)}
        full = {(signal.index, signal.family, signal.direction, signal.parameter_key) for direction in DIRECTIONS for signal in generate_signals(symbol, candles, config, direction) if signal.index < cut}
        lookahead_checks.append(prefix == full)
    family_trade_counts = {family: sum(1 for trade in portfolio["trades"] if trade.signal.family == family) for family in FAMILIES}
    stressed_double_slippage = deepcopy(config)
    stressed_double_slippage["execution"]["baseAdverseSlippageRatePerSide"] *= 2
    stressed_impact = deepcopy(config)
    stressed_impact["execution"]["baseAdverseSlippageRatePerSide"] += conservative_impact_proxy(10_000, 100_000, 0.01, config) * 1.5
    double_slippage_portfolio = simulate_portfolio(live_signals, candles_by_symbol, stressed_double_slippage, funding_by_symbol=funding_by_symbol)
    impact_stress_portfolio = simulate_portfolio(live_signals, candles_by_symbol, stressed_impact, funding_by_symbol=funding_by_symbol)
    base_mean_r = _mean_net_r(portfolio["trades"])
    double_slippage_mean_r = _mean_net_r(double_slippage_portfolio["trades"])
    impact_stress_mean_r = _mean_net_r(impact_stress_portfolio["trades"])
    cutoff = adaptive_cutoff
    long_window_start = cutoff - config["research"]["adaptiveLongWindowMonths"] * 30 * 86_400_000
    recent_window_start = cutoff - config["research"]["adaptiveRecentWindowDays"] * 86_400_000
    accepted_trades = list(portfolio["trades"])
    long_window_trades = [trade for trade in accepted_trades if trade.exit_time >= long_window_start]
    recent_trades = [trade for trade in accepted_trades if trade.exit_time >= recent_window_start]
    hard_gate_checks = [bool(row["formal_eligible"]) == bool(row["history_days"] >= config["universe"]["minFormalHistoryDays"] and row["untouched_test_trades"] >= config["research"]["minimumTestTradesPerSymbolStrategy"] and row["mean_r_lower_bound_95"] > 0 and row["untouched_max_drawdown"] <= config["research"]["maxPortfolioDrawdown"]) for row in evidence_rows]
    official_funding_ready = smoke or all(bool(funding_by_symbol.get(symbol)) for symbol in symbols)
    concentration_pass = concentration_share(contribution) < 0.5
    cost_pass = all(mean_value > 0 for mean_value in (base_mean_r, double_slippage_mean_r, impact_stress_mean_r)) if portfolio["trades"] else False
    any_pair_formal = any(row["formal_eligible"] for row in evidence_rows)
    any_family_formal = any(row["formal_eligible"] for row in family_results.values())
    historical_proxy_ready = smoke or bool(prepared_report.get("historical_liquidity_proxy_supported"))
    formal_alerts_available = any_pair_formal and any_family_formal and portfolio["max_drawdown"] <= config["research"]["maxPortfolioDrawdown"] and concentration_pass and cost_pass and official_funding_ready and historical_proxy_ready
    # ``candidate_events`` is the capacity-accepted deployed portfolio. Raw
    # live-equivalent activations remain diagnostic and are used by the merge
    # job to apply capacity exactly once across shards.
    candidate_events = _events_for_trades(accepted_trades)
    parameter_events: list[dict[str, Any]] = []
    for symbol, signals in selected_signals_by_symbol.items():
        _selection_start, _selection_validation, test_start, test_end = _split_times(candles_by_symbol[symbol], config, adaptive_window_start, adaptive_cutoff)
        for signal in signals:
            for trade in _trades_for([signal], candles_by_symbol[symbol], config, funding_by_symbol.get(symbol, {}), test_start, test_end):
                parameter_events.append({"symbol": symbol, "family": signal.family, "direction": signal.direction, "parameter_key": signal.parameter_key, "trigger_time": signal.trigger_time, "entry_time": trade.entry_time, "exit_time": trade.exit_time, "net_r": trade.net_r})
    oos_blocks = _calendar_oos_blocks(candidate_events, untouched_test_start, untouched_test_end)
    oos_block_ids = [row["block_id"] for row in oos_blocks]
    report: dict[str, Any] = {
        "report_version": "1.2.0",
        "config_version": config["configVersion"],
        "candidate_descriptor": candidate_descriptor,
        "descriptorHash": candidate_descriptor["descriptorHash"],
        "evaluation": {"role": _descriptor_role or "candidate", "cutoff": adaptive_cutoff, "same_untouched_blocks": True, "untouched_test_start": untouched_test_start, "untouched_test_end": untouched_test_end, "oos_block_ids": oos_block_ids},
        "shard": {"index": shard_index, "count": shard_count, "symbols": symbols},
        "primary_metric": "untouched_test_mean_R_after_costs",
        "split": {"train": 0.6, "validation": 0.2, "untouched_test": 0.2, "ordered": True, "random_shuffle": False, "selection_scope": "global family-direction fixed key"},
        "walk_forward": {"mode": "ordered_60_20_20", "selection": "one global parameter key per family-direction selected from aggregated train/validation only", "selected_parameters": selected_parameters, "selection_evidence": selection_evidence if shard_index == 0 else [], "selection_owner": shard_index == 0, "market_state_adx_grid": [{"trend_threshold": trend, "range_threshold": range} for trend in config["marketState"]["trendThresholds"] for range in config["marketState"]["rangeThresholds"]], "strategy_parameter_grids": "config/strategy-v1.json only", "untouched_test_used_for_reporting": True, "adaptive_windows": {"long_months": config["research"]["adaptiveLongWindowMonths"], "recent_days": config["research"]["adaptiveRecentWindowDays"], "older_data": "stress_only", "window_cutoff_time": cutoff}},
        "data_quality": {"input_source": "deterministic_fixture" if smoke else str(input_path), "raw_history_stored_in_report": False, "gap_count": sum(_gap_count(candles) for candles in candles_by_symbol.values()), "missing_data_gaps_reported": True, "survivorship_bias_warning": "official manifests include active and historically observed contracts with per-symbol availability bounds; a current snapshot alone is not point-in-time coverage", "funding_coverage_symbols": sorted(symbol for symbol in symbols if funding_by_symbol.get(symbol)), "funding_missing_symbols": sorted(symbol for symbol in symbols if not funding_by_symbol.get(symbol)), "point_in_time_membership_supported": bool(prepared_report.get("point_in_time_membership_supported", smoke)), "prepare_report_validated": bool(prepared_report) if not smoke else True},
        "liquidity_and_costs": {"volume_proxy": "rolling 24h quote volume plus conservative volatility/order-size impact proxy", "historical_spread_depth_proxy": "spread/depth are unavailable in official historical klines; formal runs require a prepared conservative proxy provenance and fail closed otherwise; live scans use actual bid/ask and depth", "historical_liquidity_proxy_supported": historical_proxy_ready, "historical_liquidity_proxy_schema": "sol-alerts-impact-proxy-v1", "reference_max_order_notional_usdt": config["universe"]["referenceMaxOrderNotionalUsdt"], "base_fee_per_side": config["execution"]["takerFeeRatePerSide"], "base_slippage_per_side": config["execution"]["baseAdverseSlippageRatePerSide"], "funding_source": "fixture-only zero funding" if smoke else str(funding_path), "impact_proxy_fixture_rate": conservative_impact_proxy(10_000, 100_000, 0.01, config), "stress_scenarios": ["base", "double_slippage", "1.5x_proxy_impact"]},
        "evidence": {"rows": evidence_rows, "directional_rows": directional_evidence_rows, "usable_by_runtime": True, "per_symbol_family_direction": True},
        "candidate_events": candidate_events,
        "live_events": live_events,
        "portfolio_events": candidate_events,
        "parameter_events": parameter_events,
        "oos_blocks": oos_blocks,
        "per_symbol": symbol_results,
        "per_family": family_results,
        "long_short": {direction: {"trades": sum(1 for trade in all_test_trades if trade.signal.direction == direction), "mean_r": _mean_net_r([trade for trade in all_test_trades if trade.signal.direction == direction])} for direction in DIRECTIONS},
        "portfolio": {"starting_equity": portfolio["starting_equity"], "ending_equity": portfolio["ending_equity"], "trades": len(portfolio["trades"]), "shadow_trades": len(portfolio["shadow_trades"]), "max_drawdown": portfolio["max_drawdown"], "max_open_positions": config["execution"]["maxOpenPositions"], "max_long_positions": config["execution"]["maxLongPositions"], "max_short_positions": config["execution"]["maxShortPositions"], "strategy_family_trades": family_trade_counts, "trade_returns": [{"symbol": trade.signal.symbol, "family": trade.signal.family, "direction": trade.signal.direction, "entry_time": trade.entry_time, "exit_time": trade.exit_time, "net_r": trade.net_r} for trade in portfolio["trades"]]},
        "concentration": {"symbol_contribution_share": concentration_share(contribution), "contributions": dict(zip(symbols, contribution)), "concentration_pass": concentration_pass},
        "cost_sensitivity": {"base_mean_r": base_mean_r, "double_slippage_mean_r": double_slippage_mean_r, "impact_stress_mean_r": impact_stress_mean_r, "all_stress_scenarios_positive": cost_pass},
        "adaptive_evidence": {"long_window_trades": len(long_window_trades), "long_window_mean_r": _mean_net_r(long_window_trades), "recent_window_trades": len(recent_trades), "recent_window_mean_r": _mean_net_r(recent_trades), "older_data": "stress_only", "long_window_start": long_window_start, "recent_window_start": recent_window_start},
        "performance": {"operation_counts": operation_counts, "aggregate_1h_calls": sum(item.get("aggregate_1h_calls", 0) for item in operation_counts.values()), "bar_iterations": sum(item.get("bar_iterations", 0) for item in operation_counts.values()), "selection_is_shard_owned": selection_path is not None},
        "regression_checks": regression,
        "gates": {"lookahead_check": all(lookahead_checks), "lookahead_checks_by_symbol": dict(zip(candles_by_symbol, lookahead_checks)), "global_selection_no_lookahead": all(item["parameter_key"] in _parameter_keys(config)[item["family"]] for item in selection_evidence), "same_bar_stop_first": regression["exit_reason"] == "stop", "score_bounds_check": score_bounds, "hard_gates_override_score": all(hard_gate_checks), "hard_gate_checks": hard_gate_checks, "formal_alerts_available": formal_alerts_available, "formal_gate_reason": "any valid symbol-family and family combination may qualify; weak pairs remain shadow-only"},
        "recent": {"mean_r": _mean_net_r(recent_trades), "trades": len(recent_trades)},
        "paired_oos_blocks": [],
        "oos_block_ids": oos_block_ids,
    }
    return report


def validate_smoke(report: dict[str, Any]) -> None:
    assert report["split"]["ordered"] and report["split"]["random_shuffle"] is False
    assert report["data_quality"]["raw_history_stored_in_report"] is False
    assert report["gates"]["lookahead_check"] is True
    assert report["gates"]["same_bar_stop_first"] is True
    assert report["regression_checks"]["trade_count"] == 1
    assert report["regression_checks"]["costs_applied"] is True
    assert all(report["regression_checks"][key] is True for key in ("fill_next_open", "entry_expiry", "gap_beyond_stop", "invalidation_exit", "max_hold_bound", "funding_direction_aware", "capacity_6_3_3"))
    assert report["gates"]["score_bounds_check"] is True
    assert report["portfolio"]["trades"] > 0
    assert all(report["portfolio"]["strategy_family_trades"][family] > 0 for family in FAMILIES)
    assert report["portfolio"]["max_open_positions"] <= 6
    assert report["portfolio"]["max_long_positions"] <= 3
    assert report["portfolio"]["max_short_positions"] <= 3
    assert report["cost_sensitivity"]["double_slippage_mean_r"] <= report["cost_sensitivity"]["base_mean_r"] + 1e-12
    assert report["walk_forward"]["selection"].startswith("one global parameter key")


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline deterministic Sol Alerts ordered walk-forward backtest")
    parser.add_argument("--smoke", action="store_true", help="run the fixture-backed deterministic verification; official inputs are rejected")
    parser.add_argument("--input", type=Path, help="directory containing official CSV or Binance JSONL candle files")
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--output", type=Path, default=Path("reports/backtest-report.json"))
    parser.add_argument("--funding", type=Path, help="directory or CSV with symbol,funding_time,funding_rate; mandatory for official input")
    parser.add_argument("--prepare-report", type=Path, help="official prepare-data provenance report; mandatory outside --smoke")
    parser.add_argument("--selection", type=Path, help="global selection artifact produced by --selection-only")
    parser.add_argument("--selection-only", action="store_true", help="prepare one global train/validation selection artifact")
    parser.add_argument("--selection-shard-index", type=int, help="selection-map shard index; each shard owns disjoint symbols")
    parser.add_argument("--selection-shard-count", type=int, default=4, help="selection-map shard count")
    parser.add_argument("--merge-selection", action="store_true", help="merge compact selection sufficient-statistic artifacts")
    parser.add_argument("--selection-evidence", type=Path, nargs="+", help="selection-map evidence files for --merge-selection")
    parser.add_argument("--build-evaluation-descriptors", action="store_true", help="freeze champion/challenger descriptors before untouched evaluation")
    parser.add_argument("--champion-descriptor", type=Path, help="protected, non-secret active champion descriptor")
    parser.add_argument("--bootstrap-champion", action="store_true", help="explicitly allow a no-champion bootstrap descriptor")
    parser.add_argument("--evaluation-descriptors", type=Path, help="frozen champion/challenger descriptors for identical OOS replay")
    args = parser.parse_args()
    if args.shard_index < 0 or args.shard_count <= 0 or args.shard_index >= args.shard_count:
        parser.error("invalid shard values")
    if args.selection_shard_index is not None and (args.selection_shard_index < 0 or args.selection_shard_count <= 0 or args.selection_shard_index >= args.selection_shard_count):
        parser.error("invalid selection shard values")
    if args.build_evaluation_descriptors:
        if args.selection is None:
            parser.error("--build-evaluation-descriptors requires --selection")
        result = build_evaluation_descriptors(args.selection, args.output, champion_path=args.champion_descriptor, bootstrap_champion=args.bootstrap_champion)
        print(json.dumps({"ok": True, "build_evaluation_descriptors": True, "champion": result["champion"] is not None, "challenger_descriptor_hash": result["challenger"]["descriptorHash"]}, sort_keys=True))
        return 0
    if args.merge_selection:
        if args.smoke or args.prepare_report is None or not args.selection_evidence:
            parser.error("--merge-selection requires official --prepare-report and --selection-evidence files")
        result = merge_selection_files(args.selection_evidence, args.prepare_report, args.output, args.selection_shard_count)
        print(json.dumps({"ok": True, "merge_selection": True, "symbols": len(result["symbols"]), "selected_parameters": result["selected_parameters"]}, sort_keys=True))
        return 0
    if args.selection_only:
        if args.smoke or args.input is None or args.funding is None or args.prepare_report is None:
            parser.error("--selection-only requires official --input, --funding and --prepare-report")
        if args.selection_shard_index is None:
            result = prepare_selection(args.input, args.funding, args.prepare_report, args.output)
            print(json.dumps({"ok": True, "selection_only": True, "symbols": len(result["symbols"]), "selected_parameters": result["selected_parameters"]}, sort_keys=True))
        else:
            result = prepare_selection_shard(args.input, args.funding, args.prepare_report, args.output, args.selection_shard_index, args.selection_shard_count)
            print(json.dumps({"ok": True, "selection_only": True, "selection_shard": result["selection_shard"], "statistics": len(result["statistics"])}, sort_keys=True))
        return 0
    report = run_backtest(args.input, args.shard_index, args.shard_count, args.funding, smoke=args.smoke, prepare_report_path=args.prepare_report, selection_path=args.selection, evaluation_descriptors_path=args.evaluation_descriptors)
    if args.smoke:
        validate_smoke(report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print(json.dumps({"ok": True, "smoke": args.smoke, "symbols": report["shard"]["symbols"], "trades": report["portfolio"]["trades"], "family_trades": report["portfolio"]["strategy_family_trades"], "max_drawdown": report["portfolio"]["max_drawdown"], "formal_alerts_available": report["gates"]["formal_alerts_available"], "regression_exit_reason": report["regression_checks"]["exit_reason"], "regression_costs_applied": report["regression_checks"]["costs_applied"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
