from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest

from sol_alerts.backtest import _parameter_keys, run_backtest
from sol_alerts.config import load_config
from sol_alerts.bootstrap import build_risk_state
from sol_alerts.data import Instrument, eligibility, ordered_split
from sol_alerts.features import ema, state
from sol_alerts.fixtures import synthetic_candles
from sol_alerts.portfolio import find_exit, simulate_signal
from sol_alerts.publish import _aggregate_single_reports, _paired_differences_from_blocks, prepare_publication, publish_if_approved
from sol_alerts.strategies import Signal, generate_signals
from sol_alerts.weights import build_candidate_descriptor, descriptor_hash, evaluate_challenger, shrink_symbol_weights, validate_descriptor, weekly_weight_step, weight_mode


def _descriptor_fixture(config: dict) -> tuple[dict, dict, list[dict]]:
    keys = _parameter_keys(config)
    selected = {family: {direction: values[0] for direction in ("long", "short")} for family, values in keys.items()}
    evidence = [
        {"family": family, "direction": direction, "parameter_key": selected[family][direction], "train_mean_r": 0.1, "validation_mean_r": 0.2}
        for family in keys for direction in ("long", "short")
    ]
    champion = build_candidate_descriptor(config, selected, evidence)
    challenger = build_candidate_descriptor(config, selected, evidence, previous_descriptor=champion)
    return champion, challenger, evidence


def _evaluated_report(config: dict, champion: dict, challenger: dict, challenger_r: float, champion_r: float, difference: float) -> dict:
    families = ("trend_breakout", "trend_pullback", "range_mean_reversion")
    paired = [{"block_id": str(index), "cutoff": 10_000, "champion_descriptor_hash": champion["descriptorHash"], "challenger_descriptor_hash": challenger["descriptorHash"], "champion_mean_r": champion_r, "challenger_mean_r": champion_r + difference, "difference": difference} for index in range(30)]
    return {
        "report_version": "1.3.0-replay-aggregate",
        "config_version": config["configVersion"],
        "candidate_descriptor": challenger,
        "descriptorHash": challenger["descriptorHash"],
        "evaluation": {"same_untouched_blocks": True, "cutoff": 10_000, "champion_descriptor_hash": champion["descriptorHash"], "challenger_descriptor_hash": challenger["descriptorHash"]},
        "walk_forward": {"selected_parameters": challenger["selectedParameters"], "selection_evidence": []},
        "gates": {"formal_alerts_available": True},
        "cost_sensitivity": {"all_stress_scenarios_positive": True},
        "concentration": {"contributions": {"A": 1, "B": 1, "C": 1}, "concentration_pass": True},
        "data_quality": {"prepare_report_validated": True, "point_in_time_membership_supported": True},
        "liquidity_and_costs": {"historical_liquidity_proxy_supported": True, "historical_liquidity_proxy_schema": "sol-alerts-impact-proxy-v1"},
        "portfolio_events": [{"symbol": "A", "entry_time": 1, "exit_time": 2, "net_r": challenger_r}],
        "portfolio": {"max_drawdown": 0.02},
        "recent": {"mean_r": challenger_r},
        "per_family": {family: {"formal_eligible": True, "trades": 400, "mean_r": challenger_r} for family in families},
        "evidence": {"rows": []},
        "champion_replay": {"portfolio_events": [{"symbol": "A", "entry_time": 1, "exit_time": 2, "net_r": champion_r}], "portfolio": {"max_drawdown": 0.02}, "recent": {"mean_r": champion_r}, "concentration": {"contributions": {"A": 1, "B": 1, "C": 1}}, "oos_blocks": [{"block_id": str(index), "mean_r": champion_r} for index in range(30)]},
        "paired_oos_blocks": paired,
    }


def test_ordered_split_has_no_shuffle() -> None:
    train, validation, test = ordered_split(list(range(10)), 0.6, 0.2)
    assert train == [0, 1, 2, 3, 4, 5]
    assert validation == [6, 7]
    assert test == [8, 9]


def test_feature_at_time_does_not_read_future() -> None:
    candles = synthetic_candles("BTCUSDT", 300)
    config = load_config()
    before = generate_signals("BTCUSDT", candles[:220], config, "long")
    after = generate_signals("BTCUSDT", candles[:220] + candles[220:], config, "long")
    assert [(signal.index, signal.parameter_key) for signal in before] == [(signal.index, signal.parameter_key) for signal in after if signal.index < 220]
    assert ema([1.0, 2.0, 3.0], 2) == 2.5


