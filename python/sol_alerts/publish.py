from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Protocol
from urllib.request import Request, urlopen

from .backtest import _calendar_block_ids, _calendar_oos_blocks, _parameter_keys
from .config import load_config
from .statistics import block_bootstrap_lower_bound, concentration_share, max_drawdown_from_r
from .weights import DESCRIPTOR_SCHEMA, FAMILIES, FamilyResult, build_candidate_descriptor, descriptor_hash, evaluate_challenger, shrink_symbol_weights, validate_descriptor, weekly_weight_step, weight_mode


def _event_drawdown(events: list[dict[str, Any]]) -> float:
    return max_drawdown_from_r([float(event.get("net_r", 0.0)) for event in sorted(events, key=lambda item: (int(item.get("exit_time", 0)), str(item.get("symbol", ""))))])


def _valid_selected_parameters(config: dict[str, Any], selected: Any) -> bool:
    if not isinstance(selected, dict):
        return False
    keys = {family: set(values) for family, values in _parameter_keys(config).items()}
    for family in FAMILIES:
        values = selected.get(family)
        if not isinstance(values, dict) or not isinstance(values.get("long"), str) or not isinstance(values.get("short"), str):
            return False
        if values["long"] not in keys[family] or values["short"] not in keys[family]:
            return False
    return True


def _portfolio_from_events(events: list[dict[str, Any]], config: dict[str, Any]) -> tuple[list[dict[str, Any]], float]:
    """Apply capacity once after all shards have been merged."""
    accepted: list[dict[str, Any]] = []
    active: list[dict[str, Any]] = []
    equity_curve = [1.0]
    risk_rate = config["execution"]["riskPerSimulatedTrade"]
    equity = 1.0

    def close_due(cutoff: int) -> None:
        nonlocal active, equity
        due = sorted((item for item in active if int(item.get("exit_time", 0)) <= cutoff), key=lambda item: (int(item.get("exit_time", 0)), str(item.get("symbol", ""))))
        active = [item for item in active if int(item.get("exit_time", 0)) > cutoff]
        for item in due:
            equity *= 1.0 + float(item.get("net_r", 0.0)) * risk_rate
            equity_curve.append(equity)

    for event in sorted(events, key=lambda item: (int(item.get("entry_time", 0)), str(item.get("symbol", "")), str(item.get("family", "")))):
        if bool(event.get("shadow_only", False)):
            continue
        entry = int(event.get("entry_time", 0))
        close_due(entry)
        longs = sum(item.get("direction") == "long" for item in active)
        shorts = sum(item.get("direction") == "short" for item in active)
        direction = str(event.get("direction"))
        if len(active) >= config["execution"]["maxOpenPositions"] or direction == "long" and longs >= config["execution"]["maxLongPositions"] or direction == "short" and shorts >= config["execution"]["maxShortPositions"]:
            continue
        accepted.append(event)
        active.append(event)
    close_due(2**63 - 1)
    peak = equity_curve[0]
    drawdown = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        drawdown = max(drawdown, (peak - value) / peak if peak else 0.0)
    return accepted, drawdown


