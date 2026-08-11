from __future__ import annotations

from dataclasses import dataclass, replace

from .data import Candle, conservative_impact_proxy
from .features import state
from .strategies import Signal, aggregate_1h


@dataclass(frozen=True)
class Trade:
    signal: Signal
    entry_index: int
    exit_index: int
    entry_price: float
    exit_price: float
    reason: str
    gross_r: float
    net_r: float
    shadow_only: bool = False
    funding_cost: float = 0.0
    entry_time: int = 0
    exit_time: int = 0
    fees_r: float = 0.0
    impact_cost: float = 0.0
    risk_budget: float = 0.0


def find_exit(signal: Signal, candles: list[Candle], entry_index: int, max_hold_bars: int) -> tuple[int, float, str]:
    """Find the first exit using only bars at or after the actual fill.

    Stop is intentionally evaluated before target when both are touched in
    one candle.  Technical invalidation is a close-based condition and is
    kept separate from the wider reference stop.
    """
    if entry_index >= len(candles):
        raise ValueError("entry index is outside candle series")
    last_index = min(len(candles) - 1, entry_index + max(1, max_hold_bars) - 1)
    for index in range(entry_index, last_index + 1):
        candle = candles[index]
        stop_hit = candle.low <= signal.reference_stop if signal.direction == "long" else candle.high >= signal.reference_stop
        target_hit = candle.high >= signal.reference_target if signal.direction == "long" else candle.low <= signal.reference_target
        invalidated = candle.close <= signal.technical_invalidation if signal.direction == "long" else candle.close >= signal.technical_invalidation
        if stop_hit:
            # A gap through the stop is filled at the next worse available
            # price.  The adverse slippage/impact adjustment is applied by
            # simulate_signal after this raw fill is selected.
            gap_fill = min(candle.open, signal.reference_stop) if signal.direction == "long" else max(candle.open, signal.reference_stop)
            return index, gap_fill, "stop"
        if target_hit:
            return index, signal.reference_target, "target"
        if invalidated:
            return index, candle.close, "invalidation"
    return last_index, candles[last_index].close, "time"


def _impact_rate(candles: list[Candle], entry_index: int, config: dict) -> float:
    recent = candles[max(0, entry_index - 20):entry_index]
    volatility = sum(abs(item.close / item.open - 1.0) for item in recent if item.open > 0) / max(1, len(recent))
    return conservative_impact_proxy(
        float(config["universe"]["referenceMaxOrderNotionalUsdt"]),
        float(config["universe"]["minDepthNotionalUsdt"]),
        volatility,
        config,
    )