def test_stop_wins_when_stop_and_target_share_bar() -> None:
    config = load_config()
    candles = synthetic_candles("BTCUSDT", 100)
    signal = Signal("BTCUSDT", "trend_breakout", "long", "fixture", 50, candles[50].close_time, 99, 101, 101, 95, 95, 110, 2.5)
    conflict_bar = replace(candles[51], open=100, high=112, low=90, close=105)
    index, price, reason = find_exit(signal, candles[:51] + [conflict_bar] + candles[52:], 51, 10)
    assert index == 51
    assert price == 95
    assert reason == "stop"
    trade = simulate_signal(signal, candles[:51] + [conflict_bar] + candles[52:], config)
    assert trade is not None
    assert trade.reason == "stop"


def test_weight_shrinkage_and_weekly_cap() -> None:
    base = {"trend_breakout": 0.34, "trend_pullback": 0.33, "range_mean_reversion": 0.33}
    results = {
        "trend_breakout": type("R", (), {"mean_r": 1.0, "trades": 3})(),
        "trend_pullback": type("R", (), {"mean_r": -1.0, "trades": 300})(),
        "range_mean_reversion": type("R", (), {"mean_r": 0.0, "trades": 300})(),
    }
    weights = shrink_symbol_weights(base, results)
    assert abs(sum(weights.values()) - 1) < 1e-12
    stepped = weekly_weight_step(base, {"trend_breakout": 0.9, "trend_pullback": 0.05, "range_mean_reversion": 0.05})
    assert all(abs(stepped[key] - base[key]) <= 0.11 for key in base)
    assert weight_mode(0.2, True) == "shadow"
    assert weight_mode(0.26, True) == "active"


def test_challenger_requires_positive_paired_lower_bound_and_not_concentrated() -> None:
    decision = evaluate_challenger([0.2] * 20, 0.1, 0.3, 0.1, 0.12, 0.05, [1, 1, 1], 0.1, 7, 100)
    assert decision.passes is True
    failed = evaluate_challenger([0.2] * 20, 0.1, 0.3, 0.1, 0.12, 0.05, [10, 1], 0.1, 7, 100)
    assert failed.passes is False


def test_state_is_finite_and_config_is_shared() -> None:
    config = load_config()
    candles = synthetic_candles("ETHUSDT", 300)
    assert state(candles) in {"trend_up", "trend_down", "range", "ambiguous"}
    assert config["schemaVersion"] == 1
    assert config["universe"]["minQuoteVolume24h"] == 20_000_000


def test_risk_bootstrap_is_seeded_and_perpetual_delivery_metadata_is_not_a_gate() -> None:
    config = load_config()
    candles = synthetic_candles("BTCUSDT", 8_641)
    other = synthetic_candles("ETHUSDT", 8_641)
    state = build_risk_state({"BTCUSDT": candles, "ETHUSDT": other})
    assert state["baselineVersion"] == "risk-v1"
    assert len(state["btcReturns"]) == 8_640
    assert len(state["eligibleMarketReturns"]) == 8_640
    instrument = Instrument("BTCUSDT", delivery_time=4_133_404_800_000)
    assert eligibility(instrument, 365, 20_000_000, 5, 200_000, config)["formal_eligible"] is True


def test_publication_preparation_is_weight_only_and_normalized() -> None:
    config = load_config()
    candidate = prepare_publication({"gates": {"formal_alerts_available": False}, "portfolio": {"max_drawdown": 0}, "per_family": {}}, config)
    assert candidate["kind"] == "weight-only-config-activation"
    assert abs(sum(candidate["weights"].values()) - 1.0) < 1e-12
    assert candidate["rollbackPointer"] == "previous_champion"


def test_publication_requires_paired_oos_gate_and_fake_boundary_is_atomic() -> None:
    config = load_config()
    champion = {"per_family": {family: {"trades": 400, "mean_r": 0.1, "formal_eligible": True} for family in ("trend_breakout", "trend_pullback", "range_mean_reversion")}, "per_symbol": {symbol: {"trades": 400, "mean_r": 0.1} for symbol in ("A", "B", "C")}, "portfolio": {"max_drawdown": 0.05}, "gates": {"formal_alerts_available": True}, "paired_oos_blocks": [0.0], "recent": {"mean_r": 0.1}}
    failed = prepare_publication(champion, config, champion_report=champion)
    assert failed["published"] is False
    calls: list[dict] = []

    class FakeClient:
        def publish_atomically(self, publication: dict) -> dict:
            calls.append(publication)
            return {"published": True, "version": "challenger-1"}

    assert publish_if_approved(failed, FakeClient())["published"] is False
    assert calls == []
    challenger = dict(champion)
    challenger["paired_oos_blocks"] = [0.2] * 30
    challenger["per_family"] = {family: {"trades": 400, "mean_r": 0.2, "formal_eligible": True} for family in ("trend_breakout", "trend_pullback", "range_mean_reversion")}
    passed = prepare_publication(challenger, config, champion_report=champion)
    assert passed["published"] is True
    assert publish_if_approved(passed, FakeClient())["published"] is True
    assert len(calls) == 1


