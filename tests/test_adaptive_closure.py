from __future__ import annotations

import csv
import gzip
import json
from dataclasses import replace
from pathlib import Path

import pytest

from sol_alerts.backtest import _all_signals, _live_equivalent_events, _load_inputs, _owned_symbols, _parameter_keys, _select_global_parameters, _selection_statistics, merge_selection_evidence, run_backtest
from sol_alerts.config import load_config
from sol_alerts.data import Candle, read_funding_rates
from sol_alerts.portfolio import simulate_signal
from sol_alerts.publish import aggregate_reports, prepare_publication
from sol_alerts.prepare_data import _availability_bounds
from sol_alerts.strategies import Signal
from sol_alerts.strategies import generate_signals


def _bars(count: int = 12) -> list[Candle]:
    return [Candle(index * 900_000, index * 900_000 + 899_999, 100, 101, 99, 100, 10, 1_000) for index in range(count)]


def _signal(direction: str = "long", index: int = 0) -> Signal:
    return Signal("TESTUSDT", "trend_breakout", direction, "fixture", index, index * 900_000 + 899_999, 99, 101, 101 if direction == "long" else 99, 98 if direction == "long" else 102, 97 if direction == "long" else 103, 200 if direction == "long" else 1, 2.5)


def test_formal_backtest_requires_explicit_official_inputs() -> None:
    with pytest.raises(ValueError, match="cannot fall back"):
        run_backtest()
    with pytest.raises(ValueError, match="fixture-only"):
        run_backtest(Path("fixtures"), smoke=True)


def test_funding_directory_is_read_and_direction_aware(tmp_path: Path) -> None:
    config = load_config()
    funding_file = tmp_path / "TESTUSDT.csv"
    funding_file.write_text("symbol,funding_time,funding_rate\nTESTUSDT,1800001,0.001\nTESTUSDT,2700001,0.0005\n", encoding="utf-8")
    assert read_funding_rates(tmp_path)["TESTUSDT"][1_800_001] == 0.001
    bars = _bars()
    long_trade = simulate_signal(_signal("long"), bars, config, {1_800_001: 0.001, 2_700_001: 0.0005})
    short_trade = simulate_signal(_signal("short"), bars, config, {1_800_001: 0.001, 2_700_001: 0.0005})
    assert long_trade is not None and short_trade is not None
    assert long_trade.funding_cost > 0 and short_trade.funding_cost < 0
    assert long_trade.funding_cost == -short_trade.funding_cost


def test_downloader_formats_and_availability_bounds_are_point_in_time_aware(tmp_path: Path) -> None:
    candle_file = tmp_path / "TESTUSDT.csv"
    candle_file.write_text("open_time,close_time,open,high,low,close,volume,quote_volume\n1,899999,100,101,99,100,1,1000\n900000,1799999,100,101,99,100,1,1000\n", encoding="utf-8")
    from sol_alerts.data import read_candles
    assert len(read_candles(candle_file)) == 2
    compressed = tmp_path / "TESTUSDT.csv.gz"
    with gzip.open(compressed, "wt", encoding="utf-8") as handle:
        handle.write(candle_file.read_text(encoding="utf-8"))
    assert len(read_candles(compressed)) == 2
    row = {"last_status": "TRADING", "observations": [{"observed_at": 900_000, "status": "TRADING", "onboard_date": 100}]}
    assert _availability_bounds(row, 0, 2_000_000) == (100, 900_000)


def test_delayed_entry_and_close_invalidation_are_timestamp_based() -> None:
    config = load_config()
    bars = _bars()
    delayed = simulate_signal(_signal("long"), [bars[0], Candle(bars[1].open_time, bars[1].close_time, 102, 103, 101, 102, 10, 1_000), Candle(bars[2].open_time, bars[2].close_time, 100, 101, 99, 100, 10, 1_000)] , config)
    assert delayed is not None
    assert delayed.entry_index == 2
    invalidation_bars = bars[:2] + [Candle(bars[2].open_time, bars[2].close_time, 100, 101, 99, 97, 10, 1_000)] + bars[3:]
    invalidated = simulate_signal(_signal("long"), invalidation_bars, config)
    assert invalidated is not None
    assert invalidated.reason == "invalidation"