def _aggregate_single_reports(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge shard sufficient statistics and events before any gate decision."""
    if not reports:
        raise ValueError("at least one shard report is required")
    config_version = reports[0].get("config_version")
    if any(report.get("config_version") != config_version for report in reports):
        raise ValueError("shards contain incompatible config versions")
    selected = reports[0].get("walk_forward", {}).get("selected_parameters", {})
    if any(report.get("walk_forward", {}).get("selected_parameters", selected) != selected for report in reports[1:]):
        raise ValueError("shards contain incompatible selected parameter maps")
    descriptor_hashes = {str(report.get("descriptorHash")) for report in reports if report.get("descriptorHash")}
    if len(descriptor_hashes) > 1:
        raise ValueError("shards contain incompatible evaluated descriptors")
    # Shards own disjoint symbols and emit live-equivalent activations.  Raw
    # candidate events are diagnostic only; capacity and all publication
    # metrics are recomputed once here across the merged absolute timeline.
    merged_events = [event for report in reports for event in report.get("live_events", report.get("candidate_events", []))]
    event_by_activation: dict[str, dict[str, Any]] = {}
    for event in merged_events:
        key = str(event.get("activation_key") or f"{event.get('symbol')}:{event.get('direction')}:{event.get('trigger_time')}")
        previous = event_by_activation.get(key)
        # Duplicate activation rows are a merge/idempotency fault, not an
        # opportunity to choose the trade with the best realized outcome.
        # Keep the same trigger-time ordering used by live combination so a
        # duplicate can never introduce untouched-test lookahead.
        event_priority = (-float(event.get("trigger_score", 0.0)), str(event.get("family", "")), str(event.get("parameter_key", "")))
        previous_priority = (-float(previous.get("trigger_score", 0.0)), str(previous.get("family", "")), str(previous.get("parameter_key", ""))) if previous is not None else None
        if previous is None or event_priority < previous_priority:
            event_by_activation[key] = event
    events = list(event_by_activation.values())
    merged_parameter_events = [event for report in reports for event in report.get("parameter_events", [])]
    parameter_by_key = {str(event.get("activation_key") or f"{event.get('symbol')}:{event.get('family')}:{event.get('direction')}:{event.get('parameter_key')}:{event.get('trigger_time')}"): event for event in merged_parameter_events}
    parameter_events = list(parameter_by_key.values())
    portfolio_events, portfolio_dd = _portfolio_from_events(events, load_config())
    per_symbol: dict[str, Any] = {}
    for report in reports:
        per_symbol.update(report.get("per_symbol", {}))
    per_family: dict[str, dict[str, Any]] = {}
    evidence_rows = [row for report in reports for row in report.get("evidence", {}).get("rows", [])]
    for family in FAMILIES:
        family_events = [event for event in portfolio_events if event.get("family") == family]
        family_values = [float(event.get("net_r", 0.0)) for event in sorted(family_events, key=lambda item: int(item.get("exit_time", 0)))]
        lower = block_bootstrap_lower_bound(family_values, load_config()["research"]["randomSeed"], min(2_000, load_config()["research"]["bootstrapReplicates"])) if family_values else 0.0
        family_rows = [row for row in evidence_rows if row.get("family") == family]
        per_family[family] = {
            "trades": len(family_values),
            "mean_r": sum(family_values) / len(family_values) if family_values else 0.0,
            "max_drawdown": _event_drawdown(family_events),
            "symbols": len({event.get("symbol") for event in family_events}),
            "lower_bound_95": lower,
            "formal_eligible": any(bool(row.get("formal_eligible")) for row in family_rows) and len(family_values) >= load_config()["research"]["minimumTestTradesPerFamily"] and lower > 0 and _event_drawdown(family_events) <= load_config()["research"]["maxPortfolioDrawdown"],
        }
    contributions = {symbol: sum(float(event.get("net_r", 0.0)) for event in portfolio_events if event.get("symbol") == symbol) for symbol in sorted({str(event.get("symbol")) for event in portfolio_events})}
    recent_cutoff = max((int(event.get("exit_time", 0)) for event in events), default=0) - load_config()["research"]["adaptiveRecentWindowDays"] * 86_400_000
    recent_events = [event for event in portfolio_events if int(event.get("exit_time", 0)) >= recent_cutoff]
    # Shards do not have the Champion's identical block returns. Preserve only
    # literal already-paired differences; raw challenger block means are
    # recomputed against the champion after aggregation.
    pair_rows = [block for report in reports for block in report.get("paired_oos_blocks", []) if isinstance(block, dict) and "difference" in block]
    cost_reports = [report.get("cost_sensitivity", {}) for report in reports]
    if portfolio_events and all("double_slippage_net_r" in event and "impact_stress_net_r" in event for event in portfolio_events):
        cost_sensitivity = {
            "base_mean_r": sum(float(event.get("net_r", 0.0)) for event in portfolio_events) / len(portfolio_events),
            "double_slippage_mean_r": sum(float(event.get("double_slippage_net_r", 0.0)) for event in portfolio_events) / len(portfolio_events),
            "impact_stress_mean_r": sum(float(event.get("impact_stress_net_r", 0.0)) for event in portfolio_events) / len(portfolio_events),
        }
    else:
        cost_sensitivity = {
            "base_mean_r": sum(float(item.get("base_mean_r", 0.0)) for item in cost_reports) / len(cost_reports) if cost_reports else 0.0,
            "double_slippage_mean_r": sum(float(item.get("double_slippage_mean_r", 0.0)) for item in cost_reports) / len(cost_reports) if cost_reports else 0.0,
            "impact_stress_mean_r": sum(float(item.get("impact_stress_mean_r", 0.0)) for item in cost_reports) / len(cost_reports) if cost_reports else 0.0,
        }
    cost_sensitivity["all_stress_scenarios_positive"] = all(value > 0 for value in (cost_sensitivity["base_mean_r"], cost_sensitivity["double_slippage_mean_r"], cost_sensitivity["impact_stress_mean_r"]))
    data_quality = {
        "gap_count": sum(int(report.get("data_quality", {}).get("gap_count", 0)) for report in reports),
        "missing_data_gaps_reported": True,
        "funding_missing_symbols": sorted({symbol for report in reports for symbol in report.get("data_quality", {}).get("funding_missing_symbols", [])}),
        "prepare_report_validated": all(bool(report.get("data_quality", {}).get("prepare_report_validated")) for report in reports),
        "point_in_time_membership_supported": all(bool(report.get("data_quality", {}).get("point_in_time_membership_supported")) for report in reports),
        "historical_liquidity_proxy_supported": all(bool(report.get("liquidity_and_costs", {}).get("historical_liquidity_proxy_supported")) for report in reports),
        "survivorship_bias_warning": "official manifests include active and historically observed contracts; availability remains data-dependent",
    }
    evaluation_values = [report.get("evaluation", {}) for report in reports]
    has_calendar_blocks = any(isinstance(value, dict) and "oos_block_ids" in value for value in evaluation_values)
    oos_block_ids: list[str] = []
    if has_calendar_blocks:
        if any(not isinstance(value, dict) or "oos_block_ids" not in value for value in evaluation_values):
            raise ValueError("shards do not all declare the untouched calendar block universe")
        block_universes = {tuple(str(block_id) for block_id in value.get("oos_block_ids", [])) for value in evaluation_values}
        interval_universes = {(int(value.get("untouched_test_start", 0)), int(value.get("untouched_test_end", 0))) for value in evaluation_values}
        if len(block_universes) != 1 or len(interval_universes) != 1 or not next(iter(block_universes), ()):
            raise ValueError("shards do not share the exact untouched calendar interval")
        oos_block_ids = list(next(iter(block_universes)))
        untouched_start, untouched_end = next(iter(interval_universes))
        if oos_block_ids != _calendar_block_ids(untouched_start, untouched_end):
            raise ValueError("declared untouched calendar blocks are invalid")
        oos_blocks = _calendar_oos_blocks(portfolio_events, untouched_start, untouched_end)
    else:
        block_values: dict[int, list[float]] = {}
        for event in portfolio_events:
            block_values.setdefault(int(event.get("exit_time", 0)) // (7 * 86_400_000), []).append(float(event.get("net_r", 0.0)))
        oos_blocks = [{"block_id": str(block_id), "mean_r": sum(values) / len(values)} for block_id, values in sorted(block_values.items()) if values]
    return {
        "report_version": "1.2.0-aggregate",
        "config_version": config_version,
        "candidate_descriptor": reports[0].get("candidate_descriptor"),
        "descriptorHash": reports[0].get("descriptorHash"),
        "shard": {"count": len(reports), "symbols": sorted(per_symbol)},
        "walk_forward": {"selected_parameters": selected, "selection": "global family-direction key selected before untouched test", "selection_evidence": [row for report in reports if report.get("walk_forward", {}).get("selection_owner", False) for row in report.get("walk_forward", {}).get("selection_evidence", [])]},
        "evidence": {"rows": evidence_rows, "usable_by_runtime": True, "per_symbol_family_direction": True},
        "data_quality": data_quality,
        "liquidity_and_costs": {"historical_liquidity_proxy_supported": data_quality["historical_liquidity_proxy_supported"], "historical_liquidity_proxy_schema": "sol-alerts-impact-proxy-v1", "historical_spread_depth_proxy": "public klines do not contain historical spread/depth; accepted events carry the conservative proxy stress"},
        "candidate_events": portfolio_events,
        "live_events": events,
        "portfolio_events": portfolio_events,
        "parameter_events": parameter_events,
        "per_symbol": per_symbol,
        "per_family": per_family,
        "portfolio": {"trades": len(portfolio_events), "max_drawdown": portfolio_dd, "max_open_positions": load_config()["execution"]["maxOpenPositions"], "max_long_positions": load_config()["execution"]["maxLongPositions"], "max_short_positions": load_config()["execution"]["maxShortPositions"]},
        "concentration": {"contributions": contributions, "symbol_contribution_share": concentration_share(list(contributions.values())), "concentration_pass": concentration_share(list(contributions.values())) < 0.5},
        "cost_sensitivity": cost_sensitivity,
        "gates": {"formal_alerts_available": any(bool(row.get("formal_eligible")) for row in evidence_rows) and any(bool(row.get("formal_eligible")) for row in per_family.values()) and portfolio_dd <= load_config()["research"]["maxPortfolioDrawdown"] and concentration_share(list(contributions.values())) < 0.5 and cost_sensitivity["all_stress_scenarios_positive"] and data_quality["gap_count"] == 0 and not data_quality["funding_missing_symbols"] and data_quality["prepare_report_validated"] and data_quality["point_in_time_membership_supported"] and data_quality["historical_liquidity_proxy_supported"]},
        "recent": {"trades": len(recent_events), "mean_r": sum(float(event.get("net_r", 0.0)) for event in recent_events) / len(recent_events) if recent_events else 0.0},
        "adaptive_evidence": {"recent_window_trades": len(recent_events), "recent_window_mean_r": sum(float(event.get("net_r", 0.0)) for event in recent_events) / len(recent_events) if recent_events else 0.0, "older_data": "stress_only"},
        "paired_oos_blocks": pair_rows,
        "oos_blocks": oos_blocks,
        "oos_block_ids": oos_block_ids,
        "evaluation": {**(reports[0].get("evaluation", {}) if isinstance(reports[0].get("evaluation"), dict) else {}), "oos_block_ids": oos_block_ids} if has_calendar_blocks else {},
    }


def _aggregate_replay_reports(reports: list[dict[str, Any]]) -> dict[str, Any]:
    if not reports or any(report.get("schema") != "sol-alerts-replay-shard-v1" for report in reports):
        raise ValueError("replay shard schema is invalid")
    cutoff_values = {int(report.get("evaluation", {}).get("cutoff", 0)) for report in reports}
    challenger_hashes = {str(report.get("evaluation", {}).get("challenger_descriptor_hash")) for report in reports}
    champion_hashes = {str(report.get("evaluation", {}).get("champion_descriptor_hash")) for report in reports if report.get("evaluation", {}).get("champion_descriptor_hash")}
    block_universes = {tuple(str(block_id) for block_id in report.get("evaluation", {}).get("oos_block_ids", [])) for report in reports}
    interval_universes = {(int(report.get("evaluation", {}).get("untouched_test_start", 0)), int(report.get("evaluation", {}).get("untouched_test_end", 0))) for report in reports}
    if len(cutoff_values) != 1 or len(challenger_hashes) != 1 or len(champion_hashes) > 1 or len(block_universes) != 1 or not next(iter(block_universes), ()) or len(interval_universes) != 1:
        raise ValueError("replay shards do not share one cutoff and descriptor set")
    if any(report.get("evaluation", {}).get("same_untouched_blocks") is not True for report in reports):
        raise ValueError("replay shard does not attest to identical untouched blocks")
    challenger_reports = [report["challenger"] for report in reports if isinstance(report.get("challenger"), dict)]
    champion_reports = [report["champion"] for report in reports if isinstance(report.get("champion"), dict)]
    if not challenger_reports:
        raise ValueError("replay has no challenger reports")
    challenger = _aggregate_single_reports(challenger_reports)
    champion = _aggregate_single_reports(champion_reports) if champion_reports else None
    cutoff = next(iter(cutoff_values))
    untouched_start, untouched_end = next(iter(interval_universes))
    block_ids = list(next(iter(block_universes)))
    if block_ids != _calendar_block_ids(untouched_start, untouched_end):
        raise ValueError("replay shards do not share the exact untouched calendar blocks")
    challenger_hash = next(iter(challenger_hashes))
    champion_hash = next(iter(champion_hashes), "")
    paired = _paired_differences_from_blocks(champion or {}, challenger, cutoff, champion_hash, challenger_hash) if champion else []
    challenger["report_version"] = "1.3.0-replay-aggregate"
    challenger["champion_replay"] = champion
    challenger["challenger_replay"] = {"descriptorHash": challenger_hash, "cutoff": cutoff, "blockIds": block_ids}
    challenger["paired_oos_blocks"] = paired
    challenger["oos_block_ids"] = block_ids
    challenger["evaluation"] = {
        "same_untouched_blocks": True,
        "cutoff": cutoff,
        "untouched_test_start": untouched_start,
        "untouched_test_end": untouched_end,
        "oos_block_ids": block_ids,
        "champion_descriptor_hash": champion_hash or None,
        "challenger_descriptor_hash": challenger_hash,
        "paired_block_ids": [row["block_id"] for row in paired],
    }
    challenger["candidate_descriptor"] = reports[0].get("challenger_descriptor")
    challenger["descriptorHash"] = challenger_hash
    return challenger


def _paired_differences_from_blocks(champion: dict[str, Any], challenger: dict[str, Any], cutoff: int, champion_hash: str, challenger_hash: str) -> list[dict[str, Any]]:
    champion_evaluation = champion.get("evaluation", {}) if isinstance(champion.get("evaluation"), dict) else {}
    challenger_evaluation = challenger.get("evaluation", {}) if isinstance(challenger.get("evaluation"), dict) else {}
    if any(int(value.get("cutoff", cutoff)) != cutoff for value in (champion_evaluation, challenger_evaluation)):
        raise ValueError("Champion and Challenger cutoff metadata differ")
    if (champion_evaluation.get("untouched_test_start"), champion_evaluation.get("untouched_test_end")) != (challenger_evaluation.get("untouched_test_start"), challenger_evaluation.get("untouched_test_end")):
        raise ValueError("Champion and Challenger untouched interval metadata differ")
    champion_blocks = {str(row.get("block_id")): float(row.get("mean_r", 0.0)) for row in champion.get("oos_blocks", []) if isinstance(row, dict) and row.get("block_id") is not None}
    challenger_blocks = {str(row.get("block_id")): float(row.get("mean_r", 0.0)) for row in challenger.get("oos_blocks", []) if isinstance(row, dict) and row.get("block_id") is not None}
    for evaluation, actual in ((champion_evaluation, champion_blocks), (challenger_evaluation, challenger_blocks)):
        declared = {str(block_id) for block_id in evaluation.get("oos_block_ids", [])}
        if declared and declared != set(actual):
            raise ValueError("reported OOS blocks do not match the declared untouched calendar universe")
    if set(champion_blocks) != set(challenger_blocks):
        raise ValueError("Champion and Challenger do not share the exact untouched calendar blocks")
    return [
        {"block_id": block_id, "cutoff": cutoff, "champion_descriptor_hash": champion_hash, "challenger_descriptor_hash": challenger_hash, "champion_mean_r": champion_blocks[block_id], "challenger_mean_r": challenger_blocks[block_id], "difference": challenger_blocks[block_id] - champion_blocks[block_id]}
        for block_id in sorted(champion_blocks)
    ]


def aggregate_reports(paths: list[Path]) -> dict[str, Any]:
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    if reports and reports[0].get("schema") == "sol-alerts-replay-shard-v1":
        return _aggregate_replay_reports(reports)
    return _aggregate_single_reports(reports)


def _paired_differences(report: dict[str, Any], champion: dict[str, Any] | None) -> list[float]:
    blocks = report.get("paired_oos_blocks", [])
    if blocks:
        if all(isinstance(block, dict) and "difference" in block for block in blocks):
            return [float(block["difference"]) for block in blocks]
        if all(isinstance(block, dict) and "champion_mean_r" in block and "challenger_mean_r" in block for block in blocks):
            return [float(block["challenger_mean_r"]) - float(block["champion_mean_r"]) for block in blocks]
        # A raw list of returns is not a paired comparison.  Legacy 1.1
        # offline reports are accepted only as an explicit compatibility path;
        # all current reports must carry the literal difference field.
        if (str(report.get("report_version", "")).startswith("1.1") or not report.get("report_version")) and all(isinstance(block, (int, float)) for block in blocks):
            return [float(block) for block in blocks]
        return []
    if champion is None:
        return []
    challenger_blocks = {str(block.get("block_id")): float(block.get("mean_r", 0.0)) for block in report.get("oos_blocks", []) if isinstance(block, dict) and "block_id" in block}
    champion_blocks = {str(block.get("block_id")): float(block.get("mean_r", 0.0)) for block in champion.get("oos_blocks", []) if isinstance(block, dict) and "block_id" in block}
    if set(challenger_blocks) != set(champion_blocks):
        raise ValueError("Champion and Challenger do not share the exact untouched calendar blocks")
    return [challenger_blocks[key] - champion_blocks[key] for key in sorted(challenger_blocks)]


def _report_metrics(report: dict[str, Any], config: dict[str, Any], champion: dict[str, Any] | None = None) -> tuple[float, float, float, list[float], list[float]]:
    events = report.get("portfolio_events", report.get("candidate_events", []))
    values = [float(event.get("net_r", 0.0)) for event in events]
    family_rows = report.get("per_family", {})
    if not values:
        trades = sum(int(row.get("trades", 0)) for row in family_rows.values())
        overall = sum(float(row.get("mean_r", 0.0)) * int(row.get("trades", 0)) for row in family_rows.values()) / trades if trades else sum(float(row.get("mean_r", 0.0)) for row in family_rows.values()) / len(family_rows) if family_rows else 0.0
    else:
        overall = sum(values) / len(values)
    recent = float(report.get("recent", {}).get("mean_r", overall))
    drawdown = float(report.get("portfolio", {}).get("max_drawdown", 1.0))
    contributions = [float(value) for value in report.get("concentration", {}).get("contributions", {}).values()]
    if not contributions:
        contributions = [sum(float(event.get("net_r", 0.0)) for event in events if event.get("symbol") == symbol) for symbol in {event.get("symbol") for event in events}]
    return overall, recent, drawdown, contributions, _paired_differences(report, champion)


def _legacy_selected_parameters(config: dict[str, Any]) -> dict[str, dict[str, str]]:
    keys = _parameter_keys(config)
    return {family: {direction: keys[family][0] for direction in ("long", "short")} for family in FAMILIES}


def prepare_publication(
    report: dict[str, Any],
    config: dict[str, Any],
    previous_weights: dict[str, float] | None = None,
    champion_report: dict[str, Any] | None = None,
    bootstrap_champion: bool = False,
    previous_modes: dict[str, str] | None = None,
    previous_symbol_weights: dict[str, dict[str, float]] | None = None,
    previous_symbol_modes: dict[str, dict[str, str]] | None = None,
    previous_weight_version_id: str | None = None,
    previous_report_id: str | None = None,
    champion_descriptor: dict[str, Any] | None = None,
    evaluated_descriptor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate a frozen descriptor; untouched outcomes can never mutate it."""
    previous_descriptor = champion_descriptor or {
        "schema": DESCRIPTOR_SCHEMA,
        "configVersion": config["configVersion"],
        "selectedParameters": (champion_report or {}).get("walk_forward", {}).get("selected_parameters", _legacy_selected_parameters(config)),
        "weights": previous_weights or config["weights"]["globalBase"],
        "symbolWeights": previous_symbol_weights or {},
        "modes": previous_modes or (champion_report or {}).get("modes", {}),
        "symbolModes": previous_symbol_modes or {},
        "source": {"kind": "legacy-prior"},
    }
    if "descriptorHash" not in previous_descriptor:
        previous_descriptor = {**previous_descriptor, "descriptorHash": descriptor_hash(previous_descriptor)}
    selected_parameters = report.get("walk_forward", {}).get("selected_parameters") or _legacy_selected_parameters(config)
    candidate_descriptor = report.get("candidate_descriptor")
    strict_descriptor = any(key in report for key in ("candidate_descriptor", "descriptorHash", "evaluation"))
    if not isinstance(candidate_descriptor, dict):
        candidate_descriptor = build_candidate_descriptor(config, selected_parameters, report.get("walk_forward", {}).get("selection_evidence", []), symbols=sorted(report.get("per_symbol", {})), previous_descriptor=None if bootstrap_champion and champion_descriptor is None else previous_descriptor)
    candidate_hash = descriptor_hash(candidate_descriptor)
    descriptor_valid = validate_descriptor(config, candidate_descriptor)
    frozen_descriptor_match = evaluated_descriptor is None or (validate_descriptor(config, evaluated_descriptor) and candidate_descriptor == evaluated_descriptor)
    evaluation = report.get("evaluation", {}) if isinstance(report.get("evaluation"), dict) else {}
    evaluated_hash = evaluation.get("challenger_descriptor_hash") or report.get("descriptorHash")
    descriptor_match = descriptor_valid and frozen_descriptor_match and (not strict_descriptor or evaluated_hash == candidate_hash)
    champion_replay = report.get("champion_replay") if isinstance(report.get("champion_replay"), dict) else None
    metrics_champion = champion_replay or champion_report
    challenger_overall, challenger_recent, challenger_dd, contributions, paired = _report_metrics(report, config, metrics_champion)
    if metrics_champion is None:
        champion_overall, champion_recent = challenger_overall, challenger_recent
        champion_exists = False
    else:
        champion_overall, champion_recent, _champion_dd, _champion_contributions, _ = _report_metrics(metrics_champion, config, metrics_champion)
        champion_exists = True
    champion_hash_match = True
    if strict_descriptor and champion_exists:
        expected_champion_hash = evaluation.get("champion_descriptor_hash")
        actual_champion_hash = (champion_descriptor or {}).get("descriptorHash")
        champion_hash_match = bool(expected_champion_hash and actual_champion_hash and expected_champion_hash == actual_champion_hash)
    formal_rows = report.get("per_family", {})
    current_report = bool(report.get("report_version"))
    quality_gate = True
    if current_report:
        quality_gate = bool(report.get("gates", {}).get("formal_alerts_available", False)) and bool(report.get("cost_sensitivity", {}).get("all_stress_scenarios_positive", False)) and bool(report.get("concentration", {}).get("concentration_pass", False)) and float(report.get("portfolio", {}).get("max_drawdown", 1.0)) <= config["research"]["maxPortfolioDrawdown"] and bool(report.get("data_quality", {}).get("prepare_report_validated", False)) and bool(report.get("data_quality", {}).get("point_in_time_membership_supported", False)) and bool(report.get("liquidity_and_costs", {}).get("historical_liquidity_proxy_supported", False)) and report.get("liquidity_and_costs", {}).get("historical_liquidity_proxy_schema") == "sol-alerts-impact-proxy-v1" and _valid_selected_parameters(config, candidate_descriptor.get("selectedParameters"))
    if strict_descriptor:
        quality_gate = quality_gate and descriptor_match and champion_hash_match and bool(evaluation.get("same_untouched_blocks"))
    formal_gate = quality_gate and any(bool(formal_rows.get(family, {}).get("formal_eligible", False)) for family in FAMILIES)
    decision = evaluate_challenger(paired, champion_overall, challenger_overall, champion_recent, challenger_recent, challenger_dd, contributions, config["research"]["maxPortfolioDrawdown"], config["research"]["randomSeed"], min(2_000, config["research"]["bootstrapReplicates"]))
    statistical_pass = (formal_gate if bootstrap_champion and not champion_exists else decision.passes) and (champion_exists or bootstrap_champion)
    valid_combinations = any(bool(formal_rows.get(family, {}).get("formal_eligible", False)) for family in FAMILIES)
    published = bool(statistical_pass and formal_gate and descriptor_match and champion_hash_match and (champion_exists or bootstrap_champion))
    report_id = hashlib.sha256(json.dumps(report, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:32]
    return {
        "publicationVersion": f"{config['configVersion']}-challenger",
        "configVersion": config["configVersion"],
        "kind": "weight-only-config-activation",
        "published": published,
        "bootstrapChampion": bool(bootstrap_champion and not champion_exists),
        "championUnchangedOnFailure": not published,
        "candidateDescriptor": candidate_descriptor,
        "descriptorHash": candidate_hash,
        "weights": candidate_descriptor.get("weights", {}),
        "modes": candidate_descriptor.get("modes", {}),
        "symbolWeights": candidate_descriptor.get("symbolWeights", {}),
        "symbolModes": candidate_descriptor.get("symbolModes", {}),
        "selectedParameters": candidate_descriptor.get("selectedParameters", selected_parameters),
        "evidenceRows": report.get("evidence", {}).get("rows", []),
        "reportLinkage": {"reportId": report_id, "reportVersion": report.get("report_version"), "configVersion": report.get("config_version", config["configVersion"]), "previousReportId": previous_report_id, "descriptorHash": candidate_hash, "evaluatedDescriptorHash": evaluated_hash},
        "requiresChampionChallengerGate": True,
        "rollbackPointer": previous_weight_version_id or "previous_champion",
        "previousWeightVersionId": previous_weight_version_id,
        "decision": {"passes": published, "statisticalPass": statistical_pass, "formalGate": formal_gate, "descriptorMatch": descriptor_match, "frozenDescriptorMatch": frozen_descriptor_match, "championHashMatch": champion_hash_match, "championPresent": champion_exists, "pairedLowerBound95": decision.paired_lower_bound, "overallImprovement": decision.overall_improvement, "recentDegradation": decision.recent_degradation, "challengerDrawdown": challenger_dd, "concentrationShare": decision.concentration_share, "pairedDifferences": paired},
        "evaluation": evaluation,
        "lifecycle": {"allStrategiesPass": valid_combinations and formal_gate, "emergency": False, "persistEvaluation": current_report, "resumeAfterConsecutiveWeeks": 2},
        "challengerReport": report,
        "reportSummary": {"formalAlertsAvailable": report.get("gates", {}).get("formal_alerts_available", False), "portfolioMaxDrawdown": challenger_dd, "perFamily": formal_rows},
    }


class PublicationClient(Protocol):
    def publish_atomically(self, publication: dict[str, Any]) -> dict[str, Any]: ...


class SupabasePublicationClient:
    def __init__(self, url: str, service_role_key: str) -> None:
        self.url = url.rstrip("/")
        self.service_role_key = service_role_key

    def get_champion_report(self) -> dict[str, Any] | None:
        request = Request(f"{self.url}/rest/v1/rpc/sol_get_champion_research_report", data=b"{}", headers={"apikey": self.service_role_key, "Authorization": f"Bearer {self.service_role_key}", "Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
        if isinstance(result, list):
            result = result[0] if result else None
        return result if isinstance(result, dict) else None

    def get_active_weight_version(self) -> dict[str, Any] | None:
        request = Request(f"{self.url}/rest/v1/rpc/sol_get_active_weight_version", data=b"{}", headers={"apikey": self.service_role_key, "Authorization": f"Bearer {self.service_role_key}", "Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
        if isinstance(result, list):
            result = result[0] if result else None
        return result if isinstance(result, dict) else None

    def publish_atomically(self, publication: dict[str, Any]) -> dict[str, Any]:
        request = Request(f"{self.url}/rest/v1/rpc/sol_publish_weight_challenger", data=json.dumps({"p_publication": publication}).encode("utf-8"), headers={"apikey": self.service_role_key, "Authorization": f"Bearer {self.service_role_key}", "Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
        return result


def _publication_descriptor_content_matches(publication: dict[str, Any]) -> bool:
    """Mirror the database content-binding checks at the protected client boundary."""
    descriptor = publication.get("candidateDescriptor")
    report = publication.get("challengerReport")
    if not isinstance(descriptor, dict) or not isinstance(report, dict):
        return False
    fields = {
        "weights": "weights",
        "modes": "modes",
        "symbolWeights": "symbolWeights",
        "symbolModes": "symbolModes",
        "selectedParameters": "selectedParameters",
    }
    if any(publication.get(top_level) != descriptor.get(descriptor_field) for top_level, descriptor_field in fields.items()):
        return False
    nested = report.get("candidate_descriptor")
    return nested is None or nested == descriptor


def publish_if_approved(publication: dict[str, Any], client: PublicationClient) -> dict[str, Any]:
    # The RPC is also the durable weekly lifecycle recorder.  It must receive
    # failed challenger evaluations so halt/resume state is not lost.
    if not _publication_descriptor_content_matches(publication):
        return {"published": False, "reason": "descriptor content mismatch"}
    if not publication.get("published") and not publication.get("lifecycle", {}).get("persistEvaluation", False):
        return {"published": False, "reason": "champion-challenger gates failed"}
    result = client.publish_atomically(publication)
    if not publication.get("published"):
        return {"published": False, "lifecycle": result}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate and optionally atomically publish a Champion-Challenger weight version")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--reports", type=Path, nargs="*")
    parser.add_argument("--champion-report", type=Path)
    parser.add_argument("--challenger-report", type=Path)
    parser.add_argument("--evaluation-descriptors", type=Path, help="immutable descriptor artifact used to verify the evaluated report")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--publish", action="store_true", help="use scoped Supabase publication secrets; otherwise prepare only")
    parser.add_argument("--fetch-champion", action="store_true", help="fetch the non-secret active descriptor for the protected research job")
    parser.add_argument("--bootstrap-champion", action="store_true", help="explicit initial publication after all historical gates pass")
    args = parser.parse_args()
    config = load_config()
    if args.fetch_champion:
        if not args.publish:
            parser.error("--fetch-champion requires the protected --publish boundary")
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        if not url or not key:
            raise SystemExit("protected champion fetch requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY")
        fetched = SupabasePublicationClient(url, key).get_active_weight_version()
        descriptor = fetched.get("descriptor") if isinstance(fetched, dict) else None
        if descriptor is not None and not validate_descriptor(config, descriptor):
            raise SystemExit("active champion descriptor is incompatible or hash-invalid")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({"schema": "sol-alerts-champion-descriptor-v1", "configVersion": config["configVersion"], "descriptor": descriptor, "descriptorHash": descriptor.get("descriptorHash") if descriptor else None}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({"ok": True, "fetch_champion": True, "champion": descriptor is not None}, sort_keys=True))
        return 0
    paths = args.reports or ([] if args.report is None else [args.report])
    if args.challenger_report:
        challenger = json.loads(args.challenger_report.read_text(encoding="utf-8"))
        if isinstance(challenger, dict) and isinstance(challenger.get("publication"), dict) and isinstance(challenger["publication"].get("challengerReport"), dict):
            challenger = challenger["publication"]["challengerReport"]
    elif paths:
        challenger = aggregate_reports(paths) if len(paths) > 1 else json.loads(paths[0].read_text(encoding="utf-8"))
    else:
        parser.error("--report, --reports, or --challenger-report is required")
    expected_challenger_descriptor: dict[str, Any] | None = None
    if args.evaluation_descriptors:
        frozen = json.loads(args.evaluation_descriptors.read_text(encoding="utf-8"))
        expected_challenger = frozen.get("challenger") if isinstance(frozen, dict) else None
        if not validate_descriptor(config, expected_challenger) or challenger.get("descriptorHash") != expected_challenger.get("descriptorHash"):
            raise SystemExit("evaluated challenger does not match the frozen descriptor artifact")
        expected_challenger_descriptor = expected_challenger
        expected_champion = frozen.get("champion")
        report_evaluation = challenger.get("evaluation", {}) if isinstance(challenger.get("evaluation"), dict) else {}
        if expected_champion is not None and report_evaluation.get("champion_descriptor_hash") != expected_champion.get("descriptorHash"):
            raise SystemExit("evaluated champion does not match the frozen descriptor artifact")
    client: SupabasePublicationClient | None = None
    if args.publish:
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        if not url or not key:
            raise SystemExit("guarded publish requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY")
        client = SupabasePublicationClient(url, key)
    active_version = client.get_active_weight_version() if client else None
    champion = json.loads(args.champion_report.read_text(encoding="utf-8")) if args.champion_report else (active_version.get("report") if active_version and isinstance(active_version.get("report"), dict) else (client.get_champion_report() if client else None))
    publication = prepare_publication(
        challenger,
        config,
        previous_weights=active_version.get("weights") if active_version else None,
        champion_report=champion,
        bootstrap_champion=args.bootstrap_champion,
        previous_modes=active_version.get("modes") if active_version else None,
        previous_symbol_weights=active_version.get("symbolWeights") if active_version else None,
        previous_symbol_modes=active_version.get("symbolModes") if active_version else None,
        previous_weight_version_id=str(active_version.get("id")) if active_version and active_version.get("id") else None,
        previous_report_id=str(active_version.get("reportId")) if active_version and active_version.get("reportId") else None,
        champion_descriptor=active_version.get("descriptor") if active_version else (champion.get("candidate_descriptor") if isinstance(champion, dict) else None),
        evaluated_descriptor=expected_challenger_descriptor,
    )
    external_result: dict[str, Any] | None = None
    if args.publish:
        if client is None:
            raise SystemExit("guarded publish client was not initialized")
        external_result = publish_if_approved(publication, client)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps({"publication": publication, "challengerReport": challenger, "external": external_result}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print(json.dumps({"ok": True, "prepared": str(args.output), "published": bool(external_result and external_result.get("published", True)) if args.publish else False, "decision": publication["decision"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
