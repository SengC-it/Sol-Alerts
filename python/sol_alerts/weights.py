from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .statistics import concentration_share, mean, paired_block_lower_bound


FAMILIES = ("trend_breakout", "trend_pullback", "range_mean_reversion")
DESCRIPTOR_SCHEMA = "sol-alerts-evaluation-descriptor-v1"


def _allowed_parameter_keys(config: dict[str, Any]) -> dict[str, set[str]]:
    """Return the frozen, config-owned parameter keys accepted by a descriptor."""
    breakout = {
        f"lookback={lookback};buffer={buffer};volume={multiplier}"
        for lookback in config["strategies"]["trend_breakout"]["lookbacks"]
        for buffer in config["strategies"]["trend_breakout"]["closeBufferAtr"]
        for multiplier in config["strategies"]["trend_breakout"]["volumeMultipliers"]
    }
    pullback_config = config["strategies"]["trend_pullback"]
    pullback = {f"ema={pullback_config['emaPeriod']};structure={pullback_config['structureLookback']}"}
    mean_reversion_config = config["strategies"]["range_mean_reversion"]
    mean_reversion = {
        f"bb={deviation};rsi={lower}/{upper}"
        for deviation in mean_reversion_config["bollingerDeviations"]
        for lower, upper in mean_reversion_config["rsiThresholds"]
    }
    return {
        "trend_breakout": breakout,
        "trend_pullback": pullback,
        "range_mean_reversion": mean_reversion,
    }


@dataclass(frozen=True)
class FamilyResult:
    family: str
    mean_r: float
    trades: int
    max_drawdown: float


def descriptor_hash(descriptor: dict[str, Any]) -> str:
    """Hash the immutable evaluated fields, excluding the self-referential hash."""
    payload = {key: value for key, value in descriptor.items() if key != "descriptorHash"}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def validate_descriptor(config: dict[str, Any], descriptor: Any) -> bool:
    if not isinstance(descriptor, dict) or descriptor.get("schema") != DESCRIPTOR_SCHEMA or descriptor.get("configVersion") != config["configVersion"]:
        return False
    selected = descriptor.get("selectedParameters")
    weights = descriptor.get("weights")
    if not isinstance(selected, dict) or set(selected) != set(FAMILIES) or not isinstance(weights, dict) or set(weights) != set(FAMILIES):
        return False
    allowed = _allowed_parameter_keys(config)
    for family in FAMILIES:
        values = selected.get(family)
        if not isinstance(values, dict) or set(values) != {"long", "short"} or not isinstance(values.get("long"), str) or not isinstance(values.get("short"), str):
            return False
        if values["long"] not in allowed[family] or values["short"] not in allowed[family]:
            return False
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and float(value) >= 0 for value in weights.values()):
        return False
    if abs(sum(float(value) for value in weights.values()) - 1.0) > 1e-8:
        return False
    modes = descriptor.get("modes")
    if not isinstance(modes, dict) or set(modes) != set(FAMILIES) or any(modes[family] not in {"active", "shadow"} for family in FAMILIES):
        return False
    symbol_weights = descriptor.get("symbolWeights")
    if not isinstance(symbol_weights, dict):
        return False
    for symbol, values in symbol_weights.items():
        if not isinstance(symbol, str) or not isinstance(values, dict) or set(values) != set(FAMILIES):
            return False
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and float(value) >= 0 for value in values.values()):
            return False
        if abs(sum(float(value) for value in values.values()) - 1.0) > 1e-8:
            return False
    symbol_modes = descriptor.get("symbolModes")
    if not isinstance(symbol_modes, dict) or set(symbol_modes) != set(symbol_weights):
        return False
    for symbol, values in symbol_modes.items():
        if not isinstance(values, dict) or set(values) != set(FAMILIES) or any(values[family] not in {"active", "shadow"} for family in FAMILIES):
            return False
    if not isinstance(descriptor.get("source"), dict):
        return False
    return isinstance(descriptor.get("descriptorHash"), str) and descriptor["descriptorHash"] == descriptor_hash(descriptor)