def test_max_hold_exits_on_last_allowed_closed_bar() -> None:
    config = load_config()
    bars = _bars(240)
    trade = simulate_signal(_signal("long"), bars, config)
    assert trade is not None
    assert trade.reason == "time"
    assert trade.exit_time <= trade.entry_time + config["execution"]["maxHoldHours"] * 60 * 60_000


def test_live_equivalent_events_combine_families_and_rearm_after_cooldown() -> None:
    config = load_config()
    bars = _bars(12)
    first = _signal("long", 0)
    second = replace(first, family="trend_pullback", parameter_key="pullback", index=0)
    sustained = replace(first, index=1, trigger_time=bars[1].close_time)
    rearmed = replace(first, index=6, trigger_time=bars[6].close_time)
    signals, events = _live_equivalent_events({"TESTUSDT": [first, second, sustained, rearmed]}, {"TESTUSDT": bars}, config, {}, smoke=True)
    assert len(signals) == 2
    assert len(events) == 2
    assert events[0]["confluence"] == 2
    assert events[1]["activation_key"].endswith(str(rearmed.trigger_time))


def test_live_equivalent_primary_uses_trigger_priority_not_realized_trade() -> None:
    config = load_config()
    bars = _bars(12)
    high_priority = replace(_signal("long", 0), family="trend_breakout", trigger_score=99.0, trigger_weight=0.34, reference_stop=99.5, technical_invalidation=98, reference_target=200)
    low_priority = replace(_signal("long", 0), family="trend_pullback", trigger_score=10.0, trigger_weight=0.34, reference_stop=90, technical_invalidation=80, reference_target=101)
    high_trade = simulate_signal(high_priority, bars, config)
    low_trade = simulate_signal(low_priority, bars, config)
    assert high_trade is not None and low_trade is not None
    assert high_trade.net_r < low_trade.net_r
    signals, events = _live_equivalent_events({"TESTUSDT": [high_priority, low_priority]}, {"TESTUSDT": bars}, config, {}, smoke=True)
    assert len(signals) == 1 and len(events) == 1
    assert signals[0].family == "trend_breakout"
    assert signals[0].reference_target == 200
    assert events[0]["family"] == "trend_breakout"


def test_live_equivalent_state_is_continuous_and_cooldown_is_from_disappearance() -> None:
    config = load_config()
    bars = _bars(20)

    continuous = [replace(_signal("long", index), index=index, trigger_time=bars[index].close_time, family="trend_breakout" if index % 2 == 0 else "trend_pullback") for index in range(9)]
    signals, _events = _live_equivalent_events({"TESTUSDT": continuous}, {"TESTUSDT": bars}, config, {}, smoke=True)
    assert len(signals) == 1

    early_rearm = [replace(_signal("long", index), index=index, trigger_time=bars[index].close_time) for index in (0, 1, 5)]
    early_signals, _early_events = _live_equivalent_events({"TESTUSDT": early_rearm}, {"TESTUSDT": bars}, config, {}, smoke=True)
    assert len(early_signals) == 1

    late_rearm = [replace(_signal("long", index), index=index, trigger_time=bars[index].close_time) for index in (0, 1, 6)]
    late_signals, _late_events = _live_equivalent_events({"TESTUSDT": late_rearm}, {"TESTUSDT": bars}, config, {}, smoke=True)
    assert len(late_signals) == 2


def test_publication_rejects_unpaired_raw_blocks_in_current_reports() -> None:
    config = load_config()
    report = {"report_version": "1.2.0", "gates": {"formal_alerts_available": True}, "per_family": {family: {"formal_eligible": True, "mean_r": 0.2} for family in ("trend_breakout", "trend_pullback", "range_mean_reversion")}, "paired_oos_blocks": [0.5] * 40, "portfolio": {"max_drawdown": 0.02}, "recent": {"mean_r": 0.2}}
    champion = {**report, "paired_oos_blocks": [0.0] * 40, "per_family": {family: {"formal_eligible": True, "mean_r": 0.3} for family in ("trend_breakout", "trend_pullback", "range_mean_reversion")}}
    publication = prepare_publication(report, config, champion_report=champion)
    assert publication["published"] is False
    assert publication["decision"]["pairedLowerBound95"] == 0