def test_descriptor_proposal_ignores_untouched_outcomes() -> None:
    config = load_config()
    champion, challenger, evidence = _descriptor_fixture(config)
    changed_oos = {"per_family": {family: {"mean_r": 999 if family == "trend_breakout" else -999} for family in ("trend_breakout", "trend_pullback", "range_mean_reversion")}}
    replayed = build_candidate_descriptor(config, challenger["selectedParameters"], evidence, previous_descriptor=champion)
    assert replayed == challenger
    assert changed_oos["per_family"]["trend_breakout"]["mean_r"] != evidence[0]["validation_mean_r"]


def test_losing_exact_descriptor_is_not_replaced_by_posthoc_oos_weight_mutation() -> None:
    config = load_config()
    champion, challenger, _evidence = _descriptor_fixture(config)
    report = _evaluated_report(config, champion, challenger, challenger_r=-0.1, champion_r=0.1, difference=-0.2)
    publication = prepare_publication(report, config, champion_report=report["champion_replay"], champion_descriptor=champion)
    assert publication["published"] is False
    assert publication["candidateDescriptor"] == challenger
    assert publication["weights"] == challenger["weights"]


def test_descriptor_hash_mutation_fails_closed() -> None:
    config = load_config()
    champion, challenger, _evidence = _descriptor_fixture(config)
    tampered = deepcopy(challenger)
    tampered["weights"] = {**tampered["weights"], "trend_breakout": tampered["weights"]["trend_breakout"] + 0.01}
    report = _evaluated_report(config, champion, tampered, challenger_r=0.3, champion_r=0.1, difference=0.2)
    report["descriptorHash"] = challenger["descriptorHash"]
    report["evaluation"]["challenger_descriptor_hash"] = challenger["descriptorHash"]
    publication = prepare_publication(report, config, champion_report=report["champion_replay"], champion_descriptor=champion)
    assert publication["published"] is False
    assert publication["decision"]["descriptorMatch"] is False


def test_publication_requires_the_external_frozen_descriptor_exactly() -> None:
    config = load_config()
    champion, challenger, _evidence = _descriptor_fixture(config)
    report = _evaluated_report(config, champion, challenger, challenger_r=0.3, champion_r=0.1, difference=0.2)
    publication = prepare_publication(
        report,
        config,
        champion_report=report["champion_replay"],
        champion_descriptor=champion,
        evaluated_descriptor=champion,
    )
    assert publication["published"] is False
    assert publication["decision"]["frozenDescriptorMatch"] is False


def test_publication_content_binding_rejects_nested_top_level_and_report_mutations() -> None:
    config = load_config()
    champion, challenger, _evidence = _descriptor_fixture(config)
    report = _evaluated_report(config, champion, challenger, challenger_r=0.3, champion_r=0.1, difference=0.2)
    original = prepare_publication(report, config, champion_report=report["champion_replay"], champion_descriptor=champion)
    assert original["published"] is True

    mutations = []
    nested_descriptor = deepcopy(original)
    nested_descriptor["candidateDescriptor"] = deepcopy(nested_descriptor["candidateDescriptor"])
    nested_descriptor["candidateDescriptor"]["weights"] = {**nested_descriptor["candidateDescriptor"]["weights"], "trend_breakout": nested_descriptor["candidateDescriptor"]["weights"]["trend_breakout"] + 0.01}
    mutations.append(nested_descriptor)
    top_level = deepcopy(original)
    top_level["weights"] = {**top_level["weights"], "trend_breakout": top_level["weights"]["trend_breakout"] + 0.01}
    mutations.append(top_level)
    nested_report = deepcopy(original)
    nested_report["challengerReport"]["candidate_descriptor"] = {**challenger, "source": {"tampered": True}}
    mutations.append(nested_report)

    calls: list[dict] = []

    class FakeClient:
        def publish_atomically(self, value: dict) -> dict:
            calls.append(value)
            return {"published": True}

    for mutation in mutations:
        assert publish_if_approved(mutation, FakeClient())["published"] is False
    assert calls == []


def test_descriptor_rejects_parameter_outside_the_frozen_grid() -> None:
    config = load_config()
    _champion, challenger, _evidence = _descriptor_fixture(config)
    invalid = deepcopy(challenger)
    invalid["selectedParameters"] = {
        **invalid["selectedParameters"],
        "trend_breakout": {**invalid["selectedParameters"]["trend_breakout"], "long": "lookback=999;buffer=9.9;volume=9.9"},
    }
    invalid["descriptorHash"] = descriptor_hash(invalid)
    assert validate_descriptor(config, invalid) is False


