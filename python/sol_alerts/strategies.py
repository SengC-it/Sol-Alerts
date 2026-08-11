from __future__ import annotations

from dataclasses import dataclass

from .data import Candle, conservative_impact_proxy
from .features import atr, bollinger, ema, median, rsi, state


# The live runtime observes at most 300 closed 15m bars and the completed
# 1h state derived from them.  Formal research uses the same bounded view at
# every trigger; the two-year history is only the chronological input stream.
OBSERVABLE_15M_BARS = 300
OBSERVABLE_1H_BARS = OBSERVABLE_15M_BARS // 4


@dataclass(frozen=True)
class Signal:
    symbol: str
    family: str
    direction: str
    parameter_key: str
    index: int
    trigger_time: int
    entry_low: float
    entry_high: float
    chase_boundary: float
    technical_invalidation: float
    reference_stop: float
    reference_target: float
    gross_reward_risk: float
    shadow_only: bool = False
    market_state: str | None = None
    # Trigger-time observable priority inputs.  These are deliberately not
    # derived from a simulated trade or untouched-test outcome.
    trigger_score: float = 0.0
    trigger_weight: float = 1.0


def aggregate_1h(candles: list[Candle]) -> list[Candle]:
    groups: dict[int, list[Candle]] = {}
    for candle in candles:
        groups.setdefault((candle.open_time // 3_600_000) * 3_600_000, []).append(candle)
    output: list[Candle] = []
    for open_time, group in sorted(groups.items()):
        if len(group) != 4:
            continue
        output.append(Candle(open_time, group[-1].close_time, group[0].open, max(item.high for item in group), min(item.low for item in group), group[-1].close, sum(item.volume for item in group), sum(item.quote_volume for item in group)))
    return output


def _add_work(operation_counter: dict[str, int] | None, key: str, units: int) -> None:
    if operation_counter is not None:
        operation_counter[key] = operation_counter.get(key, 0) + max(0, int(units))


def trigger_time_priority(state_name: str | None, gross_reward_risk: float, weight: float, config: dict) -> float:
    """Score only information known when the closed trigger bar arrives."""
    minimum = max(float(config["execution"]["minimumNetRewardRisk"]), 1e-9)
    rr_component = min(60.0, max(0.0, gross_reward_risk / minimum * 30.0))
    state_component = 25.0 if state_name not in (None, "ambiguous") else 0.0
    base_weights = config["weights"]["globalBase"]
    max_weight = max((float(value) for value in base_weights.values()), default=1.0)
    weight_component = min(15.0, max(0.0, float(weight) / max(max_weight, 1e-9) * 15.0))
    return min(100.0, rr_component + state_component + weight_component)


def _risk_reward(direction: str, entry: float, stop: float, target: float, config: dict) -> float:
    reward = target - entry if direction == "long" else entry - target
    risk = entry - stop if direction == "long" else stop - entry
    impact_rate = conservative_impact_proxy(
        config["universe"]["referenceMaxOrderNotionalUsdt"],
        config["universe"]["minDepthNotionalUsdt"],
        0.01,
        config,
    )
    costs = (entry + target) * (config["execution"]["takerFeeRatePerSide"] + config["execution"]["baseAdverseSlippageRatePerSide"] + impact_rate)
    return (reward - costs) / (risk + costs) if risk + costs > 0 else 0.0


def solve_chase_boundary(direction: str, entry_low: float, entry_high: float, stop: float, target: float, minimum: float, config: dict) -> float:
    current = entry_low if direction == "long" else entry_high
    if _risk_reward(direction, current, stop, target, config) < minimum:
        return current
    low, high = (entry_low, entry_high) if direction == "long" else (entry_low, entry_high)
    for _ in range(40):
        middle = (low + high) / 2
        valid = _risk_reward(direction, middle, stop, target, config) >= minimum
        if direction == "long":
            if valid:
                low = middle
            else:
                high = middle
        elif valid:
            high = middle
        else:
            low = middle
    return low if direction == "long" else high


def next_defensible_structure_target(direction: str, current_close: float, completed_hours: list[Candle], trigger_atr: float) -> float | None:
    """Return the nearest confirmed swing level that is actually observable.

    Flat highs/lows from ordinary bars are not targets. A level must be a
    two-bar swing with visible prominence; if no such level exists the signal
    is rejected instead of manufacturing an ATR multiple target.
    """
    if len(completed_hours) < 5 or trigger_atr <= 0:
        return None
    candidates: list[float] = []
    for index in range(2, len(completed_hours) - 2):
        bar = completed_hours[index]
        neighbors = completed_hours[index - 2:index] + completed_hours[index + 1:index + 3]
        if direction == "long":
            level = bar.high
            neighbor_high = max(item.high for item in neighbors)
            if level > current_close and level >= neighbor_high and level - neighbor_high >= trigger_atr * 0.1:
                candidates.append(level)
        else:
            level = bar.low
            neighbor_low = min(item.low for item in neighbors)
            if level < current_close and level <= neighbor_low and neighbor_low - level >= trigger_atr * 0.1:
                candidates.append(level)
    if not candidates:
        return None
    return min(candidates) if direction == "long" else max(candidates)


def _candidate(
    symbol: str,
    family: str,
    direction: str,
    key: str,
    index: int,
    candles: list[Candle],
    config: dict,
    completed_hours: list[Candle] | None = None,
    trigger_atr: float | None = None,
    market_state: str | None = None,
    operation_counter: dict[str, int] | None = None,
    trigger_weights: dict[str, float] | None = None,
    family_modes: dict[str, str] | None = None,
) -> Signal | None:
    current = candles[index]
    prefix = candles[max(0, index - OBSERVABLE_15M_BARS + 1) : index + 1]
    _add_work(operation_counter, "bounded_work_units", len(prefix))
    current_atr = trigger_atr if trigger_atr is not None else atr(prefix)
    if current_atr <= 0:
        return None
    prior = candles[max(0, index - 40) : index]
    _add_work(operation_counter, "bounded_work_units", len(prior))
    if not prior:
        return None
    low = min(candle.low for candle in prior)
    high = max(candle.high for candle in prior)
    invalidation = low - current_atr * 0.05 if direction == "long" else high + current_atr * 0.05
    stop = invalidation - current_atr * 0.25 if direction == "long" else invalidation + current_atr * 0.25
    one_hour = completed_hours if completed_hours is not None else aggregate_1h(prefix)
    hour_structure = one_hour[-41:-1]
    _add_work(operation_counter, "bounded_work_units", len(hour_structure))
    target = next_defensible_structure_target(direction, current.close, hour_structure, current_atr)
    if target is None:
        return None
    entry_band = current_atr * 0.1
    entry_low = current.close if direction == "long" else current.close - entry_band
    entry_high = current.close + entry_band if direction == "long" else current.close
    ratio = _risk_reward(direction, entry_low if direction == "long" else entry_high, stop, target, config)
    if ratio < config["execution"]["minimumNetRewardRisk"]:
        return None
    chase_boundary = solve_chase_boundary(direction, entry_low, entry_high, stop, target, config["execution"]["minimumNetRewardRisk"], config)
    trigger_weight = float((trigger_weights or config["weights"]["globalBase"]).get(family, 0.0))
    trigger_score = trigger_time_priority(market_state, ratio, trigger_weight, config)
    shadow_only = bool((family_modes or {}).get(family) == "shadow")
    return Signal(symbol, family, direction, key, index, current.close_time, entry_low, entry_high, chase_boundary, invalidation, stop, target, ratio, shadow_only, market_state, trigger_score, trigger_weight)


def generate_signals(
    symbol: str,
    candles: list[Candle],
    config: dict,
    direction: str,
    selected_parameters: dict[str, str] | None = None,
    operation_counter: dict[str, int] | None = None,
    trigger_weights: dict[str, float] | None = None,
    family_modes: dict[str, str] | None = None,
) -> list[Signal]:
    output: list[Signal] = []
    minimum_index = max(60, config["strategies"]["trend_breakout"]["lookbacks"][-1] + 1)
    # Build the closed-hour series once. The previous implementation rebuilt
    # every prefix aggregate for every bar and parameter variant, multiplying
    # the official two-year workload by the grid size.
    all_hours = aggregate_1h(candles)
    if operation_counter is not None:
        operation_counter["aggregate_1h_calls"] = operation_counter.get("aggregate_1h_calls", 0) + 1
    _add_work(operation_counter, "bounded_work_units", len(candles))
    _add_work(operation_counter, "aggregate_1h_work", len(candles))
    breakout_grid = [
        (lookback, buffer, multiplier, f"lookback={lookback};buffer={buffer};volume={multiplier}")
        for lookback in config["strategies"]["trend_breakout"]["lookbacks"]
        for buffer in config["strategies"]["trend_breakout"]["closeBufferAtr"]
        for multiplier in config["strategies"]["trend_breakout"]["volumeMultipliers"]
    ]
    if selected_parameters is not None:
        breakout_grid = [item for item in breakout_grid if item[3] == selected_parameters.get("trend_breakout")]
    pullback = config["strategies"]["trend_pullback"]
    pullback_key = f"ema={pullback['emaPeriod']};structure={pullback['structureLookback']}"
    pullback_enabled = selected_parameters is None or selected_parameters.get("trend_pullback") == pullback_key
    mean_reversion = config["strategies"]["range_mean_reversion"]
    mean_grid = [
        (deviation, lower_rsi, upper_rsi, f"bb={deviation};rsi={lower_rsi}/{upper_rsi}")
        for deviation in mean_reversion["bollingerDeviations"]
        for lower_rsi, upper_rsi in mean_reversion["rsiThresholds"]
    ]
    if selected_parameters is not None:
        mean_grid = [item for item in mean_grid if item[3] == selected_parameters.get("range_mean_reversion")]
    hour_cursor = 0
    for index in range(minimum_index, len(candles)):
        if operation_counter is not None:
            operation_counter["bar_iterations"] = operation_counter.get("bar_iterations", 0) + 1
        current, previous = candles[index], candles[index - 1]
        prefix = candles[max(0, index - OBSERVABLE_15M_BARS + 1) : index + 1]
        _add_work(operation_counter, "bounded_work_units", len(prefix))
        while hour_cursor < len(all_hours) and all_hours[hour_cursor].close_time <= current.close_time:
            hour_cursor += 1
        state_candles = all_hours[max(0, hour_cursor - OBSERVABLE_1H_BARS) : hour_cursor]
        _add_work(operation_counter, "state_window_bars", len(state_candles))
        _add_work(operation_counter, "bounded_work_units", len(state_candles) * 3)
        current_state = state(state_candles, config["marketState"]["trendThresholds"][1], config["marketState"]["rangeThresholds"][0]) if len(state_candles) >= 52 else "ambiguous"
        current_atr = atr(prefix)
        _add_work(operation_counter, "bounded_work_units", len(prefix))
        if current_atr <= 0:
            continue
        prior_volumes = [candle.volume for candle in candles[max(0, index - 20) : index]]
        for lookback, buffer, multiplier, parameter_key in breakout_grid:
            prior = candles[index - lookback : index]
            if len(prior) != lookback:
                continue
            _add_work(operation_counter, "bounded_work_units", len(prior) + len(prior_volumes))
            breakout = (
                current_state == ("trend_up" if direction == "long" else "trend_down")
                and (current.close > max(candle.high for candle in prior) + buffer * current_atr if direction == "long" else current.close < min(candle.low for candle in prior) - buffer * current_atr)
                and current.volume >= median(prior_volumes) * multiplier
            )
            if breakout:
                candidate = _candidate(symbol, "trend_breakout", direction, parameter_key, index, candles, config, state_candles, current_atr, current_state, operation_counter, trigger_weights, family_modes)
                if candidate:
                    output.append(candidate)
        ema_value = ema([candle.close for candle in prefix], pullback["emaPeriod"])
        contraction = [candle.high - candle.low for candle in candles[max(0, index - pullback["contractionLookback"]):index]]
        _add_work(operation_counter, "bounded_work_units", len(prefix) + len(contraction))
        previous_pullback = abs((previous.low if direction == "long" else previous.high) - ema_value) <= current_atr * pullback["maxDistanceAtr"]
        recovery = current.close > previous.high and current.high - current.low > median(contraction) if direction == "long" else current.close < previous.low and current.high - current.low > median(contraction)
        if pullback_enabled and current_state == ("trend_up" if direction == "long" else "trend_down") and previous_pullback and recovery:
            candidate = _candidate(symbol, "trend_pullback", direction, pullback_key, index, candles, config, state_candles, current_atr, current_state, operation_counter, trigger_weights, family_modes)
            if candidate:
                output.append(candidate)
        if current_state == "range":
            for deviation, lower_rsi, upper_rsi, mean_key in mean_grid:
                middle, upper, lower = bollinger([candle.close for candle in prefix[:-1]], mean_reversion["bollingerPeriod"], deviation)
                current_rsi = rsi([candle.close for candle in prefix], mean_reversion["rsiPeriod"])
                _add_work(operation_counter, "bounded_work_units", len(prefix) * 2)
                reverted = previous.low < lower and current.close > lower and current_rsi <= lower_rsi if direction == "long" else previous.high > upper and current.close < upper and current_rsi >= upper_rsi
                if reverted:
                    candidate = _candidate(symbol, "range_mean_reversion", direction, mean_key, index, candles, config, state_candles, current_atr, current_state, operation_counter, trigger_weights, family_modes)
                    if candidate:
                        output.append(candidate)
    return output