def test_initial_champion_requires_explicit_bootstrap_flag() -> None:
    config = load_config()
    selected = {family: {"long": "fixture", "short": "fixture"} for family in ("trend_breakout", "trend_pullback", "range_mean_reversion")}
    selected["trend_breakout"] = {"long": "lookback=20;buffer=0.1;volume=1.25", "short": "lookback=20;buffer=0.1;volume=1.25"}
    selected["trend_pullback"] = {"long": "ema=20;structure=40", "short": "ema=20;structure=40"}
    selected["range_mean_reversion"] = {"long": "bb=2;rsi=30/70", "short": "bb=2;rsi=30/70"}
    report = {"report_version": "1.2.0", "walk_forward": {"selected_parameters": selected}, "gates": {"formal_alerts_available": True}, "per_family": {family: {"formal_eligible": True, "mean_r": 0.2, "trades": 400} for family in ("trend_breakout", "trend_pullback", "range_mean_reversion")}, "portfolio": {"max_drawdown": 0.02}, "recent": {"mean_r": 0.2}, "concentration": {"contributions": {"A": 1, "B": 1, "C": 1}, "concentration_pass": True}, "cost_sensitivity": {"all_stress_scenarios_positive": True}, "data_quality": {"prepare_report_validated": True, "point_in_time_membership_supported": True}, "liquidity_and_costs": {"historical_liquidity_proxy_supported": True, "historical_liquidity_proxy_schema": "sol-alerts-impact-proxy-v1"}}
    assert prepare_publication(report, config)["published"] is False
    assert prepare_publication(report, config, bootstrap_champion=True)["published"] is True


def test_workflow_forces_official_input_and_funding_paths() -> None:
    workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "research.yml").read_text(encoding="utf-8")
    assert "sol_alerts.prepare_data" in workflow
    assert "--input .cache/market-data/klines" in workflow
    assert "--funding .cache/market-data/funding" in workflow
    assert "--selection-only" in workflow
    assert "--selection-shard-index" in workflow
    assert "selection_shard: [0, 1, 2, 3]" in workflow
    assert "--merge-selection" in workflow
    assert "--selection-evidence reports/selection-evidence/*.json" in workflow
    assert "formal_ready: ${{ steps.prepare.outputs.formal_ready }}" in workflow
    assert "cache_key: ${{ steps.cache_keys.outputs.save_key }}" in workflow
    assert "if: needs.prepare-data.outputs.formal_ready == 'true'" in workflow
    assert "market-data-${{ runner.os }}-rolling-v1-" in workflow
    assert "actions/cache/restore@6849a6489940f00c2f30c0fb92c6274307ccb58a" in workflow
    assert "actions/cache/save@6849a6489940f00c2f30c0fb92c6274307ccb58a" in workflow
    assert "${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}" in workflow
    assert workflow.count("key: ${{ needs.prepare-data.outputs.cache_key }}") == 3
    assert workflow.count("fail-on-cache-miss: true") == 3
    assert "uses: actions/cache@6849a6489940f00c2f30c0fb92c6274307ccb58a" not in workflow
    assert "key: market-data-${{ runner.os }}-rolling-v1-${{ steps.cache-key.outputs.week }}" not in workflow
    assert "--selection \"$RUNNER_TEMP/selection.json\"" in workflow
    assert "--bootstrap-champion" in workflow
    assert "production_evaluation" in workflow
    assert "build-descriptors" in workflow
    assert "--fetch-champion --publish" in workflow
    assert "--build-evaluation-descriptors" in workflow
    assert "--evaluation-descriptors \"$RUNNER_TEMP/evaluation-descriptors.json\"" in workflow
    assert "needs.build-descriptors.result == 'success'" in workflow
    assert workflow.count("SUPABASE_SERVICE_ROLE_KEY") == 4
    push_only = workflow.split("  build-descriptors:", 1)[0]
    assert "SUPABASE_SERVICE_ROLE_KEY" not in push_only
    assert "github.event_name == 'schedule' || (github.event_name == 'workflow_dispatch' && (inputs.production_evaluation == true || inputs.bootstrap_champion == true))" in workflow
    assert "python -m sol_alerts.backtest --smoke" not in workflow


def test_signal_generation_precomputes_hour_aggregates_once_per_direction() -> None:
    config = load_config()
    candles = _bars(320)
    counter: dict[str, int] = {}
    generate_signals("TESTUSDT", candles, config, "long", operation_counter=counter)
    assert counter["aggregate_1h_calls"] == 1
    assert counter["bar_iterations"] <= len(candles)