def test_aggregate_duplicate_activation_uses_trigger_priority_not_realized_net_r() -> None:
    config = load_config()
    selected = {family: {direction: _parameter_keys(config)[family][0] for direction in ("long", "short")} for family in ("trend_breakout", "trend_pullback", "range_mean_reversion")}
    common = {"symbol": "BTCUSDT", "direction": "long", "activation_key": "BTCUSDT:long:100", "entry_time": 101, "exit_time": 200, "shadow_only": False}
    low_outcome = {**common, "family": "trend_breakout", "parameter_key": selected["trend_breakout"]["long"], "trigger_score": 99, "net_r": -10}
    high_outcome = {**common, "family": "trend_pullback", "parameter_key": selected["trend_pullback"]["long"], "trigger_score": 10, "net_r": 10}
    report = _aggregate_single_reports([{
        "config_version": config["configVersion"],
        "walk_forward": {"selected_parameters": selected},
        "live_events": [low_outcome, high_outcome],
        "evidence": {"rows": []},
        "data_quality": {"gap_count": 0, "prepare_report_validated": True, "point_in_time_membership_supported": True, "funding_missing_symbols": []},
        "liquidity_and_costs": {"historical_liquidity_proxy_supported": True},
        "per_symbol": {},
    }])
    assert report["live_events"][0]["family"] == "trend_breakout"
    assert report["portfolio_events"][0]["net_r"] == -10


def test_identical_oos_block_pairing_keeps_zero_return_calendar_blocks_and_rejects_mismatch() -> None:
    rows = _paired_differences_from_blocks(
        {"oos_blocks": [{"block_id": "1", "mean_r": -0.1}, {"block_id": "2", "mean_r": 0.0}]},
        {"oos_blocks": [{"block_id": "1", "mean_r": 0.0}, {"block_id": "2", "mean_r": 0.2}]},
        12345,
        "champion-hash",
        "challenger-hash",
    )
    assert [row["block_id"] for row in rows] == ["1", "2"]
    assert rows[0]["difference"] == 0.1
    assert rows[1]["difference"] == 0.2
    assert rows[0]["cutoff"] == 12345
    assert rows[0]["champion_descriptor_hash"] == "champion-hash"
    with pytest.raises(ValueError, match="exact untouched calendar blocks"):
        _paired_differences_from_blocks(
            {"oos_blocks": [{"block_id": "1", "mean_r": 0.1}, {"block_id": "2", "mean_r": 0.2}]},
            {"oos_blocks": [{"block_id": "2", "mean_r": 0.4}, {"block_id": "3", "mean_r": 0.5}]},
            12345,
            "champion-hash",
            "challenger-hash",
        )
    with pytest.raises(ValueError, match="cutoff metadata"):
        _paired_differences_from_blocks(
            {"evaluation": {"cutoff": 1}, "oos_blocks": [{"block_id": "1", "mean_r": 0.1}]},
            {"evaluation": {"cutoff": 2}, "oos_blocks": [{"block_id": "1", "mean_r": 0.2}]},
            1,
            "champion-hash",
            "challenger-hash",
        )


def test_passing_publication_persists_the_evaluated_descriptor_exactly() -> None:
    config = load_config()
    champion, challenger, _evidence = _descriptor_fixture(config)
    report = _evaluated_report(config, champion, challenger, challenger_r=0.3, champion_r=0.1, difference=0.2)
    publication = prepare_publication(report, config, champion_report=report["champion_replay"], champion_descriptor=champion)
    assert publication["published"] is True
    captured: list[dict] = []

    class FakeClient:
        def publish_atomically(self, value: dict) -> dict:
            captured.append(value)
            return {"published": True, "descriptorHash": value["descriptorHash"]}

    assert publish_if_approved(publication, FakeClient())["published"] is True
    assert captured[0]["candidateDescriptor"] == challenger
    assert captured[0]["descriptorHash"] == descriptor_hash(challenger)


def test_replay_evaluates_both_frozen_descriptors_on_one_cutoff(tmp_path: Path) -> None:
    config = load_config()
    champion, challenger, _evidence = _descriptor_fixture(config)
    descriptor_path = tmp_path / "evaluation-descriptors.json"
    descriptor_path.write_text(json.dumps({"schema": "sol-alerts-evaluation-descriptors-v1", "configVersion": config["configVersion"], "champion": champion, "challenger": challenger}), encoding="utf-8")
    replay = run_backtest(smoke=True, evaluation_descriptors_path=descriptor_path)
    assert replay["schema"] == "sol-alerts-replay-shard-v1"
    assert replay["champion"]["descriptorHash"] == champion["descriptorHash"]
    assert replay["challenger"]["descriptorHash"] == challenger["descriptorHash"]
    assert replay["evaluation"]["same_untouched_blocks"] is True
    assert replay["champion"]["evaluation"]["cutoff"] == replay["challenger"]["evaluation"]["cutoff"]
    assert all(row["difference"] == row["challenger_mean_r"] - row["champion_mean_r"] for row in replay["paired_oos_blocks"])
