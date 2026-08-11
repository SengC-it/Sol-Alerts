import type { Candle, Direction, SignalCandidate, StrategyConfig } from "./types.js";

export interface SimulatedTrade {
  symbol: string;
  direction: Direction;
  triggerTime: number;
  entryTime: number;
  entryPrice: number;
  exitTime: number;
  exitPrice: number;
  exitReason: "stop" | "target" | "time" | "invalidation";
  grossR: number;
  netR: number;
  shadowOnly: boolean;
  riskBudget?: number;
}

export interface PortfolioSimulation {
  startingEquity: number;
  endingEquity: number;
  trades: SimulatedTrade[];
  shadowTrades: SimulatedTrade[];
  maxDrawdown: number;
  equityCurve: Array<{ time: number; equity: number }>;
}

function adverseEntry(direction: Direction, open: number, slippageRate: number): number {
  return direction === "long" ? open * (1 + slippageRate) : open * (1 - slippageRate);
}

function adverseExit(direction: Direction, price: number, slippageRate: number): number {
  return direction === "long" ? price * (1 - slippageRate) : price * (1 + slippageRate);
}

function fee(entry: number, exit: number, rate: number): number {
  return (entry + exit) * rate;
}

export function findExit(
  direction: Direction,
  candles: Candle[],
  entryIndex: number,
  stop: number,
  target: number,
  maxHoldBars: number,
): { index: number; price: number; reason: SimulatedTrade["exitReason"] } {
  const lastIndex = Math.min(candles.length - 1, entryIndex + maxHoldBars - 1);
  for (let index = entryIndex; index <= lastIndex; index += 1) {
    const candle = candles[index];
    if (!candle) continue;
    const stopHit = direction === "long" ? candle.low <= stop : candle.high >= stop;
    const targetHit = direction === "long" ? candle.high >= target : candle.low <= target;
    if (stopHit) {
      const gapFill = direction === "long" ? Math.min(candle.open, stop) : Math.max(candle.open, stop);
      return { index, price: gapFill, reason: "stop" };
    }
    if (targetHit) return { index, price: target, reason: "target" };
  }
  const exitIndex = lastIndex;
  const exitCandle = candles[exitIndex];
  if (!exitCandle) throw new Error("Cannot time-exit without a candle");
  return { index: exitIndex, price: exitCandle.close, reason: "time" };
}

function firstFillIndex(candles: Candle[], triggerTime: number, expiresAt: number, direction: Direction, chaseBoundary: number): number {
  const index = candles.findIndex((candle) => candle.openTime > triggerTime && candle.openTime <= expiresAt);
  if (index < 0) return -1;
  const candle = candles[index];
  if (!candle) return -1;
  const valid = direction === "long" ? candle.open <= chaseBoundary : candle.open >= chaseBoundary;
  return valid ? index : -1;
}

function tradeFromSignal(signal: SignalCandidate, candles: Candle[], config: StrategyConfig): SimulatedTrade | null {
  const entryIndex = firstFillIndex(candles, signal.triggerTime, signal.expiresAt, signal.direction, signal.chaseBoundary);
  if (entryIndex < 0) return null;
  const entryCandle = candles[entryIndex];
  if (!entryCandle) return null;
  const entryPrice = adverseEntry(signal.direction, entryCandle.open, config.execution.baseAdverseSlippageRatePerSide);
  const maxHoldBars = config.execution.maxHoldHours * 4;
  const exit = findExit(signal.direction, candles, entryIndex, signal.referenceStop, signal.referenceTarget, maxHoldBars);
  const exitPrice = adverseExit(signal.direction, exit.price, config.execution.baseAdverseSlippageRatePerSide);
  const grossRisk = signal.direction === "long" ? entryPrice - signal.referenceStop : signal.referenceStop - entryPrice;
  const grossPnl = signal.direction === "long" ? exitPrice - entryPrice : entryPrice - exitPrice;
  const grossR = grossRisk <= 0 ? 0 : grossPnl / grossRisk;
  const costR = grossRisk <= 0 ? 0 : fee(entryPrice, exitPrice, config.execution.takerFeeRatePerSide) / grossRisk;
  return {
    symbol: signal.symbol,
    direction: signal.direction,
    triggerTime: signal.triggerTime,
    entryTime: entryCandle.openTime,
    entryPrice,
    exitTime: candles[exit.index]?.closeTime ?? entryCandle.closeTime,
    exitPrice,
    exitReason: exit.reason,
    grossR,
    netR: grossR - costR,
    shadowOnly: signal.shadowOnly,
  };
}

function maxDrawdown(equityCurve: Array<{ time: number; equity: number }>): number {
  let peak = equityCurve[0]?.equity ?? 0;
  let drawdown = 0;
  for (const point of equityCurve) {
    peak = Math.max(peak, point.equity);
    if (peak > 0) drawdown = Math.max(drawdown, (peak - point.equity) / peak);
  }
  return drawdown;
}

export function simulatePortfolio(
  signals: SignalCandidate[],
  candlesBySymbol: Map<string, Candle[]>,
  config: StrategyConfig,
  startingEquity = 100_000,
): PortfolioSimulation {
  const ordered = [...signals].sort((left, right) => left.triggerTime - right.triggerTime || left.symbol.localeCompare(right.symbol));
  const accepted: SimulatedTrade[] = [];
  const shadowTrades: SimulatedTrade[] = [];
  let active: SimulatedTrade[] = [];
  let equity = startingEquity;
  const equityCurve: Array<{ time: number; equity: number }> = [{ time: ordered[0]?.triggerTime ?? 0, equity }];
  const closeDue = (cutoff: number): void => {
    const due = active.filter((item) => item.exitTime <= cutoff).sort((left, right) => left.exitTime - right.exitTime || left.symbol.localeCompare(right.symbol));
    active = active.filter((item) => item.exitTime > cutoff);
    for (const item of due) {
      equity += (item.riskBudget ?? equity * config.execution.riskPerSimulatedTrade) * item.netR;
      equityCurve.push({ time: item.exitTime, equity });
    }
  };
  for (const signal of ordered) {
    const candles = candlesBySymbol.get(signal.symbol);
    if (!candles) continue;
    const trade = tradeFromSignal(signal, candles, config);
    if (!trade) continue;
    if (signal.shadowOnly) {
      shadowTrades.push(trade);
      continue;
    }
    closeDue(trade.entryTime);
    const longCount = active.filter((item) => item.direction === "long").length;
    const shortCount = active.filter((item) => item.direction === "short").length;
    if (
      active.length >= config.execution.maxOpenPositions ||
      (trade.direction === "long" && longCount >= config.execution.maxLongPositions) ||
      (trade.direction === "short" && shortCount >= config.execution.maxShortPositions)
    ) {
      shadowTrades.push({ ...trade, shadowOnly: true });
      continue;
    }
    const riskBudget = equity * config.execution.riskPerSimulatedTrade;
    const acceptedTrade = { ...trade, riskBudget };
    accepted.push(acceptedTrade);
    active.push(acceptedTrade);
  }
  closeDue(Number.MAX_SAFE_INTEGER);
  return {
    startingEquity,
    endingEquity: equity,
    trades: accepted,
    shadowTrades,
    maxDrawdown: maxDrawdown(equityCurve),
    equityCurve,
  };
}