def test_signal_generation_work_is_near_linear_and_bounded_for_multiple_symbols() -> None:
    config = load_config()

    def work(count: int) -> int:
        counter: dict[str, int] = {}
        generate_signals("TESTUSDT", _bars(count), config, "long", operation_counter=counter)
        return counter["bounded_work_units"]

    smaller = work(1_280)
    larger = work(2_560)
    assert smaller > 0
    assert larger / smaller <= 2.3

    total = 0
    for symbol in ("AUSDT", "BUSDT", "CUSDT"):
        candles = _bars(640)
        for direction in ("long", "short"):
            counter: dict[str, int] = {}
            generate_signals(symbol, candles, config, direction, operation_counter=counter)
            assert counter["state_window_bars"] <= 75 * counter["bar_iterations"]
            total += counter["bounded_work_units"]
    assert total <= 15_000_000


def test_selected_shard_generation_emits_only_the_global_parameter_key() -> None:
    config = load_config()
    keys = _parameter_keys(config)
    selected = {family: {direction: values[0] for direction in ("long", "short")} for family, values in keys.items()}
    signals = _all_signals({"TESTUSDT": _bars(640)}, config, selected_parameters=selected)["TESTUSDT"]
    assert all(signal.parameter_key == selected[signal.family][signal.direction] for signal in signals)


def test_selection_shard_merge_matches_monolithic_and_artifacts_are_sufficient_statistics() -> None:
    config = load_config()
    from sol_alerts.fixtures import synthetic_candles

    candles = {f"S{index}USDT": synthetic_candles(f"S{index}USDT", 480 if index == 0 else 640) for index in range(4)}
    all_signals = _all_signals(candles, config)
    monolithic_selected, monolithic_evidence = _select_global_parameters(candles, all_signals, {}, config)
    global_cutoff = max(candle[-1].close_time for candle in candles.values())
    shard_reports: list[dict[str, object]] = []
    for index in range(4):
        owned = sorted(candles)[index::4]
        assert len(owned) <= (len(candles) + 3) // 4
        stats = _selection_statistics({symbol: candles[symbol] for symbol in owned}, {symbol: all_signals[symbol] for symbol in owned}, {}, config, global_cutoff)
        shard_reports.append({
            "schema": "sol-alerts-selection-evidence-v1",
            "config_version": config["configVersion"],
            "selection_shard": {"index": index, "count": 4, "symbols": owned},
            "symbols": owned,
            "prepare_report_sha256": "fixture-provenance",
            "statistics": [{"family": family, "direction": direction, "parameter_key": key, **values} for (family, direction, key), values in sorted(stats.items())],
        })
    assert all(set(shard["symbols"]).isdisjoint(set(other["symbols"])) for index, shard in enumerate(shard_reports) for other in shard_reports[index + 1:])
    merged = merge_selection_evidence(shard_reports, set(candles), config, 4, "fixture-provenance")
    assert merged["selected_parameters"] == monolithic_selected
    assert merged["selection_evidence"] == monolithic_evidence
    encoded = json.dumps(shard_reports[0], sort_keys=True)
    assert "signals" not in encoded and "entry_time" not in encoded and "net_r" not in encoded and len(encoded) < 50_000

    overlap = [dict(report) for report in shard_reports]
    overlap[1] = {**overlap[1], "symbols": overlap[0]["symbols"], "selection_shard": {**overlap[1]["selection_shard"], "symbols": overlap[0]["symbols"]}}
    with pytest.raises(ValueError, match="overlaps"):
        merge_selection_evidence(overlap, set(candles), config, 4, "fixture-provenance")
    with pytest.raises(ValueError, match="shard count is incomplete"):
        merge_selection_evidence(shard_reports[:3], set(candles), config, 4, "fixture-provenance")
    mismatch = [dict(report) for report in shard_reports]
    mismatch[0] = {**mismatch[0], "config_version": "wrong"}
    with pytest.raises(ValueError, match="config or schema"):
        merge_selection_evidence(mismatch, set(candles), config, 4, "fixture-provenance")