def simulate_signal(signal: Signal, candles: list[Candle], config: dict, funding_rates: dict[int, float] | None = None) -> Trade | None:
    """Simulate one signal with absolute timestamps and direction-aware costs."""
    validity_end = signal.trigger_time + int(config["execution"]["entryValidityMinutes"]) * 60_000
    entry_index: int | None = None
    # The signal is confirmed on a closed bar.  Every subsequent 15m open in
    # the validity window is a permitted fill opportunity.
    for index in range(signal.index + 1, len(candles)):
        candle = candles[index]
        if candle.open_time > validity_end:
            break
        previous = candles[index - 1]
        if (signal.direction == "long" and previous.close <= signal.technical_invalidation) or (signal.direction == "short" and previous.close >= signal.technical_invalidation):
            return None
        if signal.market_state is not None:
            completed_hours = aggregate_1h(candles[:index])
            if len(completed_hours) < 52:
                continue
            current_state = state(completed_hours, config["marketState"]["trendThresholds"][1], config["marketState"]["rangeThresholds"][0])
            expected_state = signal.market_state
            family_valid = (signal.family == "range_mean_reversion" and current_state == "range") or (signal.family != "range_mean_reversion" and current_state == ("trend_up" if signal.direction == "long" else "trend_down"))
            if current_state != expected_state or not family_valid:
                continue
        valid = candle.open <= signal.chase_boundary if signal.direction == "long" else candle.open >= signal.chase_boundary
        if valid:
            entry_index = index
            break
    if entry_index is None:
        return None

    candle = candles[entry_index]
    impact = _impact_rate(candles, entry_index, config)
    adverse = float(config["execution"]["baseAdverseSlippageRatePerSide"]) + impact
    entry = candle.open * (1 + adverse if signal.direction == "long" else 1 - adverse)
    hold_ms = int(config["execution"]["maxHoldHours"]) * 60 * 60_000
    max_bars = max(1, hold_ms // (15 * 60_000))
    exit_index, raw_exit, reason = find_exit(signal, candles, entry_index, max_bars)
    exit_time = candles[exit_index].close_time
    exit_price = raw_exit * (1 - adverse if signal.direction == "long" else 1 + adverse)

    gross_risk = entry - signal.reference_stop if signal.direction == "long" else signal.reference_stop - entry
    if gross_risk <= 0:
        return None
    pnl = exit_price - entry if signal.direction == "long" else entry - exit_price
    gross_r = pnl / gross_risk
    fee_amount = (entry + exit_price) * float(config["execution"]["takerFeeRatePerSide"])
    # Positive funding is a debit for longs and a credit for shorts.  Funding
    # exactly at entry is excluded; the event at the exit boundary is owed.
    notional = (entry + abs(exit_price)) / 2
    funding_cost = 0.0
    for funding_time, rate in sorted((funding_rates or {}).items()):
        if candle.open_time < funding_time <= exit_time:
            funding_cost += notional * rate * (1.0 if signal.direction == "long" else -1.0)
    fees_r = fee_amount / gross_risk
    funding_r = funding_cost / gross_risk
    net_r = gross_r - fees_r - funding_r
    return Trade(
        signal=signal,
        entry_index=entry_index,
        exit_index=exit_index,
        entry_price=entry,
        exit_price=exit_price,
        reason=reason,
        gross_r=gross_r,
        net_r=net_r,
        shadow_only=bool(getattr(signal, "shadow_only", False)) or signal.index < 0,
        funding_cost=funding_cost,
        entry_time=candle.open_time,
        exit_time=exit_time,
        fees_r=fees_r,
        impact_cost=impact,
    )


def max_drawdown(equity: list[float]) -> float:
    peak = equity[0] if equity else 0.0
    result = 0.0
    for value in equity:
        peak = max(peak, value)
        result = max(result, (peak - value) / peak if peak else 0.0)
    return result


def _apply_exits(open_trades: list[Trade], at_time: int, equity: float, curve: list[float], risk_rate: float) -> tuple[list[Trade], float]:
    due = sorted((trade for trade in open_trades if trade.exit_time <= at_time), key=lambda trade: (trade.exit_time, trade.signal.symbol, trade.signal.family, trade.signal.direction))
    remaining = [trade for trade in open_trades if trade.exit_time > at_time]
    for trade in due:
        equity += equity * risk_rate * trade.net_r
        curve.append(equity)
    return remaining, equity


def simulate_portfolio(
    signals: list[Signal],
    candles_by_symbol: dict[str, list[Candle]],
    config: dict,
    starting_equity: float = 100_000.0,
    funding_by_symbol: dict[str, dict[int, float]] | None = None,
) -> dict[str, object]:
    """Simulate one chronological portfolio across unequal symbol histories.

    Capacity uses absolute entry/exit timestamps.  Shadow trades go through
    the same fill/exit/funding path but never enter the formal active set or
    equity curve.
    """
    candidates: list[Trade] = []
    for signal in signals:
        candles = candles_by_symbol.get(signal.symbol)
        if not candles:
            continue
        trade = simulate_signal(signal, candles, config, (funding_by_symbol or {}).get(signal.symbol))
        if trade is not None:
            candidates.append(trade)
    candidates.sort(key=lambda trade: (trade.entry_time, trade.signal.symbol, trade.signal.direction, trade.signal.family, trade.signal.parameter_key))

    accepted: list[Trade] = []
    shadow: list[Trade] = []
    active: list[Trade] = []
    equity = starting_equity
    curve = [equity]
    risk_rate = float(config["execution"]["riskPerSimulatedTrade"])
    for candidate in candidates:
        if candidate.shadow_only:
            shadow.append(candidate)
            continue
        active, equity = _apply_exits(active, candidate.entry_time, equity, curve, risk_rate)
        long_count = sum(item.signal.direction == "long" for item in active)
        short_count = sum(item.signal.direction == "short" for item in active)
        if (
            len(active) >= int(config["execution"]["maxOpenPositions"])
            or (candidate.signal.direction == "long" and long_count >= int(config["execution"]["maxLongPositions"]))
            or (candidate.signal.direction == "short" and short_count >= int(config["execution"]["maxShortPositions"]))
        ):
            shadow.append(replace(candidate, shadow_only=True))
            continue
        accepted_trade = replace(candidate, risk_budget=equity * risk_rate)
        accepted.append(accepted_trade)
        active.append(accepted_trade)

    active, equity = _apply_exits(active, 2**63 - 1, equity, curve, risk_rate)
    return {
        "trades": accepted,
        "shadow_trades": shadow,
        "starting_equity": starting_equity,
        "ending_equity": equity,
        "max_drawdown": max_drawdown(curve),
        "equity_curve": curve,
    }