def build_candidate_descriptor(
    config: dict[str, Any],
    selected_parameters: dict[str, dict[str, str]],
    selection_evidence: list[dict[str, Any]] | None = None,
    *,
    symbols: list[str] | None = None,
    previous_descriptor: dict[str, Any] | None = None,
    source_report_id: str | None = None,
) -> dict[str, Any]:
    """Freeze a proposal using only train/validation sufficient statistics and prior state."""
    prior = previous_descriptor or {}
    base = {family: float((prior.get("weights") or config["weights"]["globalBase"])[family]) for family in FAMILIES}
    rows = selection_evidence or []
    family_scores: dict[str, list[float]] = {family: [] for family in FAMILIES}
    for row in rows:
        family = str(row.get("family", ""))
        direction = str(row.get("direction", ""))
        if family in family_scores and direction in ("long", "short") and row.get("parameter_key") == selected_parameters.get(family, {}).get(direction):
            # Both values are selected before untouched evaluation.  No report
            # family mean, trade outcome, or exit field is read here.
            family_scores[family].append((float(row.get("train_mean_r", 0.0)) + float(row.get("validation_mean_r", 0.0))) / 2.0)
    means = {family: sum(values) / len(values) if values else 0.0 for family, values in family_scores.items()}
    center = sum(means.values()) / len(FAMILIES)
    proposed = {family: max(0.0001, base[family] + (means[family] - center) * 0.02) for family in FAMILIES}
    total = sum(proposed.values())
    proposed = {family: value / total for family, value in proposed.items()}
    weights = weekly_weight_step(base, proposed, config["weights"]["weeklyChangeCap"])

    prior_modes = prior.get("modes", {}) if isinstance(prior.get("modes"), dict) else {}
    modes = {family: weight_mode(weights[family], prior_modes.get(family) == "shadow", config["weights"]["shadowBelow"], config["weights"]["recoveryAbove"]) for family in FAMILIES}
    prior_symbol_weights = prior.get("symbolWeights", {}) if isinstance(prior.get("symbolWeights"), dict) else {}
    symbol_weights = {str(symbol): dict(prior_symbol_weights[symbol]) for symbol in (symbols or prior_symbol_weights) if symbol in prior_symbol_weights}
    symbol_modes_prior = prior.get("symbolModes", {}) if isinstance(prior.get("symbolModes"), dict) else {}
    symbol_modes = {
        symbol: {
            family: weight_mode(float(symbol_weights[symbol].get(family, weights[family])), symbol_modes_prior.get(symbol, {}).get(family) == "shadow", config["weights"]["shadowBelow"], config["weights"]["recoveryAbove"])
            for family in FAMILIES
        }
        for symbol in symbol_weights
    }
    evidence_hash = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    descriptor: dict[str, Any] = {
        "schema": DESCRIPTOR_SCHEMA,
        "configVersion": config["configVersion"],
        "selectedParameters": selected_parameters,
        "weights": weights,
        "symbolWeights": symbol_weights,
        "modes": modes,
        "symbolModes": symbol_modes,
        "source": {
            "kind": "train-validation-proposal",
            "selectionEvidenceHash": evidence_hash,
            "sourceReportId": source_report_id,
            "previousDescriptorHash": prior.get("descriptorHash"),
        },
    }
    descriptor["descriptorHash"] = descriptor_hash(descriptor)
    return descriptor


def shrink_symbol_weights(global_base: dict[str, float], results: dict[str, FamilyResult], prior: float = 30.0, deviation_cap: float = 0.2) -> dict[str, float]:
    observed = [results[family].mean_r for family in FAMILIES if family in results]
    baseline = mean(observed)
    raw: dict[str, float] = {}
    for family in FAMILIES:
        result = results.get(family)
        n = result.trades if result else 0
        shrink = n / (n + prior) if n + prior else 0
        adjustment = max(-deviation_cap, min(deviation_cap, (result.mean_r if result else baseline) - baseline)) * shrink
        raw[family] = max(0.0001, global_base[family] + adjustment)
    total = sum(raw.values())
    normalized = {family: raw[family] / total for family in FAMILIES}
    # Normalization can otherwise push one family beyond the promised
    # percentage-point cap. Redistribute residual weight only through families
    # that are still inside their cap; three families make this deterministic.
    lower = {family: max(0.0001, global_base[family] - deviation_cap) for family in FAMILIES}
    upper = {family: min(1.0, global_base[family] + deviation_cap) for family in FAMILIES}
    for _ in range(len(FAMILIES)):
        clamped = {family: min(upper[family], max(lower[family], normalized[family])) for family in FAMILIES}
        residual = 1.0 - sum(clamped.values())
        if abs(residual) < 1e-12:
            return clamped
        adjustable = [family for family in FAMILIES if (residual > 0 and clamped[family] < upper[family]) or (residual < 0 and clamped[family] > lower[family])]
        if not adjustable:
            return clamped
        share = residual / len(adjustable)
        normalized = {family: clamped[family] + (share if family in adjustable else 0.0) for family in FAMILIES}
    return {family: normalized[family] / sum(normalized.values()) for family in FAMILIES}


@dataclass(frozen=True)
class ChallengerDecision:
    passes: bool
    paired_lower_bound: float
    overall_improvement: float
    recent_degradation: float
    concentration_share: float


def evaluate_challenger(
    paired_differences: list[float],
    champion_overall_mean_r: float,
    challenger_overall_mean_r: float,
    champion_recent_mean_r: float,
    challenger_recent_mean_r: float,
    challenger_drawdown: float,
    symbol_contributions: list[float],
    max_drawdown: float,
    seed: int,
    replicates: int = 2000,
) -> ChallengerDecision:
    paired_lower_bound = paired_block_lower_bound(paired_differences, seed, replicates)
    improvement = challenger_overall_mean_r - champion_overall_mean_r
    recent_degradation = champion_recent_mean_r - challenger_recent_mean_r
    concentration = concentration_share(symbol_contributions)
    passes = paired_lower_bound > 0 and improvement > 0 and recent_degradation <= 0.1 and challenger_drawdown <= max_drawdown and concentration < 0.5
    return ChallengerDecision(passes, paired_lower_bound, improvement, recent_degradation, concentration)


def weekly_weight_step(previous: dict[str, float], proposed: dict[str, float], change_cap: float = 0.1) -> dict[str, float]:
    largest_delta = max(abs(proposed[family] - previous[family]) for family in FAMILIES)
    factor = min(1.0, change_cap / largest_delta) if largest_delta else 0.0
    return {family: previous[family] + (proposed[family] - previous[family]) * factor for family in FAMILIES}


def weight_mode(weight: float, currently_shadow: bool, shadow_below: float = 0.2, recovery_above: float = 0.25) -> str:
    if currently_shadow:
        return "active" if weight > recovery_above else "shadow"
    return "shadow" if weight < shadow_below else "active"