def _shard_report(index: int, events: list[dict[str, object]], symbols: list[str], selected: dict[str, dict[str, str]]) -> dict[str, object]:
    families = ("trend_breakout", "trend_pullback", "range_mean_reversion")
    return {
        "report_version": "1.2.0",
        "config_version": load_config()["configVersion"],
        "shard": {"index": index, "count": 2, "symbols": symbols},
        "walk_forward": {"selected_parameters": selected, "selection_owner": index == 0, "selection_evidence": [{"parameter_key": "same"}] if index == 0 else []},
        "live_events": events,
        "candidate_events": [{**event, "net_r": 100.0} for event in events],
        "parameter_events": [{"symbol": symbol, "parameter_key": "same"} for symbol in symbols],
        "per_symbol": {symbol: {} for symbol in symbols},
        "evidence": {"rows": [{"symbol": symbol, "family": family, "formal_eligible": True} for symbol in symbols for family in families]},
        "data_quality": {"gap_count": 0, "prepare_report_validated": True, "point_in_time_membership_supported": True, "funding_missing_symbols": []},
        "liquidity_and_costs": {"historical_liquidity_proxy_supported": True},
        "cost_sensitivity": {"base_mean_r": 0.1, "double_slippage_mean_r": 0.05, "impact_stress_mean_r": 0.02},
        "concentration": {"concentration_pass": True},
        "gates": {"formal_alerts_available": True},
    }


def test_shard_aggregation_owns_symbols_once_and_uses_capacity_accepted_events(tmp_path: Path) -> None:
    selected = {family: {"long": "same", "short": "same"} for family in ("trend_breakout", "trend_pullback", "range_mean_reversion")}
    events: list[dict[str, object]] = []
    for index, direction in enumerate(("long", "short", "long", "short", "long", "short", "long", "short")):
        events.append({"symbol": f"S{index}", "family": "trend_breakout", "direction": direction, "entry_time": 1_000 + index, "exit_time": 10_000 + index, "net_r": 0.1, "shadow_only": False})
    first = _shard_report(0, events[:4], [f"S{index}" for index in range(4)], selected)
    second = _shard_report(1, events[4:], [f"S{index}" for index in range(4, 8)], selected)
    first_path, second_path = tmp_path / "shard-0.json", tmp_path / "shard-1.json"
    first_path.write_text(json.dumps(first), encoding="utf-8")
    second_path.write_text(json.dumps(second), encoding="utf-8")
    aggregate = aggregate_reports([first_path, second_path])
    assert set(first["shard"]["symbols"]).isdisjoint(second["shard"]["symbols"])
    assert aggregate["walk_forward"]["selected_parameters"] == selected
    assert {event["symbol"] for event in aggregate["parameter_events"]} == {f"S{index}" for index in range(8)}
    assert len(aggregate["candidate_events"]) == 6
    assert aggregate["portfolio"]["trades"] == 6
    assert aggregate["recent"]["trades"] == 6
    owned = [_owned_symbols({f"S{index}" for index in range(8)}, shard, 2) for shard in range(2)]
    assert set(owned[0]).isdisjoint(owned[1])
    assert set(owned[0]) | set(owned[1]) == {f"S{index}" for index in range(8)}


def test_positive_raw_challenger_can_fail_against_champion_on_identical_blocks() -> None:
    config = load_config()
    families = ("trend_breakout", "trend_pullback", "range_mean_reversion")
    base = {
        "report_version": "1.2.0",
        "config_version": config["configVersion"],
        "gates": {"formal_alerts_available": True},
        "per_family": {family: {"formal_eligible": True, "mean_r": 0.3, "trades": 400} for family in families},
        "portfolio": {"max_drawdown": 0.02},
        "recent": {"mean_r": 0.3},
        "concentration": {"contributions": {"A": 1, "B": 1, "C": 1}, "concentration_pass": True},
        "cost_sensitivity": {"all_stress_scenarios_positive": True},
        "data_quality": {"prepare_report_validated": True, "point_in_time_membership_supported": True},
        "liquidity_and_costs": {"historical_liquidity_proxy_supported": True},
        "oos_blocks": [{"block_id": str(index), "mean_r": 0.3} for index in range(30)],
    }
    challenger = {**base, "per_family": {family: {"formal_eligible": True, "mean_r": 0.4, "trades": 400} for family in families}, "recent": {"mean_r": 0.4}, "oos_blocks": [{"block_id": str(index), "mean_r": 0.2} for index in range(30)]}
    publication = prepare_publication(challenger, config, champion_report=base)
    assert publication["decision"]["overallImprovement"] > 0
    assert publication["decision"]["pairedLowerBound95"] < 0
    assert publication["published"] is False
